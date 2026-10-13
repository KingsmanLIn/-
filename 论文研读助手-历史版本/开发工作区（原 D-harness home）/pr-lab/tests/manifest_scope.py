"""Work out the exact file set SHA256.txt covers, so it can be regenerated faithfully."""
import sys
from pathlib import Path

root = Path(sys.argv[1])
manifest = Path(sys.argv[2])
listed = []
for line in manifest.read_text(encoding="utf-8").splitlines():
    if not line.strip():
        continue
    parts = line.split("  ", 1)
    if len(parts) == 2:
        listed.append(parts[1].strip())

EXCLUDE_DIRS = {"run", "__pycache__", "tests"}
candidates = []
for path in sorted(root.rglob("*")):
    if not path.is_file():
        continue
    rel = path.relative_to(root).as_posix()
    parts = rel.split("/")
    if any(part in EXCLUDE_DIRS for part in parts[:-1]) or parts[0] in EXCLUDE_DIRS:
        continue
    if parts[0] == "data" and rel != "data/library.json":
        continue
    candidates.append(rel)

only_manifest = sorted(set(listed) - set(candidates))
only_disk = sorted(set(candidates) - set(listed))
print("manifest entries:", len(listed))
print("candidate files :", len(candidates))
print("in manifest only:", only_manifest)
print("on disk only    :", only_disk)
