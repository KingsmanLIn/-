"""Phase 2: PDF text extraction.

Covers Chinese / English / two-column PDFs, empty page counts, scanned files, tables
with negative values and percentages, physical page numbers, and the no-fabrication
rule (a page without a text layer is reported, never guessed).

    python -m unittest discover -s tests -p "test_pdftext.py" -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

from baseline_helpers import PROJECT_ROOT

sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pdf_fixtures          # noqa: E402
import pdftext               # noqa: E402
import extraction            # noqa: E402

BUNDLED = PROJECT_ROOT / "data" / "papers"


class FixturesMixin:
    @classmethod
    def setUpClass(cls):
        cls.made = pdf_fixtures.build_all()
        cls.backend = pdftext.BuiltinBackend()

    def extract(self, key):
        return self.backend.extract(self.made[key])


class PageStructure(FixturesMixin, unittest.TestCase):
    """Requirement 2: every page carries pdf_page, text and char_count, from 1."""

    def test_chinese_pages(self):
        pages = self.extract("zh")
        self.assertEqual(3, len(pages))
        self.assertEqual([1, 2, 3], [page.pdf_page for page in pages])
        for page in pages:
            self.assertEqual(page.char_count, len(page.text))
            self.assertGreater(page.char_count, 40)
        self.assertIn("分级诊疗制度下的医疗控费机制", pages[0].text)

    def test_english_pages(self):
        pages = self.extract("en")
        self.assertEqual(2, len(pages))
        self.assertIn("Does the effect survive?", pages[0].text)
        self.assertIn("[−0.26, −0.10]".replace("−", "-"), pages[0].text)

    def test_two_column_reading_order(self):
        pages = self.extract("columns")
        text = pages[0].text
        left_end = text.find("compositional change drives the entire result.")
        table_start = text.find("Table 2. Decomposition")
        self.assertGreater(left_end, -1, "left column text missing")
        self.assertGreater(table_start, -1, "right column text missing")
        self.assertLess(left_end, table_start,
                        "left column must be read before the right column")

    def test_page_sizes_are_present(self):
        pages = self.extract("en")
        self.assertAlmostEqual(612.0, pages[0].width, places=1)
        self.assertAlmostEqual(792.0, pages[0].height, places=1)


class NoTextLayer(FixturesMixin, unittest.TestCase):
    """Requirements 3 and 7: no fabricated text, actionable outcome."""

    def test_truly_blank_page(self):
        pages = self.extract("blank")
        self.assertEqual(1, len(pages))
        self.assertEqual("", pages[0].text)
        self.assertFalse(pages[0].has_text)

    def test_scanned_pdf_reports_images_and_no_text(self):
        pages = self.extract("scanned")
        self.assertEqual(2, len(pages))
        self.assertTrue(all(page.text == "" for page in pages))
        self.assertTrue(all(page.has_images for page in pages),
                        "image-only pages must be flagged as images")

    def test_missing_file_is_reported_not_raised(self):
        import tempfile
        from storage import DataStore
        sys.path.insert(0, str(PROJECT_ROOT))
        import extraction
        with tempfile.TemporaryDirectory() as tmp:
            extractor = extraction.PdfTextExtractor(files=DataStore(Path(tmp)))
            result = extractor.extract(Path(tmp) / "nope.pdf")
            self.assertEqual("missing_file", result.code)
            self.assertEqual([], result.pages)
            self.assertTrue(result.warnings)

    def test_broken_pdf_is_reported(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            broken = Path(tmp) / "broken.pdf"
            broken.write_bytes(b"%PDF-1.6\nthis is not a real pdf")
            with self.assertRaises(pdftext.ExtractionError) as caught:
                self.backend.extract(broken)
            self.assertEqual("broken_pdf", caught.exception.code)
            self.assertTrue(caught.exception.action)


class NumbersSurvive(unittest.TestCase):
    """Requirement 4: cleaning must not change digits, signs, percent or units."""

    def test_table_values_extracted_verbatim(self):
        made = pdf_fixtures.build_all()
        pages = pdftext.BuiltinBackend().extract(made["zh"])
        text = pages[0].text
        for value in ("12345.67", "2345.10", "-820.55", "98765.43",
                      "-3.20", "5.60", "-18.90", "24.10",
                      "32.50%", "8.40%", "12.10%", "58.60%"):
            self.assertIn(value, text, f"{value} was altered or lost")

    def test_english_numbers_survive(self):
        made = pdf_fixtures.build_all()
        pages = pdftext.BuiltinBackend().extract(made["en"])
        text = pages[0].text
        for value in ("0.15%", "-12,500", "0.04", "-0.150", "[-0.23, -0.07]", "0.041", "95%"):
            self.assertIn(value, text, f"{value} was altered or lost")


class CleaningIsConservative(unittest.TestCase):
    """Requirement 4: report what changed, never touch numeric or unit content."""

    def test_numbers_and_units_are_untouched(self):
        import textclean
        source = "费用 12,345.67 元，下降 -3.20%，m² 与 km2 都保留；p<0.01。"
        pages = [{"pdf_page": 1, "text": source, "char_count": len(source)}]
        cleaned, stats = textclean.clean_pages(pages, drop_headers=False)
        self.assertEqual(source, cleaned[0]["text"])
        self.assertEqual(0, stats.total_changes())

    def test_superscripts_are_not_flattened(self):
        import textclean
        pages = [{"pdf_page": 1, "text": "面积 5 m² 与 R2 不同", "char_count": 0}]
        cleaned, _stats = textclean.clean_pages(pages, drop_headers=False)
        self.assertIn("m²", cleaned[0]["text"])
        self.assertNotIn("m2 ", cleaned[0]["text"].replace("m²", "m²"))

    def test_ligatures_and_invisible_characters_are_normalised(self):
        import textclean
        pages = [{"pdf_page": 1, "text": "classi\ufb01cation de\ufb01ned\u00ad", "char_count": 0}]
        cleaned, stats = textclean.clean_pages(pages, drop_headers=False)
        self.assertEqual("classification defined", cleaned[0]["text"])
        self.assertEqual(3, stats.ligatures + stats.invisible)

    def test_hyphen_joining_is_off_by_default_and_conservative_when_on(self):
        import textclean
        text = "the informa-\ntion on wages\ncost-benefit analysis"
        pages = [{"pdf_page": 1, "text": text, "char_count": 0}]
        default, _ = textclean.clean_pages(pages, drop_headers=False)
        self.assertIn("informa-\ntion", default[0]["text"])
        joined, stats = textclean.clean_pages(pages, drop_headers=False, join_hyphens=True)
        self.assertIn("information", joined[0]["text"])
        self.assertIn("cost-benefit", joined[0]["text"], "a real hyphen must survive")
        self.assertEqual(1, stats.hyphen_joins)

    def test_running_header_and_page_number_are_detected(self):
        import textclean
        pages = []
        for page_no in (1, 2, 3, 4):
            body = "\n".join([f"Journal of Examples 2026"] + [f"正文第 {page_no} 页第 {i} 行内容" for i in range(1, 9)]
                             + [f"- {page_no} -"])
            pages.append({"pdf_page": page_no, "text": body, "char_count": len(body)})
        cleaned, stats = textclean.clean_pages(pages, drop_headers=True)
        self.assertTrue(any("journal of examples" in signature for signature in stats.repeated_lines),
                        stats.repeated_lines)
        self.assertGreaterEqual(stats.header_footer_lines_removed, 4)
        for page in cleaned:
            self.assertNotIn("Journal of Examples", page["text"])
            self.assertIn("正文第", page["text"])

    def test_body_text_is_never_removed_as_furniture(self):
        import textclean
        pages = [{"pdf_page": number, "text": f"重复的正文句子出现在每一页\n第 {number} 页的内容",
                  "char_count": 0} for number in (1, 2, 3, 4)]
        cleaned, stats = textclean.clean_pages(pages, drop_headers=True)
        for page in cleaned:
            self.assertIn("重复的正文句子出现在每一页", page["text"])


class BundledPapers(unittest.TestCase):
    """The three real papers in data/papers must extract with physical page numbers."""

    @unittest.skip("Real original paper excluded by synthetic-data-only authorization")
    def test_medical_chinese_paper(self):
        pages = pdftext.BuiltinBackend().extract(BUNDLED / "medical-2026-zang.pdf")
        self.assertEqual(29, len(pages))
        self.assertEqual(29, len([page for page in pages if page.text.strip()]))
        self.assertEqual(list(range(1, 30)), [page.pdf_page for page in pages])
        self.assertIn("分级诊疗", pages[0].text)
        self.assertGreaterEqual(pdftext.text_quality(pages[0].text), 0.8)

    @unittest.skip("Real original paper excluded by synthetic-data-only authorization")
    def test_fertility_english_paper(self):
        pages = pdftext.BuiltinBackend().extract(BUNDLED / "fertility-2026-coffey.pdf")
        self.assertEqual(57, len(pages))
        self.assertIn("DOES FERTILITY STABILIZE", pages[0].text.upper())
        self.assertGreaterEqual(pdftext.text_quality(pages[1].text), 0.8)

    @unittest.skip("Real original paper excluded by synthetic-data-only authorization")
    def test_scanned_finance_paper_has_no_text_layer(self):
        pages = pdftext.BuiltinBackend().extract(BUNDLED / "finance-2021-zhang.pdf")
        self.assertEqual(15, len(pages))
        self.assertEqual([], [page for page in pages if page.text.strip()],
                         "this PDF is a scan; text must not be invented")

    def test_punctuation_only_text_layer_is_not_evidence(self):
        """A scan can carry a text layer of nothing but spaces and punctuation."""
        fake_text = ("：                       ：\n\n\n                     ＊\n"
                     "                                                            \n"
                     "                                                                          。\n") * 3
        pages = [pdftext.Page(pdf_page=number, text=fake_text, char_count=len(fake_text))
                 for number in (1, 2, 3)]
        code, message = extraction.PdfTextExtractor._diagnose(pages)
        self.assertEqual("no_text_layer", code)
        self.assertIn("标点", message)
        self.assertTrue(pdftext.is_junk_text(fake_text))

    def test_sparse_text_layer_is_not_evidence(self):
        """Many characters but almost no content: a scan artefact, not readable text."""
        sparse = ("       ：              ＊\n" * 200) + ("  。   ＊  \n" * 100)
        self.assertGreater(len(sparse), 4000)
        self.assertTrue(pdftext.is_junk_text(sparse))
        pages = [pdftext.Page(pdf_page=1, text=sparse, char_count=len(sparse))]
        code, _message = extraction.PdfTextExtractor._diagnose(pages)
        self.assertEqual("no_text_layer", code)

    def test_real_text_is_still_usable(self):
        text = ("本文利用 2020—2023 年病案数据估计分级诊疗制度对医疗费用的影响，"
                "回归系数为 -0.083，在 5% 水平上显著，费用下降约 8.3%。")
        pages = [pdftext.Page(pdf_page=1, text=text, char_count=len(text))]
        self.assertGreaterEqual(pdftext.meaningful_chars(text), pdftext.MIN_MEANINGFUL_CHARS)
        code, _message = extraction.PdfTextExtractor._diagnose(pages)
        self.assertEqual("ok", code)

    @unittest.skip("Real original paper excluded by synthetic-data-only authorization")
    def test_pdftotext_backend_matches_page_counts(self):
        backend = pdftext.PdftotextBackend()
        if not backend.available():
            self.skipTest("pdftotext is not installed")
        raw, _stderr = backend.extract(BUNDLED / "medical-2026-zang.pdf")
        parts = [part for part in raw.split("\f")]
        if parts and not parts[-1].strip():
            parts = parts[:-1]
        self.assertEqual(29, len(parts))
        self.assertIn("分级诊疗", parts[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
