"""Deterministic screening of independently pinned receipts, not an AI judge."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
from pathlib import Path
from typing import Any


MAX_ARTIFACT_ENTRIES = 20000
MAX_ARTIFACT_DEPTH = 64


class InputError(ValueError):
    pass


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InputError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def read_json(path: Path) -> dict:
    try:
        if path.stat().st_size > 2 * 1024 * 1024:
            raise InputError("JSON control input exceeds 2 MiB")
        value = json.loads(path.read_text(), object_pairs_hook=_pairs,
                           parse_constant=lambda value: (_ for _ in ()).throw(InputError("Nonfinite JSON number")))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InputError(f"Cannot read JSON: {path.name}") from exc
    if not isinstance(value, dict):
        raise InputError("Expected JSON object")
    return value


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def protocol() -> dict:
    return read_json(Path(__file__).with_name("protocol.json"))


def artifact_digest(root: Path, spec: dict | None = None) -> str:
    """Hash sorted path/size/content-digest entries, including source and tests."""
    spec = spec or protocol()
    if root.is_symlink() or not root.is_dir():
        raise InputError("Artifact root must be a real directory")
    entries = []
    total = 0
    limits = spec["artifact_limits"]
    # Stream directory entries so an oversized directory cannot be materialized first.
    pending = [(root, 0)]
    visited_entries = 0
    while pending:
        directory, depth = pending.pop()
        with os.scandir(directory) as children:
            for child in children:
                visited_entries += 1
                if visited_entries > MAX_ARTIFACT_ENTRIES:
                    raise InputError("Artifact exceeds directory-entry budget")
                path = Path(child.path)
                if child.is_symlink():
                    raise InputError("Artifact symlinks are not supported")
                if child.is_dir(follow_symlinks=False):
                    if depth + 1 > MAX_ARTIFACT_DEPTH:
                        raise InputError("Artifact exceeds directory-depth budget")
                    if path.name not in spec["excluded_directories"]:
                        pending.append((path, depth + 1))
                elif child.is_file(follow_symlinks=False):
                    if path.suffix in spec["excluded_suffixes"]:
                        continue
                    size = child.stat(follow_symlinks=False).st_size
                    total += size
                    if total > limits["max_bytes"] or len(entries) >= limits["max_files"]:
                        raise InputError("Artifact exceeds screening budget")
                    content = path.read_bytes()
                    if len(content) != size:
                        raise InputError("Artifact changed while hashing")
                    entries.append({"path": path.relative_to(root).as_posix(), "bytes": size,
                                    "sha256": hashlib.sha256(content).hexdigest()})
                else:
                    raise InputError("Artifact contains unsupported filesystem object")
    if not entries:
        raise InputError("Empty artifact")
    return digest(sorted(entries, key=lambda item: item["path"]))


def _positive_int(value):
    return type(value) is int and value > 0


def _nonnegative_number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def evaluate(manifest: dict, root: Path, receipts: list[dict], policy: dict) -> dict:
    if len(receipts) > 64:
        raise InputError("At most 64 receipts supported per screen")
    spec = protocol()
    protocol_hash = digest(spec)
    manifest_hash = digest(manifest)
    tree_hash = artifact_digest(root, spec)
    blockers = []
    profiles = manifest.get("profiles")
    if (not isinstance(profiles, list) or not profiles or
            any(not isinstance(p, str) or p not in spec["profiles"] for p in profiles) or
            len(set(profiles)) != len(profiles)):
        raise InputError("Nonempty, unique supported profiles required")
    candidate = manifest.get("candidate_id")
    if not isinstance(candidate, str) or not candidate.strip():
        raise InputError("candidate_id required")
    if (type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1 or
            type(policy.get("schema_version")) is not int or policy["schema_version"] != 1):
        raise InputError("Unsupported schema version")
    if policy.get("protocol_sha256") != protocol_hash:
        blockers.append("Trusted policy does not pin this protocol")
    provenance = manifest.get("provenance", {})
    if not (isinstance(provenance, dict) and provenance.get("source") and
            provenance.get("revision") and provenance.get("license_expression")):
        blockers.append("Source revision and license provenance required")
    budget = manifest.get("budgets", {})
    if not (isinstance(budget, dict) and _nonnegative_number(budget.get("max_cost_usd")) and
            _positive_int(budget.get("max_duration_seconds"))):
        blockers.append("Explicit finite cost and execution-duration budgets required")
    limits = policy.get("limits", {})
    if not (isinstance(limits, dict) and _nonnegative_number(limits.get("max_cost_usd")) and
            _positive_int(limits.get("max_duration_seconds"))):
        blockers.append("Operator budget limits required")
    elif (isinstance(budget, dict) and _nonnegative_number(budget.get("max_cost_usd")) and
          _positive_int(budget.get("max_duration_seconds")) and
          (budget["max_cost_usd"] > limits["max_cost_usd"] or
           budget["max_duration_seconds"] > limits["max_duration_seconds"])):
        blockers.append("Candidate budget exceeds operator limits")
    privacy = manifest.get("privacy", {})
    if not (isinstance(privacy, dict) and privacy.get("tenant_boundary") == "isolated" and
            privacy.get("raw_content_default") == "off" and
            privacy.get("telemetry_export") in ("off", "explicit_opt_in")):
        blockers.append("Explicit tenant isolation and opt-in content/export policy required")
    gates = sorted(set(spec["base_gates"]).union(*(spec["profiles"][p] for p in profiles)))
    reviewers = policy.get("reviewers", {})
    pins = policy.get("receipt_sha256", [])
    if not isinstance(reviewers, dict) or not isinstance(pins, list):
        raise InputError("Invalid trusted reviewer policy")
    allowed_gates = set(spec["base_gates"]).union(*(spec["profiles"].values()))
    if (len(pins) > 4096 or any(not isinstance(pin, str) or not re.fullmatch(r"[0-9a-f]{64}", pin) for pin in pins)
            or len(set(pins)) != len(pins)):
        raise InputError("Receipt pins must be unique lowercase SHA-256 strings")
    for reviewer_id, reviewer in reviewers.items():
        if (not isinstance(reviewer_id, str) or not reviewer_id.strip() or len(reviewer_id) > 128
                or not isinstance(reviewer, dict) or reviewer.get("kind") not in ("human", "automated")):
            raise InputError("Invalid reviewer identity or kind")
        scopes = reviewer.get("gates")
        if (not isinstance(scopes, list) or not scopes
                or any(not isinstance(scope, str) or scope not in allowed_gates for scope in scopes)
                or len(set(scopes)) != len(scopes)):
            raise InputError("Reviewer gates must be a unique list of exact supported gate names")
    grouped = {gate: [] for gate in gates}
    for receipt in receipts:
        if not isinstance(receipt, dict) or not isinstance(receipt.get("gate"), str) or not isinstance(receipt.get("reviewer_id"), str):
            raise InputError("Receipt gate and reviewer_id must be strings")
        gate = receipt.get("gate")
        if gate not in grouped:
            blockers.append("Receipt has unknown or inapplicable gate")
            continue
        grouped[gate].append(receipt)
    results = []
    for gate in gates:
        values = grouped[gate]
        status, reasons = "UNKNOWN", []
        if len(values) != 1:
            reasons.append("Exactly one receipt required")
        else:
            receipt = values[0]
            reviewer = reviewers.get(receipt.get("reviewer_id"), {})
            if (digest(receipt) not in pins or not isinstance(reviewer, dict) or
                    gate not in reviewer.get("gates", [])):
                reasons.append("Receipt is not independently pinned for an authorized reviewer")
            if (receipt.get("candidate_id") != candidate or receipt.get("artifact_sha256") != tree_hash or
                    receipt.get("manifest_sha256") != manifest_hash or
                    receipt.get("protocol_sha256") != protocol_hash or
                    type(receipt.get("schema_version")) is not int or receipt["schema_version"] != 1):
                reasons.append("Receipt identity, artifact, or protocol is stale")
            if gate in spec["human_gates"] and reviewer.get("kind") != "human":
                reasons.append("Human rights/maintainer/support review required")
            checks = receipt.get("checks", {})
            if not (isinstance(checks, dict) and _positive_int(checks.get("total")) and
                    type(checks.get("passed")) is int and checks["passed"] == checks["total"] and
                    type(checks.get("failed")) is int and checks["failed"] == 0 and
                    type(checks.get("skipped")) is int and checks["skipped"] == 0):
                reasons.append("Positive complete check count with no failures or skips required")
            refs = receipt.get("evidence_refs")
            if not isinstance(refs, list) or not refs or any(not isinstance(r, str) or not r.strip() for r in refs):
                reasons.append("Evidence references required")
            measurements = receipt.get("measurements", {})
            if not (isinstance(measurements, dict) and
                    _nonnegative_number(measurements.get("cost_usd")) and
                    _nonnegative_number(measurements.get("duration_seconds"))):
                reasons.append("Measured cost and duration required")
            elif isinstance(budget, dict) and _nonnegative_number(budget.get("max_cost_usd")) and _positive_int(budget.get("max_duration_seconds")):
                if measurements["cost_usd"] > budget["max_cost_usd"] or measurements["duration_seconds"] > budget["max_duration_seconds"]:
                    reasons.append("Receipt exceeds candidate budget")
            verdict = receipt.get("verdict")
            if verdict not in ("PASS", "FAIL", "UNKNOWN"):
                reasons.append("Unsupported verdict")
            status = "FAIL" if verdict == "FAIL" else "UNKNOWN" if reasons or verdict != "PASS" else "PASS"
        results.append({"gate": gate, "status": status, "reasons": reasons})
    eligible = not blockers and all(row["status"] == "PASS" for row in results)
    return {"schema_version": 1, "candidate_id": candidate, "artifact_sha256": tree_hash,
            "manifest_sha256": manifest_hash,
            "protocol_sha256": protocol_hash, "decision": "ELIGIBLE_FOR_REVIEW" if eligible else "BLOCKED",
            "blockers": blockers, "gates": results, "promotion_authorized": False}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--policy", type=Path)
    parser.add_argument("--receipt", type=Path, action="append", default=[])
    parser.add_argument("--fingerprint", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.fingerprint:
            result = {"artifact_sha256": artifact_digest(args.artifact_root), "protocol_sha256": digest(protocol())}
            if args.manifest is not None:
                result["manifest_sha256"] = digest(read_json(args.manifest))
            code = 0
        else:
            if args.manifest is None or args.policy is None:
                raise InputError("--manifest and --policy required for screening")
            # Manifest, receipts and policy are control-plane inputs; keep outside the immutable artifact.
            artifact = args.artifact_root.resolve()
            for path in [args.manifest, args.policy, *args.receipt]:
                if path.resolve().is_relative_to(artifact):
                    raise InputError("Control-plane inputs must be outside artifact root")
            result = evaluate(read_json(args.manifest), args.artifact_root,
                              [read_json(path) for path in args.receipt], read_json(args.policy))
            code = 0 if result["decision"] == "ELIGIBLE_FOR_REVIEW" else 1
    except (InputError, OSError, TypeError, ValueError) as exc:
        result, code = {"decision": "INPUT_ERROR", "error": str(exc), "promotion_authorized": False}, 2
    print(json.dumps(result, sort_keys=True, indent=2, allow_nan=False))
    return code
