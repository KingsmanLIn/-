"""Deleting a paper whose PDF was removed by hand: the record must still be cleanable.

This is the reported case "the PDF file is already gone but the entry stays in the library".
Run:  D:\\harness home\\pr-lab\\runtime\\python.exe D:\\harness home\\pr-lab\\tests\\test_missing_source_delete.py
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import reading_card      # noqa: E402
import schema            # noqa: E402
import server            # noqa: E402

PAPER_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
OTHER_ID = "ffffffff-1111-2222-3333-444444444444"
NOW = "2026-10-13T00:00:00Z"


def make_paper(paper_id, title):
    return {"id": paper_id, "title": title, "authors": "", "year": "", "venue": "", "doi": "",
            "citation": "", "tags": "", "language": "en", "type": "empirical",
            "fields": {key: "" for key in reading_card.FIELD_KEYS},
            "steps": [False, False, False, False], "annotations": [], "pageOffset": 0,
            "pageLabels": [], "lastPage": 1, "revision": 0, "created": NOW, "updated": NOW,
            "archived": False, "note_name": "x.md", "analysis": schema.default_analysis()}


class MissingSourceDelete(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="pr-missing-"))
        self.store = server.Store(self.directory)
        self.store.state["papers"].append(make_paper(PAPER_ID, "gone.pdf"))
        self.store.state["papers"].append(make_paper(OTHER_ID, "kept"))
        self.store.persist()
        (self.store.files.papers / f"{OTHER_ID}.pdf").write_bytes(b"%PDF-1.4 fixture")

    def test_snapshot_reports_the_missing_source(self):
        papers = {p["id"]: p for p in self.store.snapshot()["papers"]}
        self.assertTrue(papers[PAPER_ID]["source_missing"])
        self.assertFalse(papers[OTHER_ID]["source_missing"])
        # the flag is derived, never persisted
        stored = self.store.paper(PAPER_ID)
        self.assertNotIn("source_missing", stored)

    def test_a_paper_without_a_pdf_can_still_be_deleted(self):
        result = self.store.delete_paper(PAPER_ID)
        self.assertFalse(result["deleted"]["pdf"], "there was no file to remove")
        self.assertEqual("gone.pdf", result["title"])
        remaining = [p["id"] for p in self.store.snapshot()["papers"]]
        self.assertEqual([OTHER_ID], remaining)
        self.assertTrue((self.store.files.papers / f"{OTHER_ID}.pdf").is_file())

    def test_deleting_a_library_of_missing_sources_empties_it(self):
        for paper in list(self.store.state["papers"]):
            self.store.delete_paper(paper["id"])
        self.assertEqual([], self.store.snapshot()["papers"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
