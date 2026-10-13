"""Stage 0: how small will text become under fit-page? (stdlib, read-only)"""
from __future__ import annotations

import json
import re
from pathlib import Path

PDF_DIR = Path(r"D:\work\常用软件\论文研读助手-本地版\论文研读助手\data\papers")
# viewport-dependent fit-page scale measured in baseline-layout.json
FIT_PAGE_SCALE = {
    "1280x800": 0.585,
    "1440x900": 0.652,
    "1600x1000": 0.714,
    "1920x1080": 0.882,
}
rows = []
for pdf in sorted(PDF_DIR.glob("*.pdf")):
    blob = pdf.read_bytes()
    sizes = sorted({float(x) for x in re.findall(rb"/Tf\s*\n?\s*([\d.]+)\s+", b"")} or set())
    # font size operands appear as "<size> Tf" in content streams (may be compressed)
    tf_literal = sorted({float(x) for x in re.findall(rb"([\d.]+)\s+Tf", blob)})
    rows.append({
        "file": pdf.name,
        "Tf_sizes_plaintext": tf_literal[:20],
        "compressed_streams": "content streams are FlateDecode; Tf sizes usually not visible in plaintext",
        "estimated_body_pt": 10.5 if "medical" in pdf.name else (12 if "fertility" in pdf.name else 10),
    })
out = {"fonts": rows, "fit_page_scale": FIT_PAGE_SCALE, "effective_body_px": {}}
for name, sc in FIT_PAGE_SCALE.items():
    out["effective_body_px"][name] = {
        "zh_body_10.5pt": round(10.5 * sc / 72 * 96, 1),
        "en_body_12pt": round(12 * sc / 72 * 96, 1),
        "fit_page_area_vs_fit_width": f"{sc:.0%}",
    }
# aspect ratio of the pages (fit-page limited by height for A4-ish pages)
out["note"] = ("fit-page scale is driven by whichever of width/height binds first; for portrait A4 the "
               "height binds, so the effective scale is roughly available_height / page_height.")
print(json.dumps(out, ensure_ascii=False, indent=2))
