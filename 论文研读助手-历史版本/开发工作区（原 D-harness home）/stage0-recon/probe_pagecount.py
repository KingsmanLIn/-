"""Stage 0: authoritative page count / page size per bundled PDF (stdlib only).

Strategy: decode every xref table (following /Prev), expand every /ObjStm, build a
full in-memory object map, then walk the /Root -> /Pages -> /Kids tree.
"""
from __future__ import annotations

import json
import re
import zlib
from pathlib import Path

PDF_DIR = Path(r"D:\work\常用软件\论文研读助手-本地版\论文研读助手\data\papers")


def dict_extent(blob: bytes, at: int) -> bytes:
    depth, i = 0, at
    while i < len(blob) - 1:
        two = blob[i:i + 2]
        if two == b"<<":
            depth += 1
            i += 2
            continue
        if two == b">>":
            depth -= 1
            i += 2
            if depth == 0:
                return blob[at:i]
            continue
        i += 1
    return blob[at:at + 4000]


def stream_bytes(blob: bytes, dict_at: int):
    sm = re.compile(rb"stream\r?\n").search(blob, dict_at, dict_at + 4096)
    if not sm:
        return None
    start = sm.end()
    # /Length may be a direct integer
    d = blob[dict_at:sm.start()]
    lm = re.search(rb"/Length\s+(\d+)(?!\s+\d+\s+R)", d)
    if lm:
        return blob[start:start + int(lm.group(1))]
    end = blob.find(b"endstream", start)
    return blob[start:end if end > 0 else len(blob)]


def collect_objects(blob: bytes):
    """object number -> dict bytes (best effort, first definition wins)."""
    objs: dict[int, bytes] = {}
    # 1. classic xref tables, following /Prev chains
    offsets = []
    for m in re.finditer(rb"startxref\s+(\d+)", blob):
        offsets.append(int(m.group(1)))
    seen_x = set()
    while offsets:
        off = offsets.pop(0)
        if off in seen_x or off >= len(blob):
            continue
        seen_x.add(off)
        if blob[off:off + 4] == b"xref":
            head_end = blob.find(b"trailer", off)
            head = blob[off:head_end if head_end > 0 else off + 8192]
            for blk in re.finditer(rb"(\d+)\s+(\d+)\s*\n((?:\d{10}\s+\d{5}\s+[nf]\s*(?:\r?\n|$))+)", head):
                first, count = int(blk.group(1)), int(blk.group(2))
                for i, line in enumerate(blk.group(3).split()):
                    pass
                lines = re.findall(rb"(\d{10})\s+(\d{5})\s+([nf])", blk.group(3))
                for i, (o, _g, t) in enumerate(lines[:count]):
                    if t == b"n":
                        objs.setdefault(first + i, b"@" + o)  # marker: byte offset
            if head_end > 0:
                tm = re.search(rb"trailer", blob[head_end:head_end + 8192])
                if tm:
                    tstart = blob.find(b"<<", head_end + tm.end())
                    td = dict_extent(blob, tstart) if tstart > 0 else b""
                    for key in (b"/XRefStm", b"/Prev"):
                        km = re.search(key + rb"\s+(\d+)", td)
                        if km:
                            offsets.append(int(km.group(1)))
        elif re.match(rb"\s*\d+\s+\d+\s+obj", blob[off:off + 40]):
            num = int(re.match(rb"\s*(\d+)", blob[off:]).group(1))
            dstart = blob.find(b"<<", off)
            objs.setdefault(num, blob[dstart:off + 8192])
    # 2. expand object streams
    for num, body in list(objs.items()):
        if body.startswith(b"@"):
            continue
        if b"/ObjStm" not in body[:200]:
            continue
        raw = stream_bytes(blob, blob.find(body))
        if raw is None:
            continue
        # filter: only FlateDecode is used in these files
        try:
            data = zlib.decompress(raw)
        except zlib.error:
            continue
        n = int((re.search(rb"/N\s+(\d+)", body) or [None, b"0"])[1])
        first = int((re.search(rb"/First\s+(\d+)", body) or [None, b"0"])[1])
        header = data[:first].split()
        for i in range(n):
            try:
                onum, rel = int(header[2 * i]), int(header[2 * i + 1])
                end = int(header[2 * i + 3]) if 2 * i + 3 < len(header) else len(data) - first
            except (IndexError, ValueError):
                break
            objs.setdefault(onum, data[first + rel:first + end])
    # 3. resolve offsets recorded as "@<offset>"
    for num, body in list(objs.items()):
        if body.startswith(b"@"):
            off = int(body[1:])
            dstart = blob.find(b"<<", off)
            if dstart > 0:
                objs[num] = blob[dstart:dstart + 8192]
            else:
                objs.pop(num, None)
    return {k: v for k, v in objs.items() if v.startswith(b"<<")}


def walk_pages(objs: dict[int, bytes]):
    root = None
    for num, body in objs.items():
        if b"/Type" in body and re.search(rb"/Type\s*/Catalog", body):
            root = num
            break
    if root is None:
        return None, [], []
    kids = re.search(rb"/Pages\s+(\d+)\s+\d+\s+R", objs[root])
    if not kids:
        return root, [], []
    stack = [int(kids.group(1))]
    pages, counts = [], []
    guard = 0
    while stack and guard < 10000:
        guard += 1
        num = stack.pop(0)
        body = objs.get(num, b"")
        if re.search(rb"/Type\s*/Pages", body):
            c = re.search(rb"/Count\s+(\d+)", body)
            if c:
                counts.append(int(c.group(1)))
            km = re.search(rb"/Kids\s*\[([^\]]*)\]", body, re.S)
            if km:
                stack = [int(x) for x in re.findall(rb"(\d+)\s+\d+\s+R", km.group(1))] + stack
        elif re.search(rb"/Type\s*/Page[^s]", body):
            pages.append(num)
    return root, pages, counts


rows = []
for pdf in sorted(PDF_DIR.glob("*.pdf")):
    blob = pdf.read_bytes()
    objs = collect_objects(blob)
    root, pages, counts = walk_pages(objs)
    boxes = []
    for num in pages:
        mb = re.search(rb"/MediaBox\s*\[\s*([\d.\-]+)\s+([\d.\-]+)\s+([\d.\-]+)\s+([\d.\-]+)", objs[num])
        if mb:
            x0, y0, x1, y1 = (float(v) for v in mb.groups())
            boxes.append((round(x1 - x0, 1), round(y1 - y0, 1)))
    rows.append({
        "file": pdf.name,
        "objects_parsed": len(objs),
        "catalog_obj": root,
        "page_count": len(pages),
        "page_tree_count_tokens": counts,
        "page_sizes_pt": sorted(set(boxes)),
        "aspect_ratio": round(boxes[0][1] / boxes[0][0], 3) if boxes else None,
    })
print(json.dumps(rows, ensure_ascii=False, indent=2))
