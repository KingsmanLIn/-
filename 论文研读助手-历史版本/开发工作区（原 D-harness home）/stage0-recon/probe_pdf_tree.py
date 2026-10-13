"""Stage 0: reliable page count + MediaBox + text-op viability per bundled PDF.

Pure standard library. Read-only. Parses the *last* startxref xref (the one viewers
actually use), expands /ObjStm object streams, then walks the page tree.
"""
from __future__ import annotations

import json
import re
import zlib
from pathlib import Path

PDF_DIR = Path(r"D:\work\常用软件\论文研读助手-本地版\论文研读助手\data\papers")


def parse_xref(blob: bytes):
    """Return {objnum: (offset, gen)} plus the trailer dict text."""
    idx = blob.rfind(b"startxref")
    if idx < 0:
        return {}, b""
    m = re.search(rb"startxref\s+(\d+)", blob[idx:])
    if not m:
        return {}, b""
    start = int(m.group(1))
    objects: dict[int, tuple[int, int]] = {}
    trailer = {}
    seen = set()
    queue = [start]
    while queue:
        off = queue.pop(0)
        if off in seen or not (0 <= off < len(blob)):
            continue
        seen.add(off)
        if blob[off:off + 4] == b"xref":
            end = blob.find(b"trailer", off)
            head = blob[off:end if end > 0 else off + 4096]
            for block in re.finditer(rb"(\d+)\s+(\d+)\s*\n((?:\d{10}\s+\d{5}\s+[nf]\s*\n?)+)", head):
                first, count = int(block.group(1)), int(block.group(2))
                for i, line in enumerate(block.group(3).splitlines()):
                    parts = line.split()
                    if len(parts) == 3 and parts[2] == b"n":
                        objects[first + i] = (int(parts[0]), int(parts[1]))
            if end > 0:
                tm = re.search(rb"trailer\s*<<", blob[end:end + 4096])
                if tm:
                    trailer = read_dict(blob, end + tm.end() - 2)
                    if b"/XRefStm" in blob[end:end + 4096]:
                        xs = re.search(rb"/XRefStm\s+(\d+)", blob[end:end + 4096])
                        if xs:
                            queue.append(int(xs.group(1)))
        else:
            m2 = re.match(rb"\s*(\d+)\s+(\d+)\s+obj", blob[off:off + 40])
            if not m2:
                continue
            d = read_dict(blob, blob.find(b"<<", off))
            trailer = d
            if isinstance(d.get("/XRefStm"), int):
                queue.append(d["/XRefStm"])
            if isinstance(d.get("/Prev"), int):
                queue.append(d["/Prev"])
        for key in (b"/Prev",):
            pm = re.search(key + rb"\s+(\d+)", blob[max(0, off - 4096):off + 8192])
    return objects, trailer


def read_dict(blob: bytes, at: int):
    """Very small PDF dict scanner: returns top-level keys with int/name/ref values."""
    if at < 0 or blob[at:at + 2] != b"<<":
        return {}
    depth, i = 0, at
    while i < len(blob):
        if blob[i:i + 2] == b"<<":
            depth += 1
            i += 2
            continue
        if blob[i:i + 2] == b">>":
            depth -= 1
            i += 2
            if depth == 0:
                break
            continue
        i += 1
    body = blob[at:i]
    out = {}
    for m in re.finditer(rb"/([A-Za-z0-9]+)\s+(\d+\s+\d+\s+R|\[[^\]]{0,4000}\]|[^\s/<>\[\]]+)", body):
        key = "/" + m.group(1).decode()
        raw = m.group(2).strip()
        if raw.endswith(b"R"):
            parts = raw.split()
            out[key] = ("ref", int(parts[0]))
        elif raw.startswith(b"["):
            out[key] = [int(x) for x in re.findall(rb"(\d+)\s+\d+\s+R", raw)]
        else:
            out[key] = raw.decode("latin-1")
    return out


def inflate_objstm(blob: bytes, offset: int):
    m = re.match(rb"\s*(\d+)\s+(\d+)\s+obj", blob[offset:offset + 40])
    if not m:
        return {}
    d = read_dict(blob, blob.find(b"<<", offset))
    if d.get("/Type") != "/ObjStm":
        return {}
    n = int(d.get("/N", "0"))
    first = int(d.get("/First", "0"))
    sm = re.search(rb"stream\r?\n", blob[offset:])
    if not sm:
        return {}
    start = offset + sm.end()
    raw = blob[start:blob.find(b"endstream", start)]
    try:
        data = zlib.decompress(raw)
    except zlib.error:
        try:
            data = zlib.decompressobj().decompress(raw)
        except zlib.error:
            return {}
    header = data[:first].split()
    out = {}
    for i in range(n):
        try:
            num, rel = int(header[2 * i]), int(header[2 * i + 1])
        except (IndexError, ValueError):
            break
        end = int(header[2 * i + 3]) + first if 2 * i + 3 < len(header) else len(data)
        out[num] = data[first + rel:end]
    return out


def page_count(blob: bytes):
    objects, trailer = parse_xref(blob)
    store: dict[int, bytes] = {}
    for num, (off, _gen) in objects.items():
        if blob[off:off + 2] == b"<<":
            store[num] = blob[off:off + 4000]
    for num, (_off, _gen) in objects.items():
        expanded = inflate_objstm(blob, _off)
        for k, v in expanded.items():
            store.setdefault(k, v)
    # find /Type /Pages nodes and /Type /Page leaves
    pages, pages_nodes, counts = [], [], []
    for num, body in store.items():
        if re.search(rb"/Type\s*/Pages", body):
            pages_nodes.append(num)
            c = re.search(rb"/Count\s+(\d+)", body)
            if c:
                counts.append(int(c.group(1)))
        elif re.search(rb"/Type\s*/Page[^s]", body):
            pages.append(num)
    root = None
    rm = re.search(rb"/Root\s+(\d+)\s+\d+\s+R", blob[blob.rfind(b"trailer"):] if b"trailer" in blob else blob[-4096:])
    if rm:
        root = int(rm.group(1))
    catalog = store.get(root, b"")
    ptreem = re.search(rb"/Pages\s+(\d+)\s+\d+\s+R", catalog)
    # MediaBox values across page leaves
    boxes = []
    for num in pages:
        mb = re.search(rb"/MediaBox\s*\[\s*([\d.\-]+)\s+([\d.\-]+)\s+([\d.\-]+)\s+([\d.\-]+)", store[num])
        if mb:
            boxes.append(tuple(round(float(x), 1) for x in mb.groups()))
    return {
        "xref_last_start": blob.rfind(b"startxref"),
        "xref_style": "table" if re.search(rb"startxref\s+\d+\s*%?\s*$", blob.rstrip()[-40:]) is None else "table",
        "objstm_expanded": bool(any(inflate_objstm(blob, o) for _n, (o, _g) in objects.items())),
        "trailer_keys": sorted(trailer.keys()),
        "root_obj": root,
        "page_tree_root": int(ptreem.group(1)) if ptreem else None,
        "pages_nodes": len(pages_nodes),
        "leaf_pages": len(pages),
        "count_values": counts,
        "mediabox_sample": boxes[:3],
        "mediabox_distinct": len(set(boxes)),
    }


rows = []
for pdf in sorted(PDF_DIR.glob("*.pdf")):
    blob = pdf.read_bytes()
    info = {"file": pdf.name}
    info.update(page_count(blob))
    # text-extraction viability: are content streams Flate-compressed, and is there a ToUnicode CMap?
    info["ToUnicode_cmaps"] = len(re.findall(rb"/ToUnicode", blob))
    info["FlateDecode"] = len(re.findall(rb"/FlateDecode", blob))
    info["Font_subtypes"] = sorted({m.decode() for m in re.findall(rb"/Subtype\s*/(Type0|Type1|TrueType|CIDFontType0|CIDFontType2|Type3)", blob)})
    rows.append(info)
print(json.dumps(rows, ensure_ascii=False, indent=2))
