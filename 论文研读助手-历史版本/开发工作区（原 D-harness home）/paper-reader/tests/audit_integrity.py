"""Confirm the audit run changed no product file (compare against docs/文件清单.md)."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
text = (ROOT / "docs" / "文件清单.md").read_text(encoding="utf-8")
section = text.split("## 新增测试")[0]
rows = re.findall(r"\| `([^`]+)` \| (\d+) \| ([^|]*)\| `([0-9A-F]{12})` \|", section)
print("recorded product files:", len(rows))
mismatch = []
for name, lines, phase, expected in rows:
    path = ROOT / name
    if not path.is_file():
        mismatch.append((name, "missing", expected))
        continue
    actual = hashlib.sha256(path.read_bytes()).hexdigest()[:12].upper()
    if actual != expected:
        mismatch.append((name, actual, expected))
print("hash mismatches:", mismatch if mismatch else "none")
print("live install untouched:",
      hashlib.sha256(Path(r"D:\work\常用软件\论文研读助手-本地版\论文研读助手\data\library.json")
                     .read_bytes()).hexdigest()[:12] == "3436614b83ff")
