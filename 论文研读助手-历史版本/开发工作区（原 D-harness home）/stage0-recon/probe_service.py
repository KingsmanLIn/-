"""Stage 0 read-only reconnaissance of the running paper-reader service.

Only GETs. No writes to library.json or PDFs. Standard library only.
"""
from __future__ import annotations

import json
import re
import sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen

BASE = "http://127.0.0.1:8765"


def get(path, token=None, raw=False):
    req = Request(BASE + path)
    if token:
        req.add_header("X-Reader-Token", token)
    with urlopen(req, timeout=15) as r:
        data = r.read()
        return r.status, dict(r.headers), (data if raw else data.decode("utf-8"))


def main():
    out = {}
    status, _, html = get("/")
    m = re.search(r'<script id="boot" type="application/json">(.*?)</script>', html, re.S)
    boot = json.loads(m.group(1))
    out["bootstrapped_ok"] = bool(boot.get("token"))
    out["boot_keys"] = sorted(boot.keys())
    token = boot["token"]

    status, _, health = get("/api/health")
    out["health"] = json.loads(health)

    # unauthorized access must be refused
    try:
        get("/api/state")
        out["state_without_token"] = "ALLOWED (unexpected)"
    except HTTPError as e:
        out["state_without_token"] = f"{e.code} {e.read().decode('utf-8')}"

    status, headers, state = get("/api/state", token)
    st = json.loads(state)
    out["library_version"] = st.get("version")
    out["settings_keys"] = sorted(st.get("settings", {}).keys())
    out["paper_count"] = len(st["papers"])

    papers = []
    for p in st["papers"]:
        papers.append({
            "id": p["id"],
            "title": p["title"][:28],
            "language": p["language"],
            "type": p["type"],
            "guideKey": p.get("guideKey"),
            "pageLabels": len(p.get("pageLabels", [])),
            "lastPage": p.get("lastPage"),
            "revision": p.get("revision"),
            "annotations": len(p.get("annotations", [])),
            "steps": p.get("steps"),
            "empty_fields": sum(1 for v in p.get("fields", {}).values() if not v.strip()),
            "extra_keys": sorted(set(p) - {
                "id", "title", "authors", "year", "venue", "doi", "citation", "tags", "language",
                "type", "pageOffset", "pageLabels", "guideKey", "fields", "steps", "annotations",
                "lastPage", "revision", "created", "updated", "archived", "note_name"}),
        })
    out["papers"] = papers

    # PDF transport behaviour that matters for PDF.js (range support, mime, size)
    if st["papers"]:
        pid = st["papers"][0]["id"]
        status, headers, blob = get(f"/api/pdf/{pid}", token, raw=True)
        out["pdf_probe"] = {
            "id": pid,
            "status": status,
            "content_type": headers.get("Content-Type"),
            "accept_ranges": headers.get("Accept-Ranges"),
            "cache_control": headers.get("Cache-Control"),
            "bytes": len(blob),
            "has_eof": blob.rstrip()[-5:] == b"%%EOF",
        }
        # lightweight page-tree estimate from the raw PDF
        counts = re.findall(rb"/Type\s*/Page[^s]", blob)
        out["pdf_probe"]["page_objects"] = len(counts)
        m = re.search(rb"/Count\s+(\d+)", blob)
        out["pdf_probe"]["first_count_token"] = int(m.group(1)) if m else None
        ver = re.search(rb"%PDF-(\d\.\d)", blob[:1024])
        out["pdf_probe"]["pdf_version"] = ver.group(1).decode() if ver else None
        out["pdf_probe"]["encrypt"] = b"/Encrypt" in blob
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    sys.exit(main())
