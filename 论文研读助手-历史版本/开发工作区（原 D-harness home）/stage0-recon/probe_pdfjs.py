"""Stage 0: inspect the bundled PDF.js build (read-only)."""
import re
import sys
from pathlib import Path

p = Path(sys.argv[1])
t = p.read_text(encoding="utf-8", errors="replace")
print("bytes:", p.stat().st_size)
m = re.search(r"export\s*\{(.*?)\}\s*;?\s*$", t, re.S)
print("export block:", (m.group(1)[:900] if m else "NOT FOUND"))
print("---scale-factor occurrences:", t.count("--scale-factor"))
print("total-scale-factor occurrences:", t.count("--total-scale-factor"))
cands = sorted(set(re.findall(r'(?:version|build)["\']?\s*[:=]\s*["\']([\d]+\.[\d.]+)', t)))[:10]
print("version-ish strings:", cands)
print("has getDocument:", "getDocument" in t, "| has TextLayer:", "TextLayer" in t)
csp_relevant = [k for k in ("eval(", "new Function", "WebAssembly", "createObjectURL", "SharedArrayBuffer") if k in t]
print("csp-relevant tokens present:", csp_relevant)
