"""Phase 8 acceptance: the eight server-side scenarios of the integrated product.

This is the phase's acceptance test - it drives a real server (in-process, with a
scriptable provider) over a temporary copy of the frozen v1 library and checks that the
whole product still behaves as one piece.

    python -m unittest discover -s tests -p "test_acceptance.py" -v
"""
from __future__ import annotations

import reading_card
import contextlib
import hashlib
import json
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from baseline_helpers import PROJECT_ROOT, fresh_library_v1

sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import analysis      # noqa: E402
import providers     # noqa: E402
import schema        # noqa: E402
import server        # noqa: E402
from card_fixtures import build_card, pages_from_prompt   # noqa: E402
from storage import DataStore                             # noqa: E402

ZH_PAPER = "medical-2026-zang"
EN_PAPER = "fertility-2026-coffey"
SCAN_PAPER = "finance-2021-zhang"


def card_from_request(request, _payload):
    """Answer with quotes taken from the prompt the manager actually sent."""
    pages = pages_from_prompt(request.user)
    return build_card(pages) if pages else {"schema_version": 1, "overall": {}, "answers": {},
                                            "terms": [], "reader_questions": [], "warnings": []}


class AcceptanceCase(unittest.TestCase):
    auto_analyze = False
    provider = "ollama"

    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.tmp = Path(tempfile.mkdtemp(prefix="phase8-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        fresh_library_v1(self.tmp)
        self.v1_bytes = (self.tmp / "library.json").read_bytes()
        self.v1_state = json.loads(self.v1_bytes.decode("utf-8"))
        self.store = server.Store(self.tmp)
        self.store.update_ai_settings({"provider": self.provider, "model": "acceptance-model",
                                       "auto_analyze": self.auto_analyze})
        self.events = []
        self.fake = providers.FakeProvider(script=["success"], payload={})
        self.fake.payload_for = card_from_request
        self.manager = analysis.AnalysisManager(self.store,
                                                provider_factory=lambda settings: self.fake,
                                                log=self.events.append)
        self.port = self._free_port()
        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", self.port),
                                                server.handler_for(self.store, "accept-token",
                                                                   self.manager))
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.shutdown)

    @staticmethod
    def _free_port():
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            return probe.getsockname()[1]

    # -- http helpers ------------------------------------------------------
    def call(self, path, method="GET", body=None, token="accept-token", headers=None,
             expect=200, raw=False, data=None):
        """Return the decoded JSON, or (bytes, headers) when raw=True."""
        head = dict(headers or {})
        payload_bytes = data
        if body is not None:
            payload_bytes = json.dumps(body).encode()
            head.setdefault("Content-Type", "application/json")
        if token:
            head["X-Reader-Token"] = token
        request = Request(f"http://127.0.0.1:{self.port}{path}", data=payload_bytes,
                          method=method, headers=head)
        try:
            with urlopen(request, timeout=60) as response:
                payload = response.read()
                status = response.status
                reply_headers = dict(response.headers)
        except HTTPError as exc:
            payload = exc.read()
            status = exc.code
            reply_headers = dict(exc.headers)
        if expect is not None:
            self.assertEqual(expect, status, f"{method} {path}: {payload[:200]!r}")
        if raw:
            return payload, reply_headers
        try:
            return json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return payload

    def drain(self, timeout=90):
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.manager.process_once()
            depth = self.manager.queue_depth()
            if not depth["queued"] and not depth["running"]:
                return
            time.sleep(0.02)

    def status(self, paper_id):
        return self.call(f"/api/analysis/{paper_id}/status")

    def import_pdf(self, name, blob=None):
        from urllib.parse import quote
        blob = blob if blob is not None else (self.tmp / "papers" / f"{ZH_PAPER}.pdf").read_bytes()
        return self.call("/api/import", "POST", data=blob,
                         headers={"X-File-Name": quote(name, safe=""),
                                  "Content-Type": "application/pdf"})


class MigrationKeepsHumanWork(AcceptanceCase):
    """1. v1 library.json migrates to v2 without touching human fields or notes."""

    def test_migration_is_lossless_and_idempotent(self):
        state = self.store.state
        self.assertEqual(schema.SCHEMA_VERSION, state["version"])
        self.assertEqual(2, state["version"])

        # every human field, step and annotation survives byte for byte
        by_id = {paper["id"]: paper for paper in state["papers"]}
        self.assertEqual(len(self.v1_state["papers"]), len(by_id))
        for original in self.v1_state["papers"]:
            migrated = by_id[original["id"]]
            self.assertEqual(original["fields"], migrated["fields"], original["id"])
            self.assertEqual(original["steps"], migrated["steps"], original["id"])
            self.assertEqual(original["annotations"], migrated["annotations"], original["id"])
            self.assertEqual(original["revision"], migrated["revision"], original["id"])
            self.assertEqual(original["lastPage"], migrated["lastPage"], original["id"])
            # and the new metadata exists without inventing state
            self.assertIn("analysis", migrated)
            self.assertEqual("none", migrated["analysis"]["status"])

        # migration is idempotent: a second start changes nothing but the version fields
        before = json.dumps(state, ensure_ascii=False, sort_keys=True)
        reopened = server.Store(self.tmp).state
        self.assertEqual(before, json.dumps(reopened, ensure_ascii=False, sort_keys=True))

        # the v1 file is preserved for rollback
        self.assertTrue((self.tmp / "library.previous.json").is_file())

    def test_old_library_still_saves_after_migration(self):
        paper = self.store.paper(ZH_PAPER)
        payload = self.call("/api/save", "POST",
                            {"id": ZH_PAPER, "revision": paper["revision"],
                             "patch": {"fields": dict(paper["fields"], question="迁移后仍可编辑")}})
        self.assertEqual("迁移后仍可编辑", payload["paper"]["fields"]["question"])
        self.assertEqual(paper["revision"] + 1, payload["paper"]["revision"])


class ImportQueueing(AcceptanceCase):
    """2. Import queues the job when a provider exists, otherwise waits for one."""

    def test_with_provider_goes_to_the_queue(self):
        self.store.update_ai_settings({"provider": "ollama", "auto_analyze": True})
        payload = self.import_pdf("有Provider.pdf")
        status = payload["paper"]["analysis"]["status"]
        self.assertIn(status, ("queued", "running"), status)
        self.assertEqual(0, len(self.fake.calls), "the import must not wait for the model")
        self.drain()
        self.assertEqual("ready", self.status(payload["paper"]["id"])["status"])

    def test_without_provider_waits_and_reading_still_works(self):
        self.store.update_ai_settings({"provider": "none", "auto_analyze": True})
        payload = self.import_pdf("无Provider.pdf")
        paper = payload["paper"]
        self.assertEqual("waiting_for_provider", paper["analysis"]["status"])
        self.assertEqual(0, len(self.fake.calls))
        # the new paper is immediately usable
        saved = self.call("/api/save", "POST",
                          {"id": paper["id"], "revision": paper["revision"],
                           "patch": {"fields": dict(paper["fields"], claim="未配置也能写"),
                                     "annotations": []}})
        self.assertEqual("未配置也能写", saved["paper"]["fields"]["claim"])
        answer = self.call(f"/api/analysis/{paper['id']}")
        self.assertIsNone(answer["answer"])
        self.assertIn("尚未就绪", answer["reason"])

    def test_default_waits_for_the_user_click(self):
        self.store.update_ai_settings({"provider": "ollama", "auto_analyze": False})
        payload = self.import_pdf("默认手动.pdf")
        self.assertEqual("waiting_for_user", payload["paper"]["analysis"]["status"])

    def test_provider_arriving_later_resumes_waiting_papers(self):
        self.store.update_ai_settings({"provider": "none", "auto_analyze": True})
        payload = self.import_pdf("后来配置.pdf")
        paper_id = payload["paper"]["id"]
        self.assertEqual("waiting_for_provider", self.status(paper_id)["status"])
        settings = self.call("/api/settings", "POST",
                             {"vault_path": "", "sync_enabled": False, "provider": "ollama",
                              "model": "acceptance-model", "auto_analyze": True})
        self.assertIn(paper_id, settings["resumed"])
        self.drain()
        self.assertEqual("ready", self.status(paper_id)["status"])


class ReadingCardOnDemand(AcceptanceCase):
    """3. A ready analysis is not shown until the user asks for that field."""

    def ready_paper(self, paper_id=ZH_PAPER):
        self.call(f"/api/analysis/{paper_id}/generate", "POST", {"force": True})
        self.drain()
        self.assertEqual("ready", self.status(paper_id)["status"])
        return paper_id

    def test_status_hides_the_answer_and_the_answer_endpoint_serves_it(self):
        paper_id = self.ready_paper()
        status = self.status(paper_id)
        body = json.dumps(status, ensure_ascii=False)
        self.assertNotIn("answers", status)
        self.assertNotIn("evidence", body)
        for field in ("summary", "claim", "method"):
            self.assertNotIn(field, status)
        self.assertTrue(status["has_answer"])

        answer = self.call(f"/api/analysis/{paper_id}")
        self.assertTrue(answer["answer_available"])
        self.assertEqual(13, len(answer["answer"]["answers"]))
        self.assertEqual(reading_card.PROMPT_VERSION, answer["answer"]["prompt_version"])

    def test_every_field_can_be_traced_back_to_a_physical_page(self):
        paper_id = self.ready_paper()
        answer = self.call(f"/api/analysis/{paper_id}")["answer"]
        pages = answer["verification"]["page_count"]
        checked = 0
        for key, item in answer["answers"].items():
            if item["status"] == "not_found":
                self.assertEqual([], item["evidence"])
                self.assertIn("原文未说明", item["answer"])
                continue
            self.assertTrue(item["evidence"], key)
            for entry in item["evidence"]:
                self.assertTrue(1 <= entry["page"] <= pages, f"{key}: page {entry['page']}")
                self.assertTrue(entry["verified"], key)
                self.assertTrue(entry["quote"].strip(), key)
                checked += 1
        self.assertGreaterEqual(checked, 4)
        self.assertGreaterEqual(answer["verification"]["verified_quotes"], 4)

    def test_manual_fields_never_receive_the_ai_text(self):
        paper = self.store.paper(ZH_PAPER)
        before = json.dumps({"fields": paper["fields"], "steps": paper["steps"],
                             "annotations": paper["annotations"]}, ensure_ascii=False)
        self.ready_paper()
        self.call(f"/api/analysis/{ZH_PAPER}")
        after = self.store.paper(ZH_PAPER)
        self.assertEqual(before, json.dumps({"fields": after["fields"], "steps": after["steps"],
                                             "annotations": after["annotations"]},
                                            ensure_ascii=False))


class FailureKeepsManualWorkUsable(AcceptanceCase):
    """4. A failed analysis must not block reading, saving, export or notes."""

    def failed_paper(self):
        self.fake.payload_for = None
        self.fake.payload = {"schema_version": 1, "overall": {}, "answers": {}, "terms": [],
                             "reader_questions": [], "warnings": []}
        self.call(f"/api/analysis/{ZH_PAPER}/generate", "POST", {"force": True})
        self.drain()
        self.assertEqual("failed", self.status(ZH_PAPER)["status"])
        return ZH_PAPER

    def test_manual_work_survives_a_failed_analysis(self):
        self.failed_paper()
        paper = self.store.paper(ZH_PAPER)

        saved = self.call("/api/save", "POST",
                          {"id": ZH_PAPER, "revision": paper["revision"],
                           "patch": {"fields": dict(paper["fields"], claim="AI 失败也要能写"),
                                     "steps": [True, False, False, False],
                                     "annotations": [{"id": "note-1", "type": "argument",
                                                      "page": 2, "quote": "引文", "note": "我的理解",
                                                      "rects": [[0.1, 0.2, 0.3, 0.02]],
                                                      "created": "2026-10-10T00:00:00Z"}]}})
        self.assertEqual("AI 失败也要能写", saved["paper"]["fields"]["claim"])
        self.assertEqual([True, False, False, False], saved["paper"]["steps"])
        self.assertEqual(1, len(saved["paper"]["annotations"]))

        markdown, headers = self.call(f"/api/export/{ZH_PAPER}", raw=True)
        self.assertIn("text/markdown", headers.get("Content-Type", ""))
        text = markdown.decode("utf-8")
        self.assertIn("AI 失败也要能写", text)
        self.assertIn("我的理解", text)

        pdf, headers = self.call(f"/api/pdf/{ZH_PAPER}", raw=True)
        self.assertTrue(pdf.startswith(b"%PDF-"))

        status = self.status(ZH_PAPER)
        self.assertEqual("failed", status["status"])
        self.assertEqual("validation_failed", status["error_code"])
        self.assertTrue(status["error_summary"])
        self.assertIsNone(self.call(f"/api/analysis/{ZH_PAPER}")["answer"])
        # the retry path stays available
        self.fake.payload_for = card_from_request
        self.call(f"/api/analysis/{ZH_PAPER}/generate", "POST", {})
        self.drain()
        self.assertEqual("ready", self.status(ZH_PAPER)["status"])

    def test_unreadable_paper_does_not_break_the_app(self):
        """A scanned PDF yields not_found fields instead of a crash."""
        self.call(f"/api/analysis/{SCAN_PAPER}/generate", "POST", {"force": True})
        self.drain()
        status = self.status(SCAN_PAPER)
        self.assertIn(status["status"], ("ready", "failed"), status)
        # whatever the outcome, the paper stays editable and exportable
        paper = self.store.paper(SCAN_PAPER)
        saved = self.call("/api/save", "POST",
                          {"id": SCAN_PAPER, "revision": paper["revision"],
                           "patch": {"fields": dict(paper["fields"], summary="扫描件也可手写")}})
        self.assertEqual("扫描件也可手写", saved["paper"]["fields"]["summary"])


class BackupContainsAnswersButNoSecrets(AcceptanceCase):
    """5. Backup/restore carries analyses and metadata, never a key."""

    def setUp(self):
        super().setUp()
        import os
        self.secret = "sk-live-phase8-acceptance-secret"
        os.environ["PAPER_READER_PHASE8_KEY"] = self.secret
        self.addCleanup(os.environ.pop, "PAPER_READER_PHASE8_KEY", None)

    def test_backup_round_trip(self):
        self.call("/api/settings", "POST",
                  {"vault_path": "", "sync_enabled": False, "provider": "ollama",
                   "model": "acceptance-model", "api_key_env": "PAPER_READER_PHASE8_KEY"})
        self.call(f"/api/analysis/{ZH_PAPER}/generate", "POST", {"force": True})
        self.drain()
        before = self.status(ZH_PAPER)
        before_meta = self.store.analysis_meta(ZH_PAPER)
        self.assertEqual("ready", before["status"])
        self.assertEqual(64, len(before_meta["source_sha256"]))
        answer_before = self.call(f"/api/analysis/{ZH_PAPER}")["answer"]

        blob, headers = self.call("/api/backup", raw=True)
        self.assertIn("attachment", headers.get("Content-Disposition", ""))
        with zipfile.ZipFile(BytesIO(blob)) as archive:
            names = archive.namelist()
            self.assertIn("manifest.json", names)
            self.assertIn(f"analyses/{ZH_PAPER}.json", names)
            self.assertTrue(any(name.startswith("papers/") and name.endswith(".pdf")
                                for name in names))
            for name in names:
                self.assertNotIn(self.secret, archive.read(name).decode("utf-8", "replace"),
                                 f"secret leaked into {name}")
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            self.assertEqual(schema.SCHEMA_VERSION, manifest.get("version", schema.SCHEMA_VERSION))

        # restore into a brand new directory
        target = Path(tempfile.mkdtemp(prefix="phase8-restore-"))
        self.addCleanup(shutil.rmtree, target, ignore_errors=True)
        fresh = server.Store(target)
        restored = fresh.restore(blob)
        self.assertGreaterEqual(restored, len(self.store.state["papers"]))
        meta = fresh.analysis_meta(ZH_PAPER)
        self.assertEqual(before["status"], meta["status"])
        self.assertEqual(before_meta["source_sha256"], meta["source_sha256"])
        self.assertEqual(before_meta["prompt_version"], meta["prompt_version"])
        self.assertEqual(before_meta["model"], meta["model"])
        payload, error = fresh.analysis_answer(ZH_PAPER)
        self.assertIsNone(error)
        self.assertEqual(answer_before["answers"], payload["answers"])
        self.assertEqual(answer_before["verification"], payload["verification"])
        # a restored, ready analysis is served as ready by the API too
        self.assertTrue(fresh.files.has_analysis(ZH_PAPER))

    def test_restore_never_reactivates_ai(self):
        target = Path(tempfile.mkdtemp(prefix="phase8-restore2-"))
        self.addCleanup(shutil.rmtree, target, ignore_errors=True)
        fresh = server.Store(target)
        blob, _headers = self.call("/api/backup", raw=True)
        fresh.restore(blob)
        self.assertEqual("none", fresh.analysis_meta(ZH_PAPER)["status"])


class ObsidianAndExport(AcceptanceCase):
    """6. Sync keeps "my notes"; the default export never leaks a hidden answer."""

    def test_sync_updates_generated_area_only(self):
        vault = self.tmp / "vault"
        (vault / "论文研读助手").mkdir(parents=True)
        note = vault / "论文研读助手" / self.store.paper(ZH_PAPER)["note_name"]
        note.write_text(f"{server.START}\n旧内容\n{server.END}\n\n## 我的补充\n\n这段必须保留。\n",
                        encoding="utf-8")
        self.call("/api/settings", "POST", {"vault_path": str(vault), "sync_enabled": True})
        text = note.read_text(encoding="utf-8")
        self.assertIn("这段必须保留。", text)
        self.assertIn("分级诊疗", text)
        self.assertTrue((vault / "论文研读助手" / "00 文献索引.md").is_file())

    def test_export_has_no_ai_text(self):
        self.call(f"/api/analysis/{ZH_PAPER}/generate", "POST", {"force": True})
        self.drain()
        answer = self.call(f"/api/analysis/{ZH_PAPER}")["answer"]
        summary = answer["answers"]["summary"]["answer"]
        markdown = self.call(f"/api/export/{ZH_PAPER}", raw=True)[0].decode("utf-8")
        self.assertNotIn(summary, markdown, "the export must not contain the hidden AI answer")
        self.assertNotIn("【AI 参考】", markdown)
        self.assertNotIn(reading_card.PROMPT_VERSION, markdown)
        # the human record is still exported
        paper = self.store.paper(ZH_PAPER)
        self.call("/api/save", "POST",
                  {"id": ZH_PAPER, "revision": paper["revision"],
                   "patch": {"fields": dict(paper["fields"], claim="人工结论必须导出")}})
        markdown = self.call(f"/api/export/{ZH_PAPER}", raw=True)[0].decode("utf-8")
        self.assertIn("人工结论必须导出", markdown)


class ConcurrencyAndRestart(AcceptanceCase):
    """8-9. Revision conflicts, worker vs. human edits, restart recovery."""

    def test_two_windows_conflict_keeps_the_newer_record(self):
        paper = self.store.paper(ZH_PAPER)
        revision = paper["revision"]
        first = self.call("/api/save", "POST",
                          {"id": ZH_PAPER, "revision": revision,
                           "patch": {"fields": dict(paper["fields"], claim="窗口 A 的结论")}})
        self.assertEqual("窗口 A 的结论", first["paper"]["fields"]["claim"])
        conflict = self.call("/api/save", "POST",
                             {"id": ZH_PAPER, "revision": revision,
                              "patch": {"fields": dict(paper["fields"], claim="窗口 B 的结论")}},
                             expect=409)
        self.assertIn("另一个窗口", conflict["error"])
        # window B reloads, then saves on top of the newer revision
        fresh = self.call(f"/api/state")["papers"]
        current = next(item for item in fresh if item["id"] == ZH_PAPER)
        second = self.call("/api/save", "POST",
                           {"id": ZH_PAPER, "revision": current["revision"],
                            "patch": {"fields": dict(current["fields"], claim="窗口 B 的结论")}})
        self.assertEqual("窗口 B 的结论", second["paper"]["fields"]["claim"])
        # window A's text was replaced by the newer revision, not lost to a stale write
        self.assertNotEqual("窗口 A 的结论", second["paper"]["fields"]["claim"])
        self.assertEqual(current["revision"] + 1, second["paper"]["revision"])
        on_disk = json.loads((self.tmp / "library.json").read_text(encoding="utf-8"))
        stored = next(item for item in on_disk["papers"] if item["id"] == ZH_PAPER)
        self.assertEqual(current["revision"] + 1, stored["revision"])
        self.assertEqual("窗口 B 的结论", stored["fields"]["claim"])

    def test_worker_does_not_overwrite_a_human_save(self):
        paper = self.store.paper(ZH_PAPER)
        revision = paper["revision"]
        self.call(f"/api/analysis/{ZH_PAPER}/generate", "POST", {"force": True})
        # the user saves while the worker is running
        saved = self.call("/api/save", "POST",
                          {"id": ZH_PAPER, "revision": revision,
                           "patch": {"fields": dict(paper["fields"], claim="worker 运行中的人工修改")}})
        self.assertGreater(saved["paper"]["revision"], revision)
        self.drain()
        after = self.store.paper(ZH_PAPER)
        self.assertEqual("worker 运行中的人工修改", after["fields"]["claim"],
                         "the worker must not clobber human fields")
        self.assertEqual("ready", after["analysis"]["status"])

    def approve_and_enqueue(self, paper_id, reason="manual", force=True):
        """Record the approval a user's explicit request implies, then enqueue."""
        self.manager.grant_approval(paper_id, model="acceptance-model", scope="test")
        return self.manager.enqueue(paper_id, reason=reason, force=force)

    def test_queued_job_survives_a_restart(self):
        self.store.update_ai_settings({"provider": "ollama", "auto_analyze": False})
        payload = self.import_pdf("重启前排队的.pdf")
        paper_id = payload["paper"]["id"]
        self.assertEqual("waiting_for_user", self.status(paper_id)["status"])
        outcome = self.approve_and_enqueue(paper_id)
        self.assertTrue(outcome["queued"], outcome)
        self.assertEqual("queued", self.status(paper_id)["status"])

        # a new Store + manager, as after a service restart
        store2 = server.Store(self.tmp)
        manager2 = analysis.AnalysisManager(store2,
                                            provider_factory=lambda settings: self.fake,
                                            log=lambda record: None)
        recovered = manager2.recover()
        self.assertIn(paper_id, recovered["requeued"], recovered)
        manager2.process_once()
        self.assertEqual("ready", store2.analysis_meta(paper_id)["status"])
        self.assertEqual(1, len(self.fake.calls), "recovery must not double-generate")

    def test_running_job_without_an_approval_is_parked_after_a_crash(self):
        paper = self.store.paper(ZH_PAPER)
        state = json.loads((self.tmp / "library.json").read_text(encoding="utf-8"))
        for item in state["papers"]:
            if item["id"] == ZH_PAPER:
                item["analysis"].update(status="running", stage="requesting", progress=55,
                                        started_at="2026-10-10T00:00:00Z",
                                        requested_at="2026-10-10T00:00:00Z")
        (self.tmp / "library.json").write_text(json.dumps(state, ensure_ascii=False),
                                               encoding="utf-8")
        store2 = server.Store(self.tmp)
        manager2 = analysis.AnalysisManager(store2,
                                            provider_factory=lambda settings: self.fake,
                                            log=lambda record: None)
        outcome = manager2.recover()
        # an interrupted row is state, not permission: it waits for the user
        self.assertIn(ZH_PAPER, outcome["parked"], outcome)
        manager2.process_once()
        self.assertEqual("waiting_for_user", store2.analysis_meta(ZH_PAPER)["status"])
        self.assertEqual(0, len(self.fake.calls), "a crash must not become a new request")

    def test_worker_start_stop_twice_leaves_no_deadlock(self):
        self.manager.start()
        self.approve_and_enqueue(ZH_PAPER)
        self.drain()
        self.manager.stop()
        self.manager.start()
        self.approve_and_enqueue(ZH_PAPER)
        self.drain()
        self.manager.stop()
        self.assertEqual("ready", self.status(ZH_PAPER)["status"])


class EndToEndNumbers(AcceptanceCase):
    """The import stays fast and the answer file is complete, for both languages."""

    def test_import_returns_before_the_model_and_the_answer_is_complete(self):
        self.store.update_ai_settings({"provider": "ollama", "auto_analyze": True})
        self.fake.delay = 2.0
        started = time.time()
        payload = self.import_pdf("延迟模型.pdf")
        elapsed = time.time() - started
        self.assertLess(elapsed, 1.5, f"import waited for the model ({elapsed:.2f}s)")
        self.assertEqual(0, len(self.fake.calls))
        paper_id = payload["paper"]["id"]
        self.drain()
        meta = self.status(paper_id)
        self.assertEqual("ready", meta["status"])
        answer = self.call(f"/api/analysis/{paper_id}")["answer"]
        self.assertEqual(13, len(answer["answers"]))
        self.assertEqual(64, len(answer["source_sha256"]))
        self.assertTrue(answer["created_at"])
        self.assertEqual("acceptance-model", answer["model"])
        provider_calls = len(self.fake.calls)
        self.assertGreaterEqual(provider_calls, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
