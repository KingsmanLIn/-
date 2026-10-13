"""Phase 5: the HTTP surface the app uses, plus its security envelope.

Every endpoint is checked for success, missing token, unknown paper, not-ready and
retry/failure paths. Secrets are checked against the library, the backup, the log and
the served HTML. Obsidian sync and /api/save are regression-tested here too.

    python -m unittest discover -s tests -p "test_service_api.py" -v
"""
from __future__ import annotations

import reading_card
import contextlib
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
import server        # noqa: E402
from card_fixtures import build_card  # noqa: E402

PAPER = "medical-2026-zang"
OTHER = "finance-2021-zhang"


def card_for(paper_id):
    import extraction
    from storage import DataStore
    directory = Path(tempfile.mkdtemp(prefix="phase5-card-"))
    extractor = extraction.PdfTextExtractor(
        files=DataStore(directory).ensure_directories(), prefer=["builtin"])
    result = extractor.extract(PROJECT_ROOT / "data" / "papers" / f"{paper_id}.pdf",
                               paper_id=paper_id)
    return build_card(result.pages)


class ServiceCase(unittest.TestCase):
    provider = "ollama"
    model = "fake-model"
    auto_analyze = False
    delay = 0.0

    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.tmp = Path(tempfile.mkdtemp(prefix="phase5-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        fresh_library_v1(self.tmp)
        self.store = server.Store(self.tmp)
        self.store.update_ai_settings({"provider": self.provider, "model": self.model,
                                       "auto_analyze": self.auto_analyze})
        self.events = []
        self.fake = providers.FakeProvider(script=["success"], payload=card_for(PAPER),
                                           delay=self.delay)
        self.manager = analysis.AnalysisManager(self.store,
                                                provider_factory=lambda settings: self.fake,
                                                log=self.events.append)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", port),
                                                server.handler_for(self.store, "tok",
                                                                   self.manager))
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.shutdown)
        self.base = f"http://127.0.0.1:{port}"

    # -- raw http helpers --------------------------------------------------
    def raw(self, path, method="GET", body=None, token="tok", headers=None, timeout=30):
        data = None
        head = dict(headers or {})
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            head.setdefault("Content-Type", "application/json")
        if token is not None:
            head["X-Reader-Token"] = token
        request = Request(self.base + path, data=data, method=method, headers=head)
        try:
            with urlopen(request, timeout=timeout) as response:
                payload = response.read()
                return response.status, payload, dict(response.headers)
        except HTTPError as exc:
            return exc.code, exc.read(), dict(exc.headers)

    def json_of(self, path, method="GET", body=None, token="tok", expect=200, **kwargs):
        status, payload, headers = self.raw(path, method, body, token, **kwargs)
        if expect is not None:
            self.assertEqual(expect, status,
                             f"{method} {path} -> {status}: {payload[:300]!r}")
        try:
            return json.loads(payload.decode("utf-8")), headers
        except (UnicodeDecodeError, json.JSONDecodeError):
            return payload, headers

    def drain(self, timeout=60):
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.manager.process_once()
            depth = self.manager.queue_depth()
            if not depth["queued"] and not depth["running"]:
                return
            time.sleep(0.02)

    def import_pdf(self, name="新论文.pdf", data=None):
        blob = data if data is not None else \
            (self.tmp / "papers" / f"{PAPER}.pdf").read_bytes()
        # X-File-Name travels in a header, so non-ASCII names are percent-encoded
        from urllib.parse import quote
        return self.json_of("/api/import", "POST", blob,
                            headers={"X-File-Name": quote(name, safe=""),
                                     "Content-Type": "application/pdf"})



class SecurityEnvelope(ServiceCase):
    def test_missing_token_is_refused_everywhere(self):
        for path, method, body in (("/api/state", "GET", None),
                                   ("/api/analysis", "GET", None),
                                   (f"/api/analysis/{PAPER}/status", "GET", None),
                                   (f"/api/analysis/{PAPER}", "GET", None),
                                   (f"/api/analysis/{PAPER}/generate", "POST", {}),
                                   ("/api/settings", "POST", {"vault_path": "", "sync_enabled": False}),
                                   ("/api/ai-settings", "POST", {"ai": {"provider": "none"}}),
                                   ("/api/save", "POST", {"id": PAPER, "revision": 0, "patch": {}})):
            status, payload, _ = self.raw(path, method, body, token=None)
            self.assertEqual(403, status, f"{method} {path} -> {status} {payload[:120]!r}")

    def test_wrong_token_is_refused(self):
        status, _payload, _ = self.raw("/api/state", token="not-the-token")
        self.assertEqual(403, status)

    def test_foreign_host_header_is_refused(self):
        status, payload, _ = self.raw("/api/state", headers={"Host": "evil.example.com"})
        self.assertEqual(403, status)
        self.assertIn("仅支持本机访问".encode("utf-8"), payload)

    def test_cross_site_origin_is_refused(self):
        status, _payload, _ = self.raw("/api/state", headers={"Origin": "http://evil.example.com"})
        self.assertEqual(403, status)

    def test_local_origin_is_accepted(self):
        status, _payload, _ = self.raw("/api/state", headers={"Origin": self.base})
        self.assertEqual(200, status)

    def test_cross_site_fetch_metadata_is_refused(self):
        status, _payload, _ = self.raw("/api/state", headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(403, status)

    def test_security_headers_are_intact(self):
        _status, _payload, headers = self.raw("/api/health", token=None)
        policy = headers.get("Content-Security-Policy", "")
        for directive in ("default-src 'self'", "script-src 'self' 'wasm-unsafe-eval'",
                          "object-src 'none'", "frame-ancestors 'none'", "connect-src 'self'"):
            self.assertIn(directive, policy)
        self.assertEqual("DENY", headers.get("X-Frame-Options"))
        self.assertEqual("nosniff", headers.get("X-Content-Type-Options"))
        self.assertEqual("no-referrer", headers.get("Referrer-Policy"))

    def test_health_needs_no_token_but_leaks_nothing(self):
        status, payload, _ = self.raw("/api/health", token=None)
        self.assertEqual(200, status)
        body = json.loads(payload.decode("utf-8"))
        self.assertEqual({"app", "version", "schema_version", "root", "data"}, set(body))
        self.assertNotIn("token", json.dumps(body))
        self.assertNotIn("tok", json.dumps(body))


class ImportEndpoint(ServiceCase):
    def test_import_returns_analysis_status_immediately(self):
        started = time.time()
        payload, _ = self.import_pdf()
        elapsed = time.time() - started
        paper = payload["paper"]
        self.assertIn("analysis", paper)
        self.assertIn(paper["analysis"]["status"], ("queued", "waiting_for_user",
                                                    "waiting_for_provider"))
        self.assertEqual(0, len(self.fake.calls), "no model call during import")
        self.assertLess(elapsed, 2.0)
        self.assertEqual(64, len(paper["analysis"]["source_sha256"]))

    def test_import_time_does_not_follow_model_time(self):
        """A slow model must not slow the import response down."""
        self.fake.delay = 3.0
        started = time.time()
        payload, _ = self.import_pdf("慢模型论文.pdf")
        import_seconds = time.time() - started
        self.assertLess(import_seconds, 1.5, f"import waited for the model ({import_seconds:.2f}s)")
        self.assertEqual(0, len(self.fake.calls))
        paper_id = payload["paper"]["id"]
        self.assertEqual("waiting_for_user", self.store.analysis_meta(paper_id)["status"])
        # the job does not depend on the response having been sent; the explicit request
        # carries the approval the send gate requires
        self.manager.grant_approval(paper_id, model="fake-model", scope="test")
        self.manager.enqueue(paper_id, reason="manual", force=True)
        self.manager.process_once()
        self.assertEqual("ready", self.store.analysis_meta(paper_id)["status"])

    def test_import_rejects_non_pdf(self):
        payload, _ = self.json_of("/api/import", "POST", b"not a pdf",
                                  headers={"X-File-Name": "bad.pdf"}, expect=400)
        self.assertIn("PDF", payload["error"])

    def test_import_without_provider_returns_waiting_status(self):
        self.store.update_ai_settings({"provider": "none", "model": "", "auto_analyze": True})
        payload, _ = self.import_pdf("无Provider.pdf")
        self.assertEqual("waiting_for_provider", payload["paper"]["analysis"]["status"])


class StatusEndpoint(ServiceCase):
    def test_status_never_contains_the_answer(self):
        self.json_of(f"/api/analysis/{PAPER}/generate", "POST", {"force": True})
        self.drain()
        payload, _ = self.json_of(f"/api/analysis/{PAPER}/status")
        for key in ("status", "stage", "progress", "requested_at", "started_at",
                    "completed_at", "updated_at", "error_code", "error_summary"):
            self.assertIn(key, payload)
        self.assertEqual("ready", payload["status"])
        self.assertEqual(100, payload["progress"])
        self.assertNotIn("answer", payload)
        self.assertNotIn("answers", payload)
        self.assertNotIn("evidence", json.dumps(payload))
        self.assertTrue(payload["has_answer"])

    def test_status_for_unknown_paper(self):
        payload, _ = self.json_of("/api/analysis/nope-nope-nope/status", expect=400)
        self.assertIn("找不到", payload["error"])

    def test_status_for_invalid_id(self):
        payload, _ = self.json_of("/api/analysis/BAD_ID/status", expect=400)
        self.assertIn("无效", payload["error"])

    def test_status_before_any_run(self):
        payload, _ = self.json_of(f"/api/analysis/{PAPER}/status")
        self.assertEqual("none", payload["status"])
        self.assertEqual(0, payload["progress"])
        self.assertFalse(payload["has_answer"])
        self.assertTrue(payload["generated_at"])

    def test_status_reports_failure_summary_only(self):
        self.fake.payload = {"schema_version": 1, "overall": {}, "answers": {}, "terms": [],
                             "reader_questions": [], "warnings": []}
        self.json_of(f"/api/analysis/{PAPER}/generate", "POST", {"force": True})
        self.drain()
        payload, _ = self.json_of(f"/api/analysis/{PAPER}/status")
        self.assertEqual("failed", payload["status"])
        self.assertEqual("validation_failed", payload["error_code"])
        self.assertLessEqual(len(payload["error_summary"]), 200)
        self.assertNotIn("answers", payload)


class AnswerEndpoint(ServiceCase):
    def test_answer_requires_ready(self):
        payload, _ = self.json_of(f"/api/analysis/{PAPER}")
        self.assertEqual("none", payload["status"])
        self.assertIsNone(payload["answer"])
        self.assertFalse(payload["answer_available"])
        self.assertTrue(payload["reason"])

    def test_answer_after_generate(self):
        self.json_of(f"/api/analysis/{PAPER}/generate", "POST", {"force": True})
        self.drain()
        payload, _ = self.json_of(f"/api/analysis/{PAPER}")
        self.assertEqual("ready", payload["status"])
        self.assertTrue(payload["answer_available"])
        self.assertEqual(13, len(payload["answer"]["answers"]))
        self.assertEqual(reading_card.PROMPT_VERSION, payload["answer"]["prompt_version"])

    def test_answer_hidden_while_running(self):
        self.fake.delay = 0.6
        self.fake.enter_delay = False
        self.json_of(f"/api/analysis/{PAPER}/generate", "POST", {"force": True})
        worker = threading.Thread(target=self.manager.process_once)
        worker.start()
        time.sleep(0.15)
        payload, _ = self.json_of(f"/api/analysis/{PAPER}")
        self.assertIn(payload["status"], ("running", "queued"))
        self.assertIsNone(payload["answer"], "the answer must not be served mid-run")
        worker.join(30)

    def test_answer_for_unknown_paper(self):
        payload, _ = self.json_of("/api/analysis/nope-nope-nope", expect=400)
        self.assertIn("找不到", payload["error"])

    def test_unknown_subresource_is_404(self):
        status, _payload, _ = self.raw(f"/api/analysis/{PAPER}/whatever")
        self.assertEqual(404, status)


class GenerateEndpoint(ServiceCase):
    def test_generate_queues_and_can_be_polled(self):
        payload, _ = self.json_of(f"/api/analysis/{PAPER}/generate", "POST", {})
        self.assertEqual("queued", payload["run"]["status"])
        self.assertEqual("queued", payload["status"]["status"])
        self.drain()
        status, _ = self.json_of(f"/api/analysis/{PAPER}/status")
        self.assertEqual("ready", status["status"])

    def test_ready_is_not_regenerated_without_force(self):
        self.json_of(f"/api/analysis/{PAPER}/generate", "POST", {})
        self.drain()
        calls = len(self.fake.calls)
        payload, _ = self.json_of(f"/api/analysis/{PAPER}/generate", "POST", {})
        self.assertFalse(payload["run"]["queued"])
        self.assertEqual("already_ready", payload["run"]["reason"])
        self.drain()
        self.assertEqual(calls, len(self.fake.calls), "no extra model call without force")

    def test_force_regenerates(self):
        self.json_of(f"/api/analysis/{PAPER}/generate", "POST", {})
        self.drain()
        calls = len(self.fake.calls)
        payload, _ = self.json_of(f"/api/analysis/{PAPER}/generate", "POST", {"force": True})
        self.assertTrue(payload["run"]["queued"])
        self.drain()
        self.assertEqual(calls + 1, len(self.fake.calls))
        status, _ = self.json_of(f"/api/analysis/{PAPER}/status")
        self.assertEqual("ready", status["status"])

    def test_retry_after_failure(self):
        self.fake.payload = {"schema_version": 1, "overall": {}, "answers": {}, "terms": [],
                             "reader_questions": [], "warnings": []}
        self.json_of(f"/api/analysis/{PAPER}/generate", "POST", {})
        self.drain()
        status, _ = self.json_of(f"/api/analysis/{PAPER}/status")
        self.assertEqual("failed", status["status"])
        self.fake.payload = card_for(PAPER)
        self.json_of(f"/api/analysis/{PAPER}/generate", "POST", {})
        self.drain()
        status, _ = self.json_of(f"/api/analysis/{PAPER}/status")
        self.assertEqual("ready", status["status"], status["error_summary"])

    def test_generate_for_unknown_paper(self):
        payload, _ = self.json_of("/api/analysis/nope-nope-nope/generate", "POST", {}, expect=400)
        self.assertIn("找不到", payload["error"])

    def test_cancel_keeps_the_compatibility_route(self):
        payload, _ = self.json_of(f"/api/analysis/{PAPER}/cancel", "POST", {})
        self.assertIn("cancelled", payload)


class SettingsEndpoint(ServiceCase):
    def test_settings_saves_ai_fields(self):
        payload, _ = self.json_of("/api/settings", "POST",
                                  {"vault_path": "", "sync_enabled": False,
                                   "provider": "lmstudio", "model": "local-model",
                                   "base_url": "http://127.0.0.1:1234/v1",
                                   "api_key_env": "LMSTUDIO_API_KEY",
                                   "auto_analyze": True, "max_concurrent_jobs": 2})
        self.assertEqual("lmstudio", payload["ai"]["provider"])
        self.assertEqual("local-model", payload["ai"]["model"])
        self.assertEqual("env:LMSTUDIO_API_KEY", payload["ai"]["api_key_env"])
        self.assertTrue(payload["ai"]["auto_analyze"])
        self.assertIn("health", payload)
        stored = json.loads((self.tmp / "library.json").read_text(encoding="utf-8"))
        self.assertEqual("lmstudio", stored["settings"]["ai"]["provider"])
        self.assertEqual("env:LMSTUDIO_API_KEY", stored["settings"]["ai"]["api_key_env"])

    def test_settings_rejects_key_values(self):
        payload, _ = self.json_of("/api/settings", "POST",
                                  {"vault_path": "", "sync_enabled": False,
                                   "api_key": "sk-live-abcdef123456"}, expect=400)
        self.assertIn("环境变量名", payload["error"])
        stored = (self.tmp / "library.json").read_text(encoding="utf-8")
        self.assertNotIn("sk-live", stored)

    def test_settings_rejects_invalid_provider(self):
        payload, _ = self.json_of("/api/settings", "POST",
                                  {"vault_path": "", "sync_enabled": False,
                                   "provider": "openai"}, expect=400)
        self.assertIn("Provider", payload["error"])

    def test_settings_never_echoes_a_secret_from_the_environment(self):
        import os
        os.environ["PAPER_READER_PHASE5_KEY"] = "sk-live-phase5-secret-123456"
        self.addCleanup(os.environ.pop, "PAPER_READER_PHASE5_KEY", None)
        payload, _ = self.json_of("/api/settings", "POST",
                                  {"vault_path": "", "sync_enabled": False,
                                   "provider": "ollama", "model": "m",
                                   "api_key_env": "PAPER_READER_PHASE5_KEY"})
        body = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("sk-live-phase5-secret-123456", body)
        self.assertEqual("env:PAPER_READER_PHASE5_KEY", payload["ai"]["api_key_env"])
        stored = (self.tmp / "library.json").read_text(encoding="utf-8")
        self.assertNotIn("sk-live-phase5-secret-123456", stored)

    def test_ai_settings_endpoint_still_works(self):
        payload, _ = self.json_of("/api/ai-settings", "POST",
                                  {"ai": {"provider": "ollama", "model": "m2"}})
        self.assertEqual("ollama", payload["ai"]["provider"])
        self.assertIn("health", payload)


class SecretIsolation(ServiceCase):
    """The key may live only in the environment or data/credentials.json."""

    def setUp(self):
        super().setUp()
        import os
        self.secret = "sk-live-phase5-secret-123456"
        os.environ["PAPER_READER_PHASE5_KEY"] = self.secret
        self.addCleanup(os.environ.pop, "PAPER_READER_PHASE5_KEY", None)

    def scan(self):
        found = {}
        for path in sorted(self.tmp.rglob("*")):
            if path.is_file():
                try:
                    blob = path.read_bytes()
                except OSError:
                    continue
                if self.secret.encode() in blob:
                    found[str(path.relative_to(self.tmp))] = len(blob)
        return found

    def test_secret_never_lands_in_the_library_backup_log_or_html(self):
        self.json_of("/api/settings", "POST",
                     {"vault_path": "", "sync_enabled": False, "provider": "ollama",
                      "model": "fake-model", "api_key_env": "PAPER_READER_PHASE5_KEY"})
        self.json_of(f"/api/analysis/{PAPER}/generate", "POST", {"force": True})
        self.drain()

        # library and everything else under data/
        self.assertEqual({}, self.scan(), "the secret leaked into the data directory")

        # backup archive
        status, blob, _headers = self.raw("/api/backup")
        self.assertEqual(200, status)
        with zipfile.ZipFile(BytesIO(blob)) as archive:
            for name in archive.namelist():
                self.assertNotIn(self.secret, archive.read(name).decode("utf-8", "replace"),
                                 f"secret inside backup member {name}")

        # logs
        self.assertNotIn(self.secret, json.dumps(self.events, ensure_ascii=False))

        # served HTML
        status, html, _headers = self.raw("/", token=None)
        self.assertEqual(200, status)
        self.assertNotIn(self.secret, html.decode("utf-8", "replace"))

        # provider objects keep the value out of their descriptions
        adapter = providers.build_adapter(self.store.ai_settings(), self.store.files)
        self.assertNotIn(self.secret, json.dumps(adapter.describe(), ensure_ascii=False))

    def test_redacted_failure_log_has_no_secret(self):
        self.fake.payload = {"schema_version": 1, "overall": {}, "answers": {}, "terms": [],
                             "reader_questions": [], "warnings": []}
        self.json_of(f"/api/analysis/{PAPER}/generate", "POST", {"force": True})
        self.drain()
        self.assertEqual({}, self.scan())


class RegressionWithObsidian(ServiceCase):
    """Existing behaviour must keep working: /api/save, revisions, sync, export."""

    def test_save_and_revision_conflict(self):
        paper = self.store.paper(PAPER)
        payload, _ = self.json_of("/api/save", "POST",
                                  {"id": PAPER, "revision": paper["revision"],
                                   "patch": {"fields": dict(paper["fields"],
                                                            question="人工研究问题"),
                                             "steps": [True, False, False, False]}})
        self.assertEqual(1, payload["paper"]["revision"])
        self.assertEqual("人工研究问题", payload["paper"]["fields"]["question"])
        stale, _ = self.json_of("/api/save", "POST",
                                {"id": PAPER, "revision": paper["revision"],
                                 "patch": {"fields": dict(paper["fields"])}}, expect=409)
        self.assertIn("另一个窗口", stale["error"])

    def test_ai_metadata_is_not_writable_through_save(self):
        before = json.dumps(self.store.analysis_meta(PAPER), ensure_ascii=False, sort_keys=True)
        paper = self.store.paper(PAPER)
        self.json_of("/api/save", "POST",
                     {"id": PAPER, "revision": paper["revision"],
                      "patch": {"fields": dict(paper["fields"]),
                                "analysis": {"status": "ready"}}})
        after = json.dumps(self.store.analysis_meta(PAPER), ensure_ascii=False, sort_keys=True)
        self.assertEqual(before, after)

    def test_obsidian_sync_writes_notes_and_index_and_keeps_manual_text(self):
        vault = self.tmp / "vault"
        (vault / "论文研读助手").mkdir(parents=True)
        note = vault / "论文研读助手" / self.store.paper(PAPER)["note_name"]
        note.write_text(server.START + "\n旧内容\n" + server.END + "\n\n## 我的补充\n\n人工写在同步区外。\n",
                        encoding="utf-8")
        payload, _ = self.json_of("/api/settings", "POST",
                                  {"vault_path": str(vault), "sync_enabled": True})
        self.assertTrue(payload["settings"]["sync_enabled"])
        self.assertEqual("", payload["settings"]["sync_error"])
        text = note.read_text(encoding="utf-8")
        self.assertIn("人工写在同步区外。", text)
        self.assertIn("分级诊疗", text, "the note body should come from the paper record")
        index = vault / "论文研读助手" / "00 文献索引.md"
        self.assertTrue(index.is_file())
        self.assertIn("文献索引", index.read_text(encoding="utf-8"))
        backups = list((vault / "论文研读助手" / ".同步备份").glob("*"))
        self.assertTrue(backups, "the previous note version is kept")

    def test_sync_refuses_a_tampered_marker_and_keeps_the_file(self):
        vault = self.tmp / "vault2"
        (vault / "论文研读助手").mkdir(parents=True)
        note = vault / "论文研读助手" / self.store.paper(PAPER)["note_name"]
        note.write_text("标记被删掉了\n", encoding="utf-8")
        payload, _ = self.json_of("/api/settings", "POST",
                                  {"vault_path": str(vault), "sync_enabled": True})
        self.assertIn("自动区域标记", payload["settings"]["sync_error"])
        self.assertEqual("标记被删掉了\n", note.read_text(encoding="utf-8"))

    def test_export_and_pdf_download_still_work(self):
        status, markdown, headers = self.raw(f"/api/export/{PAPER}")
        self.assertEqual(200, status)
        self.assertIn("text/markdown", headers.get("Content-Type", ""))
        self.assertIn("阅读卡", markdown.decode("utf-8"))
        status, blob, headers = self.raw(f"/api/pdf/{PAPER}")
        self.assertEqual(200, status)
        self.assertEqual("application/pdf", headers.get("Content-Type"))
        self.assertTrue(blob.startswith(b"%PDF-"))

    def test_backup_and_restore_round_trip(self):
        status, blob, _ = self.raw("/api/backup")
        self.assertEqual(200, status)
        with zipfile.ZipFile(BytesIO(blob)) as archive:
            self.assertIn("manifest.json", archive.namelist())
        target = Path(tempfile.mkdtemp(prefix="phase5-restore-"))
        self.addCleanup(shutil.rmtree, target, ignore_errors=True)
        fresh = server.Store(target)
        self.assertEqual(len(self.store.state["papers"]), fresh.restore(blob))
        self.assertEqual("none", fresh.analysis_meta(PAPER)["status"])


class ReadinessAfterRestart(ServiceCase):
    def test_status_survives_a_restart(self):
        self.json_of(f"/api/analysis/{PAPER}/generate", "POST", {"force": True})
        self.drain()
        store = server.Store(self.tmp)
        manager = analysis.AnalysisManager(store, provider_factory=lambda s: self.fake,
                                           log=lambda record: None)
        request = Request(f"http://127.0.0.1:{self.httpd.server_port}"
                          f"/api/analysis/{PAPER}/status",
                          headers={"X-Reader-Token": "tok"})
        with urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual("ready", payload["status"])
        self.assertTrue(payload["has_answer"])
        self.assertTrue(manager.statuses()[PAPER]["answer_file"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
