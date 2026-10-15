"""Bounded, offline, fail-closed integrity screening; not a semantic safety proof."""
from __future__ import annotations
import hashlib
import json
import os
import posixpath
from pathlib import Path, PurePosixPath
import re
from urllib.parse import unquote, urlsplit

MAX_CANDIDATES = 128
MAX_FILES = 256
MAX_FILE_BYTES = 262144
MAX_CANDIDATE_BYTES = 2097152
MAX_TOTAL_BYTES = 33554432
LICENSES = {"MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC", "CC0-1.0"}
TEXT_SUFFIXES = {".md", ".txt", ".json", ".yaml", ".yml", ".csv", ".toml"}
SUSPICIOUS = (
    r"ignore\s+(?:all\s+)?(?:previous|system|developer)\s+instructions",
    r"(?:override|supersede|take\s+precedence\s+over)\s+(?:the\s+)?(?:system|developer|host)\s+(?:instructions|policy|permissions)",
    r"disable\s+(?:all\s+)?(?:safety|security|approval)\s+(?:checks|gates|controls)",
    r"(?:send|upload|exfiltrate)\s+(?:your\s+|the\s+)?(?:api\s*keys|credentials|secrets)\s+to\s+https?://",
)

def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe(base: Path, value: object) -> Path:
    if not isinstance(value, str) or not value or len(value) > 512 or "\\" in value or "\x00" in value:
        raise ValueError("unsafe_path")
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(p in ("..", ".") for p in value.split("/")):
        raise ValueError("unsafe_path")
    current = base
    for part in pure.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("symlink")
    if not current.resolve().is_relative_to(base.resolve()):
        raise ValueError("unsafe_path")
    return current


def _inventory(base: Path) -> set[str]:
    found: set[str] = set()
    count = 0
    stack = [(base, 0)]
    while stack:
        directory, depth = stack.pop()
        if depth > 16:
            raise ValueError("inventory_depth")
        with os.scandir(directory) as entries:
            for entry in entries:
                count += 1
                if count > MAX_FILES * 2:
                    raise ValueError("inventory_limit")
                if entry.is_symlink():
                    raise ValueError("symlink")
                if entry.is_dir(follow_symlinks=False):
                    stack.append((Path(entry.path), depth + 1))
                elif entry.is_file(follow_symlinks=False):
                    found.add(Path(entry.path).relative_to(base).as_posix())
                    if len(found) > MAX_FILES:
                        raise ValueError("inventory_limit")
                else:
                    raise ValueError("non_regular_file")
    return found


def frontmatter(text: str) -> dict[str, str]:
    match = re.match(r"\A---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)", text)
    if not match:
        return {}
    # Parse only discovery scalars. Never evaluate YAML tags, aliases or objects.
    fields: dict[str, str] = {}
    key = None
    for line in match[1].splitlines():
        item = re.match(r"^([a-z][a-z0-9_-]*):\s*(.*?)\s*$", line)
        if item:
            key = item[1]
            fields[key] = item[2].strip("\"'") if item[2] not in ("|", ">", "|-", ">-") else ""
        elif key and line[:1].isspace() and line.strip():
            fields[key] += " " + line.strip()
    return {k: v.strip() for k, v in fields.items()}


def _normal(text: str) -> str:
    # Preserve case/code semantics; only normalize layout and frontmatter metadata.
    body = re.sub(r"\A---\r?\n[\s\S]*?\r?\n---(?:\r?\n|$)", "", text)
    return "\n".join(line.rstrip() for line in body.replace("\r\n", "\n").splitlines()).strip()


def screen_catalog(root: Path, manifest: dict) -> dict:
    root = Path(root).resolve()
    report = {"schema": "gradia.skills.screen.v1", "screenVersion": "0.1.0", "records": [], "errors": [], "limits": {"candidates": MAX_CANDIDATES, "filesPerCandidate": MAX_FILES, "fileBytes": MAX_FILE_BYTES, "candidateBytes": MAX_CANDIDATE_BYTES, "totalBytes": MAX_TOTAL_BYTES}, "boundary": "Static integrity, packaging and heuristic screening only; behavioral quality, tool compatibility and legal clearance are not established"}
    if not isinstance(manifest, dict) or manifest.get("schemaVersion") != 1 or not isinstance(manifest.get("sources"), list) or not isinstance(manifest.get("candidates"), list):
        report["errors"].append("invalid_manifest")
        return report
    if len(manifest["candidates"]) > MAX_CANDIDATES or len(manifest["sources"]) > MAX_CANDIDATES:
        report["errors"].append("catalog_limit")
        return report
    sources = {}
    for source in manifest["sources"]:
        if not isinstance(source, dict) or not isinstance(source.get("id"), str) or source["id"] in sources:
            report["errors"].append("invalid_source")
            return report
        sources[source["id"]] = source
    report['sourceManifestSha256'] = digest(json.dumps(manifest,sort_keys=True,separators=(',',':')).encode())
    report['evaluatorSha256'] = digest(Path(__file__).read_bytes())
    ids: set[str] = set()
    normalized: dict[str, str] = {}
    words: dict[str, set[str]] = {}
    total_bytes = 0
    for candidate in manifest["candidates"]:
        candidate = candidate if isinstance(candidate, dict) else {}
        identity = candidate.get("id")
        entry = {"id": identity if isinstance(identity, str) and len(identity) <= 96 else "invalid", "status": "quarantined", "reasons": [], "resources": [], "contentSha256": None, "behavioral": "not_evaluated", "checks": {}, "overlaps": []}
        report["records"].append(entry)
        reasons = entry["reasons"]
        try:
            if not isinstance(identity, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,95}", identity) or identity in ids:
                raise ValueError("invalid_or_duplicate_id")
            ids.add(identity)
            source = sources.get(candidate.get("sourceId"))
            if source is None:
                raise ValueError("unknown_source")
            pin = source.get("commit", "")
            repository = source.get("repository", "")
            if not isinstance(pin, str) or not re.fullmatch(r"[a-f0-9]{40}", pin) or candidate.get("commit") != pin:
                raise ValueError("source_pin_mismatch")
            if not isinstance(repository, str) or not re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
                raise ValueError("invalid_repository")
            url = candidate.get("sourceUrl", "")
            if not isinstance(url, str) or not any(url.startswith(repository + kind + pin + "/") for kind in ("/tree/", "/blob/")):
                raise ValueError("source_url_mismatch")
            if candidate.get("license") not in LICENSES or source.get("license") != candidate.get("license"):
                raise ValueError("license_not_supported")
            base = _safe(root, candidate.get("path"))
            if not base.is_dir():
                raise ValueError("missing_candidate")
            files = candidate.get("files")
            if not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES:
                raise ValueError("inventory_limit")
            contents: dict[str, bytes] = {}
            candidate_bytes = 0
            for item in files:
                if not isinstance(item, dict):
                    raise ValueError("invalid_inventory")
                path = _safe(base, item.get("path"))
                relative = path.relative_to(base).as_posix()
                if relative in contents or not path.is_file():
                    raise ValueError("missing_or_duplicate_file")
                metadata_stat = path.stat()
                if metadata_stat.st_nlink > 1:
                    raise ValueError("hardlinked_file")
                size = metadata_stat.st_size
                if size > MAX_FILE_BYTES or size != item.get("bytes"):
                    raise ValueError("file_size_or_limit")
                candidate_bytes += size
                total_bytes += size
                if candidate_bytes > MAX_CANDIDATE_BYTES or total_bytes > MAX_TOTAL_BYTES:
                    raise ValueError("byte_limit")
                with path.open("rb") as stream:
                    data = stream.read(MAX_FILE_BYTES + 1)
                if len(data) != size or digest(data) != item.get("sha256"):
                    raise ValueError("content_hash_mismatch")
                contents[relative] = data
            if _inventory(base) != set(contents):
                raise ValueError("unlisted_file")
            license_path = candidate.get("licensePath")
            if license_path not in contents or digest(contents[license_path]) != source.get("licenseSha256"):
                raise ValueError("license_hash_mismatch")
            license_text = contents[license_path].decode("utf-8", errors="strict")
            license_markers = {"MIT": "permission is hereby granted", "Apache-2.0": "apache license", "BSD-2-Clause": "redistribution and use", "BSD-3-Clause": "redistribution and use", "ISC": "permission to use", "CC0-1.0": "creative commons"}
            if license_markers[candidate["license"]] not in license_text.casefold():
                raise ValueError("license_text_mismatch")
            entry["checks"]["sourceAndLicensePins"] = "pass"
            entrypoint = candidate.get("entrypoint")
            if entrypoint not in contents:
                raise ValueError("missing_entrypoint")
            text = contents[entrypoint].decode("utf-8", errors="strict")
            metadata = frontmatter(text)
            if not metadata.get("name") or not metadata.get("description"):
                raise ValueError("missing_frontmatter")
            entry["contentSha256"] = digest(contents[entrypoint])
            executable = []
            decoded: dict[str, str] = {}
            for relative, data in contents.items():
                path = Path(relative)
                if path.suffix.lower() not in TEXT_SUFFIXES and path.name.upper() not in {"LICENSE", "NOTICE", "COPYING", "AUTHORS"}:
                    executable.append(relative)
                    continue
                try:
                    decoded[relative] = data.decode("utf-8", errors="strict")
                except UnicodeDecodeError:
                    raise ValueError("binary_resource")
            if executable:
                reasons.append("requires_execution_or_asset_review")
            for relative, body in decoded.items():
                if any(len(line) > 8192 for line in body.splitlines()):
                    reasons.append("line_length_limit")
                    continue
                if any(re.search(pattern, body, flags=re.I) for pattern in SUSPICIOUS):
                    reasons.append("authority_escalation_review")
                # Markdown inline links; directory links are preserved if their files exist.
                links = [link for line in body.splitlines() for link in re.findall(r"!?\[[^\[\]]*\]\(([^\s)]+)(?:\s+[^)]*)?\)", line)]
                links += re.findall(r"^[ \t]{0,3}\[[^\[\]\r\n]+\]:[ \t]*<?([^\s>]+)>?", body, flags=re.M)
                for link in links:
                    link = link.strip("<>")
                    target = urlsplit(link)
                    if target.scheme:
                        if target.scheme not in ("https", "http", "mailto"):
                            reasons.append("unsafe_reference_scheme")
                        continue
                    if target.netloc:
                        reasons.append("unsafe_reference_scheme")
                        continue
                    reference = unquote(target.path)
                    if not reference:
                        continue
                    joined = posixpath.normpath((PurePosixPath(relative).parent / reference).as_posix())
                    try:
                        resolved = _safe(base, joined)
                        if not resolved.exists():
                            reasons.append("missing_relative_reference")
                    except ValueError:
                        reasons.append("reference_escape_or_symlink")
            blocked = candidate.get("blockedReasons", [])
            if not isinstance(blocked, list) or len(blocked) > 32:
                reasons.append("invalid_blocking_metadata")
            elif blocked:
                reasons.append("upstream_intake_blocked")
            entry["checks"].update({"inventoryIntegrity": "pass", "format": "pass", "resourceLinks": "fail" if any("reference" in r for r in reasons) else "pass", "instructionHeuristics": "review" if "authority_escalation_review" in reasons else "pass", "execution": "not_run"})
            normal_hash = digest(_normal(text).encode())
            if normal_hash in normalized:
                reasons.append("duplicate_of:" + normalized[normal_hash])
                entry["duplicateOf"] = normalized[normal_hash]
            if not reasons:
                normalized[normal_hash] = identity
                entry["status"] = "screened-reference"
                for relative, body in decoded.items():
                    if relative == entrypoint:
                        continue
                    entry["resources"].append({"id": digest(relative.encode())[:16], "name": relative, "mediaType": "text/plain; charset=utf-8", "bytes": len(contents[relative]), "contentSha256": digest(contents[relative])})
            tokens = set(re.findall(r"\b[a-z][a-z0-9_-]{2,}\b", _normal(text).lower()))
            if len(tokens) <= 5000:
                for other, prior in words.items():
                    score = len(tokens & prior) / max(1, len(tokens | prior))
                    if score >= .55:
                        entry["overlaps"].append({"id": other, "score": round(score, 3), "method": "lexical-jaccard; not semantic equivalence"})
                words[identity] = tokens
        except (ValueError, OSError, UnicodeDecodeError, TypeError, KeyError) as exc:
            reasons.append(str(exc) if isinstance(exc, ValueError) and len(str(exc)) < 80 else "invalid_or_unreadable_candidate")
        entry["reasons"] = list(dict.fromkeys(reasons))
        if entry["reasons"]:
            entry["status"] = "quarantined"
            entry["resources"] = []
    report["summary"] = {"candidates": len(report["records"]), "screened": sum(r["status"] == "screened-reference" for r in report["records"]), "quarantined": sum(r["status"] == "quarantined" for r in report["records"]), "inspectedBytes": total_bytes, "behavioralEvaluations": 0}
    return report


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.manifest.stat().st_size > 2097152:
        parser.error("Manifest exceeds 2MiB")
    report = screen_catalog(args.root, json.loads(args.manifest.read_text()))
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report.get("summary", {"errors": report["errors"]})))
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
