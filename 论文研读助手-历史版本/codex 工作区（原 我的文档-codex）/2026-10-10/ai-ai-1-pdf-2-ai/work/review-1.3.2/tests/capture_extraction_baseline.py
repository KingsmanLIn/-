"""End-to-end check of the phase 2 input layer on the three real papers.

Extraction -> cache hit -> cleaning -> chunking, with page mapping verified.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import extraction        # noqa: E402
import pdftext           # noqa: E402
from storage import DataStore  # noqa: E402

PAPERS = ROOT / "data" / "papers"
report = {"backend_order": pdftext.BACKEND_ORDER, "papers": []}

tmp = Path(tempfile.mkdtemp(prefix="phase2-"))
files = DataStore(tmp).ensure_directories()
extractor = extraction.PdfTextExtractor(files=files)

for pdf in sorted(PAPERS.glob("*.pdf")):
    paper_id = pdf.stem
    entry = {"paper_id": paper_id, "size_mb": round(pdf.stat().st_size / 1048576, 2)}

    started = time.time()
    first = extractor.extract(pdf, paper_id=paper_id)
    entry["first_pass_seconds"] = round(time.time() - started, 2)
    entry["code"] = first.code
    entry["backend"] = first.backend
    entry["pages"] = first.page_count
    entry["pages_with_text"] = len(first.text_pages)
    entry["total_chars"] = first.total_chars
    entry["message"] = first.message
    entry["cached"] = first.cached
    entry["page_numbers"] = [page.pdf_page for page in first.pages][:3] + ["..."] + \
                            [page.pdf_page for page in first.pages][-1:]

    started = time.time()
    second = extractor.extract(pdf, paper_id=paper_id)
    entry["second_pass_seconds"] = round(time.time() - started, 3)
    entry["second_pass_cached"] = second.cached
    entry["cache_identical"] = [page.text for page in first.pages] == [page.text for page in second.pages]

    if first.ok:
        entry["quality_page1"] = round(pdftext.text_quality(first.pages[0].text), 3)
        cleaned, stats = extraction.PdfTextExtractor.clean(first, join_hyphens=False)
        entry["clean_stats"] = stats.as_dict()
        entry["clean_stats"].pop("repeated_lines", None)
        chunks = extraction.PdfTextExtractor.chunks(first, max_chars=6000,
                                                    overlap_ratio=0.1, document_id=paper_id)
        entry["chunks"] = len(chunks)
        entry["chunk_example"] = chunks[0].as_dict() if chunks else None
        if entry["chunk_example"]:
            entry["chunk_example"]["text"] = entry["chunk_example"]["text"][:160] + "…"
        # every chunk must map back to the pages it claims
        by_page = {page.pdf_page: "".join(page.text.split()) for page in first.pages}
        mismatches = []
        for chunk in chunks:
            probe = "".join(chunk.text.split())[:30]
            window = "".join(by_page.get(number, "") for number in chunk.pages())
            if probe and probe not in window:
                mismatches.append(chunk.chunk_id)
        entry["chunks_failing_page_mapping"] = mismatches
        entry["page_numbers_contiguous"] = [page.pdf_page for page in first.pages] == \
                                          list(range(1, first.page_count + 1))
    report["papers"].append(entry)

report["extracted_files"] = sorted(path.name for path in files.extracted.glob("*.json"))
report["_meta"] = {
    "capturedBy": "tests/capture_extraction_baseline.py",
    "extractorVersion": pdftext.EXTRACTOR_VERSION,
    "note": ("pdftotext is the primary backend and the builtin extractor is the fallback; "
             "a page whose text layer is unreadable is reported in `pages_with_text`/`message` "
             "instead of being exposed as evidence. The extracted cache lives in a temporary "
             "data directory and is deleted after capture."),
}
print(json.dumps(report, ensure_ascii=False, indent=2))
(path := ROOT / "tests" / "phase2-extraction-baseline.json").write_text(
    json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print("written:", path)
shutil.rmtree(tmp, ignore_errors=True)
