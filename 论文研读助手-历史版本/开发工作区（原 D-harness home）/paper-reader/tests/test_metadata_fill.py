"""Metadata auto-fill: the analysis-time integration and the never-overwrite rule.

Run:  D:\\harness home\\pr-lab\\runtime\\python.exe D:\\harness home\\pr-lab\\tests\\test_metadata_fill.py

Offline: an injected extractor supplies a synthetic front page, an injected provider answers,
and no request ever leaves the machine.
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import analysis          # noqa: E402
import schema            # noqa: E402
import server            # noqa: E402
import test_multipass as harness   # noqa: E402

FRONT_PAGE = "\n".join([
    "GLOBAL SYNTHETIC CYCLES AND RISK PREMIUMS",
    "Jane Q. Author, John Doe",
    "NBER Working Paper No. 24677",
    "June 2018",
    "doi:10.1234/synthetic.2018.1",
    "",
    "The sample shows that " + harness.sentence(1) + " in the data.",
    " ".join(f"filler{index}" for index in range(60)),
])

#: The same page with the byline marked the way journals print it. The bare form above (no
#: "By") is the deliberate-limitation case: the parser would rather return "" than guess a
#: name, so the test only requires that it never fabricates one.
FRONT_PAGE_WITH_BY = FRONT_PAGE.replace("Jane Q. Author, John Doe",
                                        "By Jane Q. Author, John Doe")


class FrontMatterExtractor(harness.FakeExtractor):
    """Same pages as the harness, with a real-looking front page on page 1."""

    front_page = FRONT_PAGE_WITH_BY

    def extract(self, path, paper_id=""):
        result = harness.FakeExtraction(self.page_count)
        result.pages[0]["text"] = self.front_page
        result.pages[0]["char_count"] = len(self.front_page)
        return result


class BareBylineExtractor(FrontMatterExtractor):
    """The conservative case: a byline with no "By" marker must not be invented."""

    front_page = FRONT_PAGE


def build(page_count=6, prefill=None, extractor_class=FrontMatterExtractor):
    directory = tempfile.mkdtemp(prefix="pr-metadata-")
    store = server.Store(directory)
    store.update_ai_settings({"provider": "openai_compatible", "mode": "cloud_https",
                              "base_url": "https://example.invalid/v1",
                              "model": "fake-model", "api_key_env": "",
                              "auto_analyze": False})
    paper = {"id": harness.PAPER_ID, "title": "synthetic-front-matter-paper", "authors": "",
             "year": "", "venue": "", "doi": "", "citation": "", "tags": "", "language": "en",
             "type": "empirical",
             "fields": {key: "" for key in __import__("reading_card").FIELD_KEYS},
             "steps": [False, False, False, False], "annotations": [], "pageOffset": 0,
             "pageLabels": [], "lastPage": 1, "revision": 0, "created": harness.NOW,
             "updated": harness.NOW, "archived": False, "note_name": "synthetic.md",
             "analysis": schema.default_analysis()}
    paper.update(prefill or {})
    store.state["papers"].append(paper)
    store.persist()
    (store.files.papers / f"{harness.PAPER_ID}.pdf").write_bytes(b"%PDF-1.4 synthetic fixture")
    adapter = harness.FakeAdapter(page_count)
    manager = analysis.AnalysisManager(store, provider_factory=lambda settings: adapter,
                                       extractor_factory=lambda: extractor_class(page_count),
                                       log=lambda record: None)
    return store, manager, adapter


class AutoFillDuringAnalysis(unittest.TestCase):
    def test_front_matter_is_filled_from_the_extraction(self):
        store, manager, adapter = build()
        manager.approve_target()
        manager.enqueue(harness.PAPER_ID, reason="manual")
        manager.process(harness.PAPER_ID)
        self.assertEqual(1, adapter.calls)
        self.assertEqual("ready", store.analysis_meta(harness.PAPER_ID)["status"])
        paper = store.paper(harness.PAPER_ID)
        # The values are printed so the report can show what the parser actually produced.
        print("filled:", {key: paper[key] for key in
                          ("title", "authors", "year", "venue", "doi", "citation")})
        self.assertTrue(paper["authors"], "authors must be filled")
        self.assertEqual("2018", paper["year"])
        self.assertTrue(paper["venue"], "venue must be filled")
        self.assertTrue(paper["doi"].startswith("10.1234/"), paper["doi"])
        # The title belongs to the import path: an analysis-time fill never renames a paper.
        self.assertEqual("synthetic-front-matter-paper", paper["title"])

    def test_a_bare_byline_is_read_without_a_by_marker(self):
        """A byline on the line under the title must be read (no "By" prefix needed)."""
        store, manager, _adapter = build(extractor_class=BareBylineExtractor)
        manager.approve_target()
        manager.enqueue(harness.PAPER_ID, reason="manual")
        manager.process(harness.PAPER_ID)
        paper = store.paper(harness.PAPER_ID)
        authors = paper["authors"]
        print("bare byline authors:", repr(authors))
        self.assertIn("Jane", authors, "the byline must be read, not skipped")
        self.assertIn("Doe", authors)
        self.assertNotIn("NBER", authors, "furniture must never become an author")
        # the fields that do not depend on the byline are still filled
        self.assertEqual("2018", paper["year"])
        self.assertTrue(paper["doi"].startswith("10.1234/"))

    def test_existing_values_are_never_overwritten(self):
        store, manager, _adapter = build(prefill={"authors": "我自己填的作者",
                                                  "year": "1999",
                                                  "doi": "10.9999/mine"})
        manager.approve_target()
        manager.enqueue(harness.PAPER_ID, reason="manual")
        manager.process(harness.PAPER_ID)
        paper = store.paper(harness.PAPER_ID)
        self.assertEqual("我自己填的作者", paper["authors"])
        self.assertEqual("1999", paper["year"])
        self.assertEqual("10.9999/mine", paper["doi"])
        # the fields that were still empty are the ones that got filled
        self.assertTrue(paper["venue"])
        self.assertTrue(paper["citation"])


class ApplyMetadata(unittest.TestCase):
    def test_only_empty_fields_are_written(self):
        store, _manager, _adapter = build()
        changed = store.apply_metadata(harness.PAPER_ID,
                                       {"authors": "A. Author", "year": "2018",
                                        "venue": "NBER", "doi": "10.1/x", "citation": "c",
                                        "title": "Parsed Title"})
        self.assertEqual(["authors", "year", "venue", "doi", "citation"], changed)
        paper = store.paper(harness.PAPER_ID)
        self.assertEqual("A. Author", paper["authors"])
        self.assertEqual("synthetic-front-matter-paper", paper["title"])

        again = store.apply_metadata(harness.PAPER_ID, {"authors": "Other", "year": "2020"})
        self.assertEqual([], again)
        self.assertEqual("A. Author", store.paper(harness.PAPER_ID)["authors"])

    def test_title_is_replaced_only_when_allowed(self):
        store, _manager, _adapter = build()
        changed = store.apply_metadata(harness.PAPER_ID, {"title": "Parsed Title"},
                                       allow_title=True)
        self.assertEqual(["title"], changed)
        self.assertEqual("Parsed Title", store.paper(harness.PAPER_ID)["title"])

    def test_blank_and_missing_values_are_ignored(self):
        store, _manager, _adapter = build()
        self.assertEqual([], store.apply_metadata(harness.PAPER_ID,
                                                  {"authors": "   ", "year": "", "venue": None}))
        self.assertEqual("", store.paper(harness.PAPER_ID)["year"])


class BrokenExtractor(harness.FakeExtractor):
    """An extractor that refuses the document (no text layer)."""

    def extract(self, path, paper_id=""):
        result = harness.FakeExtraction(1)
        result.ok = False
        result.code = "no_text_layer"
        result.message = "扫描件"
        return result


class ImportTimeFill(unittest.TestCase):
    PDF = b"%PDF-1.4 synthetic import fixture\n" + b"x" * 2048

    def test_import_fills_metadata_and_drops_the_file_name_title(self):
        directory = tempfile.mkdtemp(prefix="pr-import-")
        store = server.Store(directory, extractor_factory=lambda: FrontMatterExtractor(6))
        paper = store.import_pdf(self.PDF, "hahn-et-al-2026-welfare.pdf")
        print("imported:", {key: paper[key] for key in
                            ("title", "authors", "year", "venue", "doi", "citation")})
        self.assertNotEqual("hahn-et-al-2026-welfare", paper["title"])
        self.assertIn("Synthetic Cycles", paper["title"])
        self.assertEqual("Jane Q. Author and John Doe", paper["authors"])
        self.assertEqual("2018", paper["year"])
        self.assertTrue(paper["venue"])
        self.assertTrue(paper["doi"].startswith("10.1234/"))
        # the note name follows the final title, not the file name
        self.assertTrue(paper["note_name"].startswith(paper["title"][:10]),
                        paper["note_name"])

    def test_import_survives_an_unreadable_document(self):
        directory = tempfile.mkdtemp(prefix="pr-import-")
        store = server.Store(directory, extractor_factory=lambda: BrokenExtractor(1))
        paper = store.import_pdf(self.PDF, "scanned-paper.pdf")
        self.assertEqual("scanned-paper", paper["title"])
        self.assertEqual("", paper["authors"])
        self.assertEqual("", paper["year"])
        self.assertEqual(1, len(store.state["papers"]))

    def test_chinese_title_switches_the_language_budget(self):
        directory = tempfile.mkdtemp(prefix="pr-import-")
        store = server.Store(directory, extractor_factory=lambda: FrontMatterExtractor(6))
        original = FrontMatterExtractor.front_page
        try:
            FrontMatterExtractor.front_page = "学术财经笔记全汇总\n张三，李四\n2023年\n"
            paper = store.import_pdf(self.PDF, "notes.pdf")
        finally:
            FrontMatterExtractor.front_page = original
        self.assertEqual("zh", paper["language"])
        self.assertEqual("zh", paper["analysis"]["language"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
