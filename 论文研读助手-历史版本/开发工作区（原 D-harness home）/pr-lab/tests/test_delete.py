"""Tests for paper deletion (storage + Store + POST /api/delete).

Run with the bundled interpreter, from the project root:

    runtime\\python.exe -m unittest discover -s tests -v

Every test uses a `tempfile` directory of its own. Nothing here ever points at the
library that ships in `data/` - deleting from that one would destroy the lab library.
"""
from __future__ import annotations

import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import uuid

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import job_policy                     # noqa: E402
import schema                         # noqa: E402
import server                         # noqa: E402
import session                        # noqa: E402
import storage                        # noqa: E402


# --- fixtures ---------------------------------------------------------------
def new_id():
    return str(uuid.uuid4())


def paper_record(paper_id, title, status="waiting_for_user"):
    """A minimal but valid paper record, written by the test itself."""
    return {
        "id": paper_id, "title": title, "authors": "", "year": "", "venue": "",
        "doi": "", "citation": "", "tags": "", "language": "en", "type": "empirical",
        "fields": {name: "" for name in server.FIELDS},
        "steps": [False, False, False, False], "annotations": [], "pageOffset": 0,
        "pageLabels": [], "lastPage": 1, "revision": 0,
        "created": "2026-01-01T00:00:00Z", "updated": "2026-01-01T00:00:00Z",
        "archived": False, "note_name": f"{title}--{paper_id[:8]}.md",
        "analysis": dict(schema.default_analysis(), status=status),
    }


class DeleteFixture(unittest.TestCase):
    """A private library in a temp directory with two fully populated papers."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="paper-delete-test-")
        self.directory = Path(self._tmp.name).resolve()
        self.target_id, self.other_id = new_id(), new_id()
        state = {
            "version": schema.SCHEMA_VERSION,
            "settings": {"vault_path": "", "sync_enabled": False, "last_sync": "",
                         "sync_error": "", "ai": schema.default_ai_settings()},
            "papers": [paper_record(self.target_id, "被删除的论文", status="queued"),
                       paper_record(self.other_id, "保留的论文")],
        }
        storage.atomic_write_json(self.directory / "library.json", state)
        self.store = server.Store(self.directory)
        self.store.state["settings"]["ai"]["auto_analyze"] = True
        self._write_artifacts(self.target_id)
        self._write_artifacts(self.other_id)
        self.store.persist()

    def tearDown(self):
        self._tmp.cleanup()

    def _write_artifacts(self, paper_id):
        self.store.files.write_analysis(paper_id, {"paper_id": paper_id, "fields": {}})
        self.store.files.write_failed_answer(paper_id, {"paper_id": paper_id,
                                                        "error_code": "invalid_response"})
        self.store.files.write_extracted(paper_id, {"paper_id": paper_id, "pages": [{"n": 1}]})
        storage.atomic_write_bytes(self.store.files.pdf_path(paper_id), b"%PDF-1.4 test\n")

    def read_library(self):
        return json.loads((self.directory / "library.json").read_text(encoding="utf-8"))


# --- storage.py: file level -------------------------------------------------
class StorageDeleteTests(DeleteFixture):

    def test_delete_paper_files_removes_all_four_artifacts(self):
        files = self.store.files
        self.assertTrue(files.pdf_path(self.target_id).is_file())
        self.assertTrue(files.analysis_path(self.target_id).is_file())
        self.assertTrue(files.failed_answer_path(self.target_id).is_file())
        self.assertTrue(files.extracted_path(self.target_id).is_file())

        removed = files.delete_paper_files(self.target_id)

        self.assertEqual(removed, {"pdf": True, "analysis": True,
                                   "failed_answer": True, "extracted": True})
        for path in (files.pdf_path(self.target_id), files.analysis_path(self.target_id),
                     files.failed_answer_path(self.target_id),
                     files.extracted_path(self.target_id)):
            self.assertFalse(path.exists(), str(path))

    def test_delete_paper_files_is_idempotent(self):
        files = self.store.files
        self.assertTrue(files.delete_paper_files(self.target_id)["pdf"])
        second = files.delete_paper_files(self.target_id)
        self.assertEqual(second, {"pdf": False, "analysis": False,
                                  "failed_answer": False, "extracted": False})

    def test_delete_paper_files_rejects_bad_ids_and_touches_nothing(self):
        files = self.store.files
        for bad in ("", "../library", "..\\library", "short", "A" * 80,
                    self.target_id + "/x", "UPPER-CASE-ID"):
            with self.assertRaises(ValueError):
                files.delete_paper_files(bad)
        # The valid paper and every one of its files is still there.
        self.assertTrue(files.pdf_path(self.target_id).is_file())
        self.assertTrue(files.analysis_path(self.target_id).is_file())
        self.assertTrue(files.failed_answer_path(self.target_id).is_file())
        self.assertTrue(files.extracted_path(self.target_id).is_file())


# --- server.py: library level ----------------------------------------------
class StoreDeleteTests(DeleteFixture):

    def test_delete_removes_record_and_files_and_keeps_other_paper(self):
        result = self.store.delete_paper(self.target_id)

        self.assertEqual(result["paper_id"], self.target_id)
        self.assertEqual(result["title"], "被删除的论文")
        self.assertEqual(result["deleted"], {"pdf": True, "analysis": True,
                                             "failed_answer": True, "extracted": True})

        ids = [paper["id"] for paper in self.store.snapshot()["papers"]]
        self.assertNotIn(self.target_id, ids)
        self.assertEqual(ids, [self.other_id])

        # The record is gone from the file on disk too, not only from memory.
        on_disk = [paper["id"] for paper in self.read_library()["papers"]]
        self.assertEqual(on_disk, [self.other_id])

        files = self.store.files
        for path in (files.pdf_path(self.target_id), files.analysis_path(self.target_id),
                     files.failed_answer_path(self.target_id),
                     files.extracted_path(self.target_id)):
            self.assertFalse(path.exists(), str(path))

        # ... and the other paper keeps every one of its files.
        self.assertTrue(files.pdf_path(self.other_id).is_file())
        self.assertTrue(files.analysis_path(self.other_id).is_file())
        self.assertTrue(files.failed_answer_path(self.other_id).is_file())
        self.assertTrue(files.extracted_path(self.other_id).is_file())

    def test_previous_library_keeps_the_pre_delete_state(self):
        self.store.delete_paper(self.target_id)
        previous = json.loads(self.store.files.previous.read_text(encoding="utf-8"))
        self.assertIn(self.target_id, [paper["id"] for paper in previous["papers"]])

    def test_unknown_and_malformed_ids_fail_cleanly(self):
        other_files = (self.store.files.pdf_path(self.other_id),
                       self.store.files.analysis_path(self.other_id))

        with self.assertRaises(ValueError) as caught:
            self.store.delete_paper(new_id())
        self.assertIn("找不到", str(caught.exception))

        with self.assertRaises(ValueError):
            self.store.delete_paper("")
        with self.assertRaises(ValueError):
            self.store.delete_paper("../../library")
        with self.assertRaises(ValueError):
            self.store.delete_paper("no-such")

        # Nothing was removed by a failed attempt.
        for paper_id in (self.target_id, self.other_id):
            self.assertIn(paper_id, [paper["id"] for paper in self.read_library()["papers"]])
        for path in other_files:
            self.assertTrue(path.is_file(), str(path))

    def test_deleting_twice_reports_a_clean_error(self):
        self.store.delete_paper(self.target_id)
        with self.assertRaises(ValueError) as caught:
            self.store.delete_paper(self.target_id)
        self.assertIn("找不到", str(caught.exception))


# --- ledger ----------------------------------------------------------------
class LedgerDeleteTests(DeleteFixture):

    def _snapshot(self, paper_id, job_id="job-delete-1"):
        return job_policy.JobSnapshot(
            job_id=job_id, paper_id=paper_id, generation=1, source_sha256="a" * 64,
            profile_id="p1", adapter="openai_compatible", mode="cloud_https",
            target_identity="cloud_https|api.example.test:443", model="m",
            credential_ref="env:TEST_KEY", credential_revision=1, config_revision=1,
            policy_version=job_policy.POLICY_VERSION, prompt_version="reading-card-v2",
            language="en", approval_id="ap-1", budget_id="b-1",
            created_at="2026-01-01T00:00:00Z", reason="manual")

    def _ledger_with_rows(self, paper_id, job_id="job-delete-1"):
        ledger = job_policy.JobLedger(self.directory)
        ledger.freeze_job(self._snapshot(paper_id, job_id))
        ledger.grant_approval(paper_id, self._snapshot(paper_id, job_id).as_dict(), model="m")
        return ledger

    def test_ledger_loses_job_and_approval_for_the_deleted_paper_only(self):
        ledger = self._ledger_with_rows(self.target_id, "job-target")
        ledger.freeze_job(self._snapshot(self.other_id, "job-other"))
        ledger.grant_approval(self.other_id,
                              self._snapshot(self.other_id, "job-other").as_dict(), model="m")
        self.assertIsNotNone(ledger.approval_for(self.target_id))
        self.store._job_ledger = ledger

        self.store.delete_paper(self.target_id)

        self.assertIsNone(ledger.frozen_job(self.target_id))
        self.assertIsNone(ledger.approval_for(self.target_id))
        self.assertNotIn(self.target_id, ledger.state["jobs"])
        self.assertNotIn(self.target_id, ledger.state["approvals"])
        # persisted, not only held in memory
        reloaded = job_policy.JobLedger(self.directory)
        self.assertNotIn(self.target_id, reloaded.state["jobs"])
        self.assertNotIn(self.target_id, reloaded.state["approvals"])

        # the other paper's rows survive untouched
        self.assertIsNotNone(reloaded.frozen_job(self.other_id))
        self.assertIsNotNone(reloaded.approval_for(self.other_id))

    def test_approval_removal_is_persisted_even_without_a_job_row(self):
        ledger = job_policy.JobLedger(self.directory)
        ledger.grant_approval(self.target_id, self._snapshot(self.target_id).as_dict())
        self.store._job_ledger = ledger

        self.store.delete_paper(self.target_id)

        reloaded = job_policy.JobLedger(self.directory)
        self.assertNotIn(self.target_id, reloaded.state["approvals"])
        self.assertIsNone(reloaded.approval_for(self.target_id))


# --- POST /api/delete ------------------------------------------------------
class DeleteRouteTests(DeleteFixture):
    """The route end-to-end over a real loopback socket, with a real cookie session."""

    def setUp(self):
        super().setUp()
        self.sessions = session.SessionStore()
        self.token = "t" * 32
        handler = server.handler_for(self.store, self.token, manager=None,
                                     sessions=self.sessions)
        self.httpd = server.BoundedHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.current = self.sessions.create_session("test-agent")

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        super().tearDown()

    def post(self, path, payload, csrf=True, cookie=True):
        headers = {"Host": f"127.0.0.1:{self.port}",
                   "Content-Type": "application/json"}
        if csrf:
            headers["X-CSRF-Token"] = self.current.csrf
        if cookie:
            headers["Cookie"] = f"{session.COOKIE_NAME}={self.current.session_id}"
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.request("POST", path, body=json.dumps(payload), headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read().decode("utf-8"))
        finally:
            connection.close()

    def get(self, path):
        headers = {"Host": f"127.0.0.1:{self.port}"}
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.request("GET", path, headers=headers)
            response = connection.getresponse()
            return response.status, response.getheader("Content-Type"), response.read()
        finally:
            connection.close()

    def test_delete_route_removes_paper_and_returns_state(self):
        status, body = self.post("/api/delete", {"paper_id": self.target_id})

        self.assertEqual(status, 200)
        self.assertEqual(body["title"], "被删除的论文")
        self.assertEqual(body["deleted"], {"pdf": True, "analysis": True,
                                           "failed_answer": True, "extracted": True})
        ids = [paper["id"] for paper in body["state"]["papers"]]
        self.assertEqual(ids, [self.other_id])
        self.assertFalse(self.store.files.pdf_path(self.target_id).exists())

    def test_delete_route_needs_the_session_csrf_token(self):
        status, body = self.post("/api/delete", {"paper_id": self.target_id}, csrf=False)
        self.assertEqual(status, 403)
        self.assertIn("error", body)
        # nothing was deleted
        self.assertTrue(self.store.files.pdf_path(self.target_id).is_file())

        status, _ = self.post("/api/delete", {"paper_id": self.target_id}, cookie=False)
        self.assertEqual(status, 403)
        self.assertTrue(self.store.files.pdf_path(self.target_id).is_file())

    def test_delete_route_rejects_empty_and_unknown_ids_with_chinese_errors(self):
        for payload, expected in (({"paper_id": ""}, "请选择要删除的论文"),
                                  ({"paper_id": "   "}, "请选择要删除的论文"),
                                  ({}, "请选择要删除的论文"),
                                  ({"paper_id": new_id()}, "找不到这篇论文"),
                                  ({"paper_id": "../../library"}, "无效的论文编号")):
            with self.subTest(payload=payload):
                status, body = self.post("/api/delete", payload)
                self.assertEqual(status, 400)
                self.assertEqual(body["error"], expected)
                self.assertNotIn("Traceback", json.dumps(body))

        # The library is intact after every refused request.
        ids = [paper["id"] for paper in self.read_library()["papers"]]
        self.assertEqual(sorted(ids), sorted([self.target_id, self.other_id]))

    def test_second_delete_of_the_same_paper_is_a_clean_400(self):
        status, _ = self.post("/api/delete", {"paper_id": self.target_id})
        self.assertEqual(status, 200)
        status, body = self.post("/api/delete", {"paper_id": self.target_id})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "找不到这篇论文")

    def test_ui_declares_the_delete_action_and_the_vault_warning(self):
        """The button, the confirm dialog and the `api('delete')` call all ship together."""
        status, _, raw = self.get("/")
        self.assertEqual(status, 200)
        html = raw.decode("utf-8")
        for marker in ('id="delete-paper"', 'id="delete-dialog"',
                       'id="delete-paper-title"', 'id="confirm-delete-paper"'):
            self.assertIn(marker, html)
        # The confirmation must say the Obsidian vault is not touched.
        self.assertIn("Obsidian 笔记库不会被改动", html)

        status, _, raw = self.get("/static/app.js")
        self.assertEqual(status, 200)
        script = raw.decode("utf-8")
        self.assertIn("function openDeletePaper()", script)
        self.assertIn("api('delete',{paper_id:id})", script)
        self.assertIn("Obsidian 笔记库未改动", script)


if __name__ == "__main__":
    unittest.main(verbosity=2)
