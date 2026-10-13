"""Build a fresh, distributable 1.3.8 portable package from the live install.

The package carries no personal data: `data/` is replaced with a brand-new empty library
created by the shipped code itself. `SHA256.txt` is regenerated with the release's own
coverage rule. The zip stores names as UTF-8 so the Chinese file names survive.

Usage:  <live python> build_package.py
"""
import hashlib
import json
import shutil
import sys
import zipfile
from pathlib import Path

LIVE = Path(r"C:\Users\ASUS\OneDrive\Desktop\论文研读助手\1.3.8版本\论文研读助手-本地版-1.3.8-完整便携版")
DIST = Path(r"C:\Users\ASUS\OneDrive\Desktop\论文研读助手\论文研读助手-历史版本\开发工作区（原 D-harness home）\dist")
STAGE = DIST / "论文研读助手-本地版-1.3.8-完整便携版"
ZIP = DIST / "论文研读助手-本地版-1.3.8-完整便携版.zip"

EXCLUDE_DIRS = {"run", "__pycache__", "tests"}
report = {}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def stage_files():
    """Copy everything except the excluded folders and the personal data directory."""
    copied = 0
    for path in sorted(LIVE.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(LIVE).as_posix()
        parts = rel.split("/")
        if parts[0] in EXCLUDE_DIRS or any(part in EXCLUDE_DIRS for part in parts[:-1]):
            continue
        if parts[0] == "data":                      # replaced by a fresh library below
            continue
        if rel == "SHA256.txt":                     # regenerated below
            continue
        target = STAGE / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        copied += 1
    return copied


def fresh_library():
    """Create data/ with the shipped code, then keep only the library file."""
    sys.path.insert(0, str(STAGE))
    import server  # noqa: PLC0415 - imported from the staged tree on purpose

    server.Store(STAGE / "data")
    # Importing the shipped code writes byte-code caches; a release ships source only.
    for cache in [STAGE / "__pycache__", *STAGE.rglob("__pycache__")]:
        shutil.rmtree(cache, ignore_errors=True)
    keep = {"library.json"}
    removed = []
    for path in sorted((STAGE / "data").glob("*")):
        if path.is_file() and path.name not in keep:
            path.unlink()
            removed.append(path.name)
    state = json.loads((STAGE / "data" / "library.json").read_text(encoding="utf-8"))
    return {"papers": len(state.get("papers", [])),
            "provider": state["settings"]["ai"].get("provider"),
            "max_output_tokens": state["settings"]["ai"].get("max_output_tokens"),
            "auto_analyze": state["settings"]["ai"].get("auto_analyze"),
            "removed_extra_files": removed,
            "dirs": sorted(p.name for p in (STAGE / "data").iterdir() if p.is_dir())}


def regenerate_manifest():
    lines = []
    for path in sorted(STAGE.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(STAGE).as_posix()
        parts = rel.split("/")
        if parts[0] in EXCLUDE_DIRS or any(part in EXCLUDE_DIRS for part in parts[:-1]):
            continue
        if parts[0] == "data" and rel != "data/library.json":
            continue
        if rel == "SHA256.txt":
            continue
        lines.append(f"{digest(path)}  {rel}")
    (STAGE / "SHA256.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(lines)


def compare_with_live():
    """Every staged file must be byte-identical to the live one, except the two we own."""
    different, missing = [], []
    for path in sorted(STAGE.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(STAGE).as_posix()
        parts = rel.split("/")
        if parts[0] in EXCLUDE_DIRS or any(part in EXCLUDE_DIRS for part in parts[:-1]):
            continue
        if rel in ("SHA256.txt", "data/library.json"):
            continue
        counterpart = LIVE / rel
        if not counterpart.is_file():
            missing.append(rel)
        elif digest(counterpart) != digest(path):
            different.append(rel)
    return {"different": different, "missing": missing}


def make_zip():
    """One entry per file, no directory entries - the layout the 1.3.3 release used.

    An empty `data/papers` style directory therefore does not exist inside the archive; the
    application recreates its data directories on first start (verified by running the
    extracted package), so nothing is missing for the user.
    """
    if ZIP.exists():
        ZIP.unlink()
    written = 0
    with zipfile.ZipFile(ZIP, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(STAGE.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(STAGE).as_posix()
            archive.write(path, f"{STAGE.name}/{rel}")
            written += 1
    return written, ZIP.stat().st_size


if __name__ == "__main__":
    if STAGE.exists():
        shutil.rmtree(STAGE)
    STAGE.mkdir(parents=True)
    report["copied_files"] = stage_files()
    report["fresh_library"] = fresh_library()
    report["manifest_entries"] = regenerate_manifest()
    report["compare_with_live"] = compare_with_live()
    report["staged_bytes"] = sum(p.stat().st_size for p in STAGE.rglob("*") if p.is_file())
    report["zip_files"], report["zip_bytes"] = make_zip()
    report["stage"] = str(STAGE)
    report["zip"] = str(ZIP)
    print(json.dumps(report, ensure_ascii=False, indent=2))
