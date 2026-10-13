"""Stage 0: structural recon of the three bundled PDFs. Read-only."""
from __future__ import annotations

import json
import re
from pathlib import Path

DATA = Path(r"D:\work\常用软件\论文研读助手-本地版\论文研读助手\data")
PDF_DIR = DATA / "papers"

rows = []
for pdf in sorted(PDF_DIR.glob("*.pdf")):
    blob = pdf.read_bytes()
    tail = blob[-2048:]
    page_objs = len(re.findall(rb"/Type\s*/Page[^s]", blob))
    counts = [int(x) for x in re.findall(rb"/Count\s+(\d+)", blob)]
    kids = len(re.findall(rb"/Kids\s*\[", blob))
    linearized = b"/Linearized" in blob[:2048]
    rows.append({
        "file": pdf.name,
        "MB": round(len(blob) / 1048576, 2),
        "pdf_version": (re.search(rb"%PDF-(\d\.\d)", blob[:1024]) or [None, b"?"])[1].decode(),
        "encrypted": b"/Encrypt" in tail or b"/Encrypt" in blob[:4096],
        "has_xref": b"startxref" in tail,
        "linearized": linearized,
        "page_objects": page_objs,
        "max_count": max(counts) if counts else None,
        "count_tokens": sorted(set(counts))[:6],
        "kids_arrays": kids,
        "objstm": b"/ObjStm" in blob,
        "has_images_jpx": b"/JPXDecode" in blob,
        "has_jbig2": b"/JBIG2Decode" in blob,
        "text_operators": blob.count(b"Tj") + blob.count(b"TJ"),
    })

lib = json.loads((DATA / "library.json").read_text(encoding="utf-8"))
for p in lib["papers"]:
    for r in rows:
        if r["file"] == p["id"] + ".pdf":
            r["library_pageLabels"] = len(p.get("pageLabels", []))
            r["declared_lastPage"] = p.get("lastPage")
            r["pageOffset"] = p.get("pageOffset")
            r["type"] = p["type"]
            r["guideKey"] = p.get("guideKey")
print(json.dumps(rows, ensure_ascii=False, indent=2))
