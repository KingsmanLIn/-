"""Phase 1: safe storage helpers and the interrupted-write guarantee.

    python -m unittest discover -s tests -p "test_storage.py" -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from baseline_helpers import PROJECT_ROOT

sys.path.insert(0, str(PROJECT_ROOT))
import schema      # noqa: E402
import storage     # noqa: E402

PAPER_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


class PathSafety(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="storage-"))
        self.files = storage.DataStore(self.tmp).ensure_directories()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_paper_id_validation(self):
        for good in (PAPER_ID, "medical-2026-zang", "a1b2c3d4"):
            self.assertEqual(good, storage.check_paper_id(good))
        for bad in ("", "short", "../escape", "..", "a/b", "a\\b", "UPPER-case-id", "id with space",
                    "x" * 65, "id\x00null", "../../etc/passwd"):
            with self.assertRaises(ValueError, msg=bad):
                storage.check_paper_id(bad)

    def test_file_and_directory_name_validation(self):
        self.assertEqual("answer.json", storage.check_file_name("answer.json"))
        for bad in ("answer.txt", "../answer.json", "a/b.json", "", ".hidden.json", "-lead.json"):
            with self.assertRaises(ValueError, msg=bad):
                storage.check_file_name(bad)
        self.assertEqual("analyses", storage.check_dir_name("analyses"))
        for bad in ("../analyses", "Analyses", "a/b", "", "1abc"):
            with self.assertRaises(ValueError, msg=bad):
                storage.check_dir_name(bad)

    def test_paths_stay_inside_the_data_directory(self):
        self.assertEqual(self.files.analyses / f"{PAPER_ID}.json", self.files.analysis_path(PAPER_ID))
        self.assertEqual(self.files.extracted / f"{PAPER_ID}.json", self.files.extracted_path(PAPER_ID))
        for bad in ("../library", "..%2f..%2flibrary", "/etc/passwd"):
            with self.assertRaises(ValueError, msg=bad):
                self.files.analysis_path(bad)
        with self.assertRaises(ValueError):
            storage.ensure_within(self.tmp / "analyses" / ".." / ".." / "outside.json", self.tmp / "analyses")

    def test_unknown_files_in_analyses_are_ignored(self):
        (self.files.analyses / "UPPER-CASE-ID.json").write_text("{}", encoding="utf-8")
        (self.files.analyses / "under_score.json").write_text("{}", encoding="utf-8")
        (self.files.analyses / f"{PAPER_ID}.json").write_text("{}", encoding="utf-8")
        (self.files.analyses / "notes.txt").write_text("x", encoding="utf-8")
        self.assertEqual([PAPER_ID], [pid for pid, _ in self.files.iter_analyses()])


class AtomicWrites(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="atomic-"))
        self.files = storage.DataStore(self.tmp).ensure_directories()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_interrupted_write_keeps_the_previous_file(self):
        """Requirement: simulate an interrupted analysis write; the old file survives."""
        self.files.write_analysis(PAPER_ID, {"answer": "first", "fields": 13})
        good = self.files.analysis_path(PAPER_ID).read_bytes()

        def explode(path, data):
            raise OSError("simulated interruption (disk full)")

        original = storage.os.replace
        storage.os.replace = explode
        try:
            with self.assertRaises(OSError):
                self.files.write_analysis(PAPER_ID, {"answer": "second"})
        finally:
            storage.os.replace = original

        self.assertEqual(good, self.files.analysis_path(PAPER_ID).read_bytes(),
                         "a failed write must leave the old answer intact")
        payload, error = self.files.read_analysis(PAPER_ID)
        self.assertIsNone(error)
        self.assertEqual("first", payload["answer"])

    def test_no_temporary_files_are_left_behind(self):
        self.files.write_analysis(PAPER_ID, {"answer": "first"})
        self.files.write_analysis(PAPER_ID, {"answer": "second"})
        self.assertEqual([], [p.name for p in self.files.analyses.iterdir() if p.suffix == ".tmp"])
        payload, error = self.files.read_analysis(PAPER_ID)
        self.assertIsNone(error)
        self.assertEqual("second", payload["answer"])

    def test_partial_write_is_never_parsed_as_valid(self):
        """Even a torn file is reported as unreadable instead of silently accepted."""
        broken = self.files.analysis_path(PAPER_ID)
        broken.write_text('{"answer": "trunc', encoding="utf-8")
        payload, error = self.files.read_analysis(PAPER_ID)
        self.assertIsNone(payload)
        self.assertIn("JSONDecodeError", error)
        self.assertTrue(broken.is_file(), "reading must not delete or rewrite the broken file")

    def test_size_limits_are_enforced(self):
        with self.assertRaises(ValueError):
            self.files.write_analysis(PAPER_ID, {"blob": "x" * 200}, limit=100)
        self.assertFalse(self.files.has_analysis(PAPER_ID))
        accepted, error = storage.read_json_from_bytes(b"{}" * 100, limit=50)
        self.assertIsNone(accepted)
        self.assertIn("大小上限", error)

    def test_concurrent_writes_never_produce_a_mixed_file(self):
        payloads = [{"answer": f"run-{i}", "pad": "y" * 5000} for i in range(12)]

        def worker(payload):
            self.files.write_analysis(PAPER_ID, payload)

        threads = [threading.Thread(target=worker, args=(p,)) for p in payloads]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        payload, error = self.files.read_analysis(PAPER_ID)
        self.assertIsNone(error)
        self.assertIn(payload, payloads, "interleaved writes must not corrupt the file")
        self.assertEqual([], [p.name for p in self.files.analyses.iterdir() if p.suffix == ".tmp"])

    def test_extracted_cache_round_trip(self):
        pages = [{"page": 1, "text": "摘要"}, {"page": 2, "text": "引言"}]
        self.files.write_extracted(PAPER_ID, {"schema": schema.EXTRACTED_FILE_SCHEMA,
                                              "pdf_sha256": "ab" * 32, "pages": pages})
        payload, error = self.files.read_extracted(PAPER_ID)
        self.assertIsNone(error)
        self.assertEqual(pages, payload["pages"])
        self.assertTrue(self.files.has_extracted(PAPER_ID))

    def test_delete_analysis_removes_only_the_answer(self):
        self.files.write_analysis(PAPER_ID, {"answer": "x"})
        self.files.write_extracted(PAPER_ID, {"pages": []})
        self.assertTrue(self.files.delete_analysis(PAPER_ID))
        self.assertFalse(self.files.has_analysis(PAPER_ID))
        self.assertTrue(self.files.has_extracted(PAPER_ID), "the cache is independent of the answer")
        self.assertFalse(self.files.delete_analysis(PAPER_ID))


class ErrorMessagesAreSafe(unittest.TestCase):
    """Error text reaches the browser, so it must not carry data or credentials."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="safe-"))
        self.files = storage.DataStore(self.tmp).ensure_directories()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_size_and_validation_errors_do_not_echo_content(self):
        secret = "sk-live-abcdef0123456789"
        payload = {"api_key": secret, "answer": "机密研究内容"}
        with self.assertRaises(ValueError) as caught:
            self.files.write_analysis(PAPER_ID, payload, limit=10)
        message = str(caught.exception)
        self.assertNotIn(secret, message)
        self.assertNotIn("机密研究内容", message)
        self.assertIn("大小上限", message)

    def test_broken_file_message_has_no_file_body(self):
        broken = self.files.analysis_path(PAPER_ID)
        broken.write_text('{"answer": "用户写的内容机密"} tail', encoding="utf-8")
        _payload, error = self.files.read_analysis(PAPER_ID)
        self.assertIn("JSONDecodeError", error)
        self.assertNotIn("用户写的内容机密", error)

    def test_rejected_names_are_quoted_but_nothing_else_leaks(self):
        with self.assertRaises(ValueError) as caught:
            self.files.analysis_path("../../etc/passwd")
        self.assertNotIn("passwd", str(caught.exception).replace("../../etc/passwd", ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
