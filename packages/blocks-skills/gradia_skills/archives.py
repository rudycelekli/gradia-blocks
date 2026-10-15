"""Immutable edition archives; changed contents require a new edition."""
from pathlib import Path
import zipfile


def write_archive(source: Path, target: Path, paths: list[Path], prefix: str) -> bool:
    desired = {}
    for path in paths:
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(source.resolve()):
            raise ValueError('Unsafe archive source')
        name = prefix + '/' + path.relative_to(source).as_posix()
        desired[name] = path.read_bytes()
    if target.exists():
        with zipfile.ZipFile(target) as archive:
            if len(archive.namelist()) != len(set(archive.namelist())) or set(archive.namelist()) != set(desired) or any(archive.read(name) != data for name, data in desired.items()):
                raise ValueError('Existing archive edition changed; increment the package version: ' + target.name)
        return False
    target.parent.mkdir(exist_ok=True, parents=True)
    # Fixed metadata gives reproducible bytes across machines and build dates.
    with zipfile.ZipFile(target, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(desired.items()):
            item = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            item.compress_type = zipfile.ZIP_DEFLATED
            item.external_attr = 0o100644 << 16
            archive.writestr(item, data)
    return True


def exportable_skill_file(relative: str, candidate_status: dict[str, bool]) -> bool:
    """Positive export policy; candidate ownership is independent of folder name."""
    matches = [allowed for prefix, allowed in candidate_status.items() if relative == prefix or relative.startswith(prefix + '/')]
    if matches:
        return all(matches)
    path = Path(relative)
    return relative in {'README.md', 'LICENSE', 'NOTICE', 'block.manifest.json', 'THIRD-PARTY-NOTICES.md'} or path.parts[0] in {'gradia_skills', 'skills', 'sources', 'tests'}
