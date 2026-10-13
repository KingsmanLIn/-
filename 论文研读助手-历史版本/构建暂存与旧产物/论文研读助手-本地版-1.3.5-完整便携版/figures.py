"""Render figure-bearing pages so a multimodal model can read the charts.

Text extraction throws pictures away: a paper's figures, charts and maps only exist as pixels.
This module finds the pages that carry them (the extractor already records `has_images` per
page, and captions are visible in the text) and renders those pages to JPEG with the poppler
tools the machine already has (`pdftocairo`/`pdftoppm`). The images travel with the request as
OpenAI-compatible `image_url` parts; only a multimodal model can use them.

Everything here is best-effort and bounded: a missing renderer, a timeout or an oversized image
returns fewer images (or none) with a reason, and the analysis continues text-only.
"""
from __future__ import annotations

import base64
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

#: Rendering cost is set here. A page at 130 dpi in colour PNG is roughly 150-200 KB, which
#: keeps a four-page attachment under a megabyte while axes, legends and colour coding stay
#: readable. (This build of poppler has no JPEG output, so PNG it is; the endpoint takes both.)
DEFAULT_DPI = 130
MAX_IMAGE_BYTES = 900 * 1024
MAX_TOTAL_BYTES = 3 * 1024 * 1024
RENDER_TIMEOUT_SECONDS = 25

_CAPTION_RE = re.compile(
    r"(?:^|\n)\s*(?:figure|fig\.?|table|chart|exhibit|panel|map|plate)\s*"
    r"(?:[A-Z]?\d{1,3}|[IVXLC]{1,6})\b", re.I)
_CJK_CAPTION_RE = re.compile(r"(?:^|\n)\s*(?:图|表|图表)\s*\d{1,3}")
#: A page that is mostly a picture (a scanned page) is not a "figure page" worth sending.
_PAGE_NUMBER_RE = re.compile(r"^\s*\d{1,4}\s*$")


def renderer():
    """The first available poppler renderer, or ''."""
    for name in ("pdftocairo", "pdftoppm"):
        found = shutil.which(name)
        if found:
            return found
    return ""


def caption_pages(pages):
    """Pages whose text mentions a figure/table caption (fallback when images are unknown)."""
    found = []
    for page in pages or ():
        number = _page_number(page)
        text = _page_text(page)
        if number and (_CAPTION_RE.search(text) or _CJK_CAPTION_RE.search(text)):
            found.append(number)
    return sorted(set(found))


def figure_pages(pages, limit=0):
    """Pages worth showing to a multimodal model, in reading order.

    A page qualifies when the extractor saw an image on it, or when its text carries a figure or
    table caption (some PDFs draw charts as vector art, which no image flag would catch).
    """
    flagged, captioned = [], []
    for page in pages or ():
        number = _page_number(page)
        if not number:
            continue
        if _has_images(page):
            flagged.append(number)
        elif _CAPTION_RE.search(_page_text(page)) or _CJK_CAPTION_RE.search(_page_text(page)):
            captioned.append(number)
    ordered = sorted(set(flagged)) + [page for page in sorted(set(captioned))
                                      if page not in set(flagged)]
    return ordered[:limit] if limit and limit > 0 else ordered


def render_pages(pdf_path, page_numbers, *, dpi=DEFAULT_DPI,
                 max_image_bytes=MAX_IMAGE_BYTES, max_total_bytes=MAX_TOTAL_BYTES,
                 timeout=RENDER_TIMEOUT_SECONDS):
    """``([{"page", "mime", "data", "bytes"}], note)`` for the requested pages.

    `data` is base64 PNG ready for an `image_url` data URL. Pages that fail or exceed the caps
    are skipped, and `note` says so in a fixed phrase (it is logged, never shown as user text).
    """
    tool = renderer()
    pages = [int(page) for page in page_numbers or () if int(page) > 0]
    if not tool:
        return [], "no_renderer"
    if not pages:
        return [], "no_pages"
    source = Path(pdf_path)
    if not source.is_file():
        return [], "missing_pdf"

    out = []
    total = 0
    skipped = []
    scratch = Path(tempfile.mkdtemp(prefix="figures-"))
    try:
        for page in pages:
            stem = scratch / f"page-{page}"
            command = _command(tool, source, page, stem, dpi)
            try:
                subprocess.run(command, check=False, timeout=timeout,
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except (OSError, subprocess.SubprocessError):
                skipped.append(page)
                continue
            target = stem.with_suffix(".png")
            if not target.is_file():
                skipped.append(page)
                continue
            blob = target.read_bytes()
            if len(blob) > max_image_bytes or total + len(blob) > max_total_bytes:
                skipped.append(page)
                continue
            total += len(blob)
            out.append({"page": page, "mime": "image/png",
                        "data": base64.b64encode(blob).decode("ascii"),
                        "bytes": len(blob)})
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    note = "" if not skipped else "skipped:" + ",".join(str(page) for page in skipped)
    return out, note


def _command(tool, source, page, stem, dpi):
    """Both tools write ``<stem>.png`` with -singlefile, so the stem carries no extension."""
    return [tool, "-png", "-r", str(dpi), "-f", str(page), "-l", str(page),
            "-singlefile", str(source), str(stem)]


def _page_number(page):
    if isinstance(page, dict):
        value = page.get("pdf_page")
    else:
        value = getattr(page, "pdf_page", None)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _page_text(page):
    if isinstance(page, dict):
        return str(page.get("text", "") or "")
    return str(getattr(page, "text", "") or "")


def _has_images(page):
    if isinstance(page, dict):
        return bool(page.get("has_images"))
    return bool(getattr(page, "has_images", False))


def _self_check():
    """Render from a real PDF when one is available, and report sizes."""
    import json
    root = Path(__file__).resolve().parent
    pdfs = sorted((root / "data" / "papers").glob("*.pdf"))
    report = {"renderer": renderer(), "pdfs": [p.name for p in pdfs]}
    if not pdfs or not report["renderer"]:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    cache = sorted((root / "data" / "extracted").glob("*.json"))
    pages = []
    for path in cache:
        payload = json.loads(path.read_text(encoding="utf-8"))
        pages = payload.get("pages") or []
        if pages:
            report["cache"] = path.name
            break
    report["flagged_pages"] = figure_pages(pages)
    report["caption_pages"] = caption_pages(pages)
    chosen = (report["flagged_pages"] or report["caption_pages"] or [1])[:4]
    images, note = render_pages(pdfs[0], chosen)
    report["rendered"] = [{"page": item["page"], "kb": round(item["bytes"] / 1024, 1)}
                          for item in images]
    report["note"] = note
    report["total_kb"] = round(sum(item["bytes"] for item in images) / 1024, 1)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _self_check()
