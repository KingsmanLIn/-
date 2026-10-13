"""Phase 2: chunking and the extraction cache.

Covers chunk ids, page ranges, ~10% overlap, that every chunk's page range maps back
to the original pages, and the cache rules: same PDF -> hit, replaced PDF -> miss.

    python -m unittest discover -s tests -p "test_chunk_cache.py" -v
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from baseline_helpers import PROJECT_ROOT

sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import chunker as chunker_module   # noqa: E402
import extraction                  # noqa: E402
import pdf_fixtures                # noqa: E402
import pdftext                     # noqa: E402
from storage import DataStore      # noqa: E402

BUNDLED = PROJECT_ROOT / "data" / "papers"


def make_pages(texts):
    return [{"pdf_page": index, "text": text, "char_count": len(text)}
            for index, text in enumerate(texts, start=1)]


class ChunkStructure(unittest.TestCase):
    def setUp(self):
        self.chunker = chunker_module.Chunker(max_chars=600, overlap_ratio=0.1)

    def test_chunk_ids_and_order(self):
        pages = make_pages(["段落" * 400, "第二页" * 400])
        chunks = self.chunker.chunk(pages, document_id="paper-1")
        self.assertGreater(len(chunks), 2)
        self.assertEqual([f"paper-1-c{index + 1:03d}" for index in range(len(chunks))],
                         [chunk.chunk_id for chunk in chunks])
        self.assertEqual(list(range(len(chunks))), [chunk.order for chunk in chunks])

    def test_size_limit_is_respected(self):
        pages = make_pages(["甲" * 5000])
        chunks = self.chunker.chunk(pages)
        self.assertTrue(all(chunk.char_count <= 600 for chunk in chunks))

    def test_overlap_is_about_ten_percent(self):
        pages = make_pages(["abcdefghij" * 200])
        chunks = self.chunker.chunk(pages)
        self.assertGreater(len(chunks), 3)
        for previous, current in zip(chunks, chunks[1:]):
            shared = 0
            for length in range(min(120, previous.char_count), 0, -1):
                if current.text.startswith(previous.text[-length:]):
                    shared = length
                    break
            self.assertGreater(shared, 0, "consecutive chunks must overlap")
            self.assertLessEqual(shared, 0.2 * self.chunker.max_chars)

    def test_page_range_covers_each_chunk(self):
        pages = make_pages(["第一页内容" * 200, "第二页内容" * 200, "第三页内容" * 200])
        chunks = self.chunker.chunk(pages)
        self.assertEqual(1, chunks[0].start_page)
        self.assertEqual(3, chunks[-1].end_page)
        for chunk in chunks:
            self.assertLessEqual(chunk.start_page, chunk.end_page)
            self.assertTrue(set(chunk.pages()).issubset({1, 2, 3}))

    def test_chunk_pages_really_contain_the_chunk_text(self):
        """Every chunk's evidence pages must map back to the original page text."""
        pages = make_pages(["第一页内容" * 200, "第二页内容" * 200, "第三页内容" * 200])
        chunks = self.chunker.chunk(pages)
        for chunk in chunks:
            joined = "".join(page["text"] for page in pages
                             if page["pdf_page"] in chunk.pages())
            probe = "".join(chunk.text.split())[:40]
            self.assertIn(probe, "".join(joined.split()),
                          f"{chunk.chunk_id} claims pages {chunk.pages()} that do not contain it")

    def test_page_of_offset_maps_inside_the_chunk(self):
        pages = make_pages(["A" * 1200, "B" * 1200, "C" * 1200])
        chunks = self.chunker.chunk(pages)
        for chunk in chunks:
            for offset in range(0, chunk.char_count, 97):
                page = chunk.page_of_offset(offset)
                self.assertIn(page, chunk.pages())

    def test_sections_are_detected_and_named(self):
        pages = make_pages([
            "Abstract\nWe study fertility.\n" + "内容" * 100,
            "3. Data and methods\n" + "方法" * 100,
            "References\n" + "文献" * 100,
        ])
        chunks = chunker_module.Chunker(max_chars=500).chunk(pages)
        sections = {chunk.section for chunk in chunks}
        self.assertTrue(any("Abstract" in name for name in sections), sections)
        self.assertTrue(any("Data and methods" in name for name in sections), sections)

    def test_empty_page_list_gives_no_chunks(self):
        self.assertEqual([], self.chunker.chunk([]))
        self.assertEqual([], self.chunker.chunk(make_pages(["", "   "])))

    @unittest.skip("Real paper content excluded; synthetic page/chunk mapping is tested")
    def test_real_paper_chunks_map_back_to_pages(self):
        pages = pdftext.BuiltinBackend().extract(BUNDLED / "medical-2026-zang.pdf")
        chunks = chunker_module.Chunker(max_chars=4000).chunk(pages)
        self.assertGreater(len(chunks), 5)
        by_page = {page.pdf_page: page.text for page in pages}
        for chunk in chunks:
            self.assertGreaterEqual(chunk.start_page, 1)
            self.assertLessEqual(chunk.end_page, 29)
            probe = "".join(chunk.text.split())[:30]
            window = "".join(by_page[number] for number in chunk.pages())
            self.assertIn(probe, "".join(window.split()),
                          f"{chunk.chunk_id} does not map back to pages {chunk.pages()}")


class ExtractionCache(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cache-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.files = DataStore(self.tmp).ensure_directories()
        self.made = pdf_fixtures.build_all()
        self.pdf = self.tmp / "paper.pdf"
        shutil.copy2(self.made["en"], self.pdf)
        self.extractor = extraction.PdfTextExtractor(files=self.files, prefer=["builtin"])

    def cached_files(self):
        return sorted(path.name for path in self.files.extracted.glob("*.json"))

    def test_second_extraction_hits_the_cache(self):
        first = self.extractor.extract(self.pdf, paper_id="paper-0001-abcd")
        self.assertFalse(first.cached)
        self.assertEqual(["paper-0001-abcd.json"], self.cached_files())

        second = self.extractor.extract(self.pdf, paper_id="paper-0001-abcd")
        self.assertTrue(second.cached)
        self.assertEqual(first.source_sha256, second.source_sha256)
        self.assertEqual([page.text for page in first.pages], [page.text for page in second.pages])
        self.assertEqual("hit", self.extractor.last_diagnostics["cache"])

    def test_cache_is_validated_against_the_source_hash(self):
        first = self.extractor.extract(self.pdf, paper_id="paper-0001-abcd")
        # same page count, different bytes: must not be served from cache
        replaced = self.tmp / "paper-replaced.pdf"
        shutil.copy2(self.made["en"], replaced)
        with replaced.open("ab") as handle:
            handle.write(b"\n% changed\n")
        result = self.extractor.extract(replaced, paper_id="paper-0001-abcd")
        self.assertFalse(result.cached, "a replaced PDF must not hit the old cache")
        self.assertNotEqual(first.source_sha256, result.source_sha256)
        payload = json.loads(self.files.extracted_path("paper-0001-abcd").read_text(encoding="utf-8"))
        self.assertEqual(result.source_sha256, payload["source_sha256"])

    def test_cache_records_extractor_version_and_page_count(self):
        self.extractor.extract(self.pdf, paper_id="paper-0001-abcd")
        payload = json.loads(self.files.extracted_path("paper-0001-abcd").read_text(encoding="utf-8"))
        self.assertEqual(extraction.CACHE_SCHEMA, payload["schema"])
        self.assertEqual("builtin", payload["extractor"]["name"])
        self.assertTrue(payload["extractor"]["version"])
        self.assertEqual(len(payload["pages"]), payload["page_count"])
        self.assertEqual([1, 2], [page["pdf_page"] for page in payload["pages"]])

    def test_version_change_invalidates_the_cache(self):
        self.extractor.extract(self.pdf, paper_id="paper-0001-abcd")
        path = self.files.extracted_path("paper-0001-abcd")
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["extractor"]["version"] = "builtin-0.0.1"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        result = self.extractor.extract(self.pdf, paper_id="paper-0001-abcd")
        self.assertFalse(result.cached)
        self.assertIn("版本", self.extractor.last_diagnostics["note"])

    def test_page_count_mismatch_invalidates_the_cache(self):
        self.extractor.extract(self.pdf, paper_id="paper-0001-abcd")
        path = self.files.extracted_path("paper-0001-abcd")
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["pages"] = payload["pages"][:1]
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        result = self.extractor.extract(self.pdf, paper_id="paper-0001-abcd")
        self.assertFalse(result.cached)
        self.assertEqual(2, len(result.pages))

    def test_corrupt_cache_is_ignored_and_rewritten(self):
        self.extractor.extract(self.pdf, paper_id="paper-0001-abcd")
        path = self.files.extracted_path("paper-0001-abcd")
        path.write_text('{"schema": 1, "pages": [', encoding="utf-8")
        result = self.extractor.extract(self.pdf, paper_id="paper-0001-abcd")
        self.assertFalse(result.cached)
        self.assertEqual(2, len(result.pages))
        json.loads(path.read_text(encoding="utf-8"))          # rewritten and valid again

    def test_scanned_pdf_is_cached_as_no_text_layer(self):
        scanned = self.tmp / "scan.pdf"
        shutil.copy2(self.made["scanned"], scanned)
        result = self.extractor.extract(scanned, paper_id="paper-0002-abcd")
        self.assertEqual("no_text_layer", result.code)
        again = self.extractor.extract(scanned, paper_id="paper-0002-abcd")
        self.assertTrue(again.cached)
        self.assertEqual("no_text_layer", again.code)

    def test_cache_key_falls_back_to_the_source_hash(self):
        result = self.extractor.extract(self.pdf)
        self.assertFalse(result.cached)
        names = self.cached_files()
        self.assertEqual(1, len(names))
        self.assertTrue(names[0].startswith(result.source_sha256[:32]))

    def test_cleaning_and_chunking_helpers(self):
        result = self.extractor.extract(self.pdf, paper_id="paper-0001-abcd")
        pages, stats = extraction.PdfTextExtractor.clean(result, join_hyphens=True)
        self.assertIn("information", "".join(page["text"] for page in pages))
        self.assertGreaterEqual(stats.hyphen_joins, 1)
        chunks = extraction.PdfTextExtractor.chunks(result, max_chars=500, cleaned=True,
                                                    document_id="paper-0001-abcd")
        self.assertTrue(chunks)
        self.assertTrue(all(chunk.chunk_id.startswith("paper-0001-abcd-c") for chunk in chunks))


class BackendSelection(unittest.TestCase):
    def test_extractor_reports_available_backends(self):
        extractor = extraction.PdfTextExtractor()
        self.assertIn("builtin", extractor.available())
        self.assertTrue(extractor.backend_name())

    def test_prefer_builtin_only(self):
        extractor = extraction.PdfTextExtractor(prefer=["builtin"])
        self.assertEqual(["builtin"], extractor.available())
        self.assertEqual("builtin", extractor.backend_name())

    def test_pdftotext_and_builtin_agree_on_page_count(self):
        pdftotext = pdftext.PdftotextBackend()
        if not pdftotext.available():
            self.skipTest("pdftotext is not installed")
        pdf = BUNDLED / "medical-2026-zang.pdf"
        with tempfile.TemporaryDirectory() as tmp:
            extractor = extraction.PdfTextExtractor(files=DataStore(Path(tmp)), prefer=["pdftotext"])
            pdftotext_result = extractor.extract(pdf, paper_id="paper-0003-abcd")
        builtin_result = pdftext.BuiltinBackend().extract(pdf)
        self.assertEqual(len(builtin_result), pdftotext_result.page_count)
        self.assertEqual(list(range(1, len(builtin_result) + 1)),
                         [page.pdf_page for page in pdftotext_result.pages])


if __name__ == "__main__":
    unittest.main(verbosity=2)
