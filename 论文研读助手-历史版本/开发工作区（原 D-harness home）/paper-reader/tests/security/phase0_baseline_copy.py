"""Phase 0 rollback point: snapshot the code, never credential-bearing material.

The baseline copy must be sufficient to restore the application while guaranteeing that no
plaintext credential, environment dump, startup log or diagnostic failure file is copied
into the development backup.

    python tests/security/phase0_baseline_copy.py
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

WORKING = Path(r"D:\harness home\paper-reader")
BASE_DIR = Path(r"D:\harness home\backups")
SHIPPED = ["server.py", "launch.py", "schema.py", "storage.py", "pdftext.py", "textclean.py",
           "chunker.py", "extraction.py", "providers.py", "analysis.py", "reading_card.py",
           "quotes.py", "使用说明.md", "启动论文研读助手.vbs", "备用启动.cmd"]
SHIPPED_DIRS = ["static", "vendor"]
# never copy these into a development artifact
NEVER_COPY = {"credentials.json", "library.previous.json", "startup.log", "startup.old.log",
              "instance.lock", "server.json"}

stamp = datetime.now(timezone.utc).astimezone().strftime("%Y%m%d-%H%M%S")
target = BASE_DIR / f"sec-phase0-src-{stamp}"
report = {"created": str(target), "files": [], "skipped": [], "secret_hits": {}}


def sha256(path: Path, length=12):
    return hashlib.sha256(path.read_bytes()).hexdigest()[:length].upper()


def copy_file(source: Path, destination: Path):
    if source.name in NEVER_COPY:
        report["skipped"].append(str(source.relative_to(WORKING)))
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    report["files"].append({"path": str(destination.relative_to(target)),
                            "sha256": sha256(source), "bytes": source.stat().st_size})


target.mkdir(parents=True, exist_ok=True)
for name in SHIPPED:
    source = WORKING / name
    if source.is_file():
        copy_file(source, target / name)
for folder in SHIPPED_DIRS:
    for item in (WORKING / folder).rglob("*"):
        if item.is_file() and "__pycache__" not in item.parts:
            copy_file(item, target / item.relative_to(WORKING))
# the library is user data, not code: the baseline keeps a schema shape only
library = WORKING / "data" / "library.json"
if library.is_file():
    payload = json.loads(library.read_text(encoding="utf-8"))
    shape = {"version": payload.get("version"),
             "papers": len(payload.get("papers", [])),
             "settings": sorted(payload.get("settings", {})),
             "note": "结构摘要，不是数据副本；人工记录未复制到开发备份"}
    (target / "library-shape.json").write_text(json.dumps(shape, ensure_ascii=False, indent=2),
                                               encoding="utf-8")

# prove the snapshot carries no key-shaped material
PATTERNS = {"sk_key": re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
            "bearer": re.compile(r"(?i)bearer\s+[A-Za-z0-9_\-\.]{8,}"),
            "assignment": re.compile(r"(?i)(api[_-]?key|secret|password)\s*[:=]\s*[\"'][^\"']{8,}")}
for item in target.rglob("*"):
    if not item.is_file():
        continue
    try:
        text = item.read_text(encoding="utf-8", errors="replace")
    except OSError:
        continue
    for name, pattern in PATTERNS.items():
        if pattern.search(text):
            report["secret_hits"].setdefault(name, []).append(str(item.relative_to(target)))

report["total_bytes"] = sum(item["bytes"] for item in report["files"])
(target / "BASELINE.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
print(f"rollback point : {target}")
print(f"files copied   : {len(report['files'])} ({report['total_bytes']:,} bytes)")
print(f"skipped        : {report['skipped'] or 'none'}")
print(f"secret scan    : {report['secret_hits'] or 'clean'}")
