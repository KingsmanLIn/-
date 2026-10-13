"""Summarise the curl smoke report written by tests/api_smoke.cmd."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

path = Path(sys.argv[1] if len(sys.argv) > 1 else "phase5-curl-report.txt")
text = path.read_text(encoding="utf-8", errors="replace")
rows = []
for block in re.split(r"\n## ", text)[1:]:
    lines = block.split("\n")
    title = lines[0].strip()
    body = "\n".join(lines[1:]).strip()
    match = re.search(r"HTTP (\d+) in ([\d.]+)s", body)
    status = match.group(1) if match else "?"
    seconds = match.group(2) if match else "?"
    payload = body.split("\n", 1)[1] if "\n" in body else ""
    summary = payload[:110].replace("\n", " ")
    rows.append({"case": title, "http": status, "seconds": seconds, "body": summary})

print(f"{'case':40s} {'HTTP':5s} {'time':9s} body")
for row in rows:
    print(f"{row['case']:40s} {row['http']:5s} {row['seconds']+'s':9s} {row['body']}")
out = Path(path).with_name("phase5-curl-summary.json")
out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
print("\nwritten:", out)
