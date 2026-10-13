"""Phase 0 baseline: verify the release ZIP and extract it defensively.

Safety rules enforced here (this script is part of the security baseline):
* the archive is never opened for writing;
* every member is validated before extraction: no absolute paths, no drive letters,
  no parent traversal, no symlinks/links, sane member count and expanded size;
* nothing from the archive is executed: no install scripts, no .vbs/.cmd run, no imports
  from the extracted tree;
* the extracted copy is read-only inspection material.

    python tests/security/phase0_extract.py
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import stat
import zipfile
from pathlib import Path

DEFAULT_ZIP = Path(r"D:\harness home\releases\论文研读助手-本地版-1.3.1-全新安装.zip")
EXPECTED_SHA256 = "2EFF14507859B8DB51537B4D9EF301766DEB5EABBB352B20FEB297A1B718D5D5"
DEFAULT_OUT = Path(r"D:\harness home\paper-reader-sec\baseline-1.3.1")
MAX_MEMBERS = 5000
MAX_TOTAL_BYTES = 500 * 1024 * 1024
MAX_SINGLE_BYTES = 200 * 1024 * 1024


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def unsafe_reason(name: str):
    """Return a reason string when a member name must not be extracted."""
    if not name or name.endswith("/"):
        return None if name else "empty name"
    if "\\" in name:
        return "backslash in name"
    if name.startswith("/") or name.startswith("\\"):
        return "absolute path"
    if len(name) > 2 and name[1] == ":":
        return "drive letter"
    parts = name.split("/")
    if any(part in ("..", "") for part in parts):
        return "parent traversal or empty segment"
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--zip", default=str(DEFAULT_ZIP))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--expected", default=EXPECTED_SHA256)
    args = parser.parse_args()

    archive_path = Path(args.zip)
    out = Path(args.out)
    report = {"archive": str(archive_path), "expected_sha256": args.expected}

    if not archive_path.is_file():
        raise SystemExit(f"archive not found: {archive_path}")
    actual = sha256_of(archive_path)
    report["actual_sha256"] = actual
    report["hash_match"] = actual == args.expected.upper()
    print(f"archive      : {archive_path}")
    print(f"size         : {archive_path.stat().st_size:,} bytes")
    print(f"sha256       : {actual}")
    print(f"hash match   : {report['hash_match']}")

    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)
    root = out / "tree"

    violations, links, total, extracted = [], [], 0, 0
    with zipfile.ZipFile(archive_path) as archive:
        members = archive.infolist()
        report["members"] = len(members)
        if len(members) > MAX_MEMBERS:
            raise SystemExit(f"member count {len(members)} > {MAX_MEMBERS}")
        for info in members:
            reason = unsafe_reason(info.filename)
            if reason:
                violations.append({"name": info.filename[:120], "reason": reason})
                continue
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                links.append(info.filename[:120])
                continue
            if info.file_size > MAX_SINGLE_BYTES:
                violations.append({"name": info.filename[:120], "reason": "single member too large"})
                continue
            total += info.file_size
            if total > MAX_TOTAL_BYTES:
                raise SystemExit(f"expanded size exceeds {MAX_TOTAL_BYTES} bytes")
            target = root / info.filename
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open("wb") as sink:
                shutil.copyfileobj(source, sink)
            extracted += 1

    report["extracted_members"] = extracted
    report["expanded_bytes"] = total
    report["violations"] = violations
    report["symlinks_skipped"] = links
    top = sorted({item.name for item in root.iterdir()}) if root.exists() else []
    report["top_level"] = top
    print(f"extracted    : {extracted} members, {total:,} bytes")
    print(f"violations   : {violations if violations else 'none'}")
    print(f"symlinks     : {links if links else 'none'}")
    print(f"top level    : {top}")

    # inventory without reading any content
    files = sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())
    groups = {}
    for name in files:
        key = name.split("/")[0]
        groups[key] = groups.get(key, 0) + 1
    report["file_groups"] = groups
    report["files"] = files
    (out / "phase0-extract-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("report       :", out / "phase0-extract-report.json")
    print("group counts :", json.dumps(groups, ensure_ascii=False))


if __name__ == "__main__":
    main()
