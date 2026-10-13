"""Phase 3: the analysis endpoints over HTTP.

Proves the import path returns before any model work happens and that the manager,
settings and status endpoints agree with the library on disk.

    python -m unittest discover -s tests -p "test_analysis_api.py" -v
"""
from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from baseline_helpers import PROJECT_ROOT, fresh_library_v1

sys.path.insert(0, str(PROJECT_ROOT))

import analysis      # noqa: E402
import providers     # noqa: E402
import server        # noqa: E402
from card_fixtures import build_card, pages_from_prompt   # noqa: E402
from tests.pdf_fixtures import write_pdf, zh_document    # noqa: E402

CARD_CACHE = {}


def card_for(paper_id):
    """A valid reading card whose quotes come from the bundled paper's own text layer."""
    if paper_id not in CARD_CACHE:
        import tempfile

        import extraction
        from storage import DataStore
        pdf = PROJECT_ROOT / "data" / "papers" / f"{paper_id}.pdf"
        extractor = extraction.PdfTextExtractor(
            files=DataStore(Path(tempfile.mkdtemp(prefix="card-cache-"))).ensure_directories(),
            prefer=["builtin"])
        result = extractor.extract(pdf, paper_id=paper_id)
        CARD_CACHE[paper_id] = build_card(result.pages)
    return CARD_CACHE[paper_id]


class AnalysisApiCase(unittest.TestCase):
    """A real Store + manager behind a throwaway HTTP server on a free port."""

    def setUp(self):
        import contextlib
        import socket

        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.tmp = Path(tempfile.mkdtemp(prefix="analysis-api-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        fresh_library_v1(self.tmp)
        self.store = server.Store(self.tmp)
        # A provider must be configured for jobs to run; the adapter itself is the fake.
        # auto_analyze stays off: the AI reference is only fetched on explicit request.
        self.store.update_ai_settings({"provider": "ollama", "model": "fake-model",
                                       "auto_analyze": False})
        self.fake = providers.FakeProvider(script=["success"],
                                           payload=card_for("medical-2026-zang"))
        # a synthetic PDF cannot be answered with the bundled paper's quotes: build the
        # card from whatever text the manager extracted for the paper in question.
        self.fake.payload_for = lambda request, payload: (
            build_card(pages_from_prompt(request.user)) or payload)
        self.manager = analysis.AnalysisManager(self.store,
                                                provider_factory=lambda settings: self.fake,
                                                log=lambda record: None)
        # A real session, obtained the way the page does: spend a one-time capability. The
        # body token is gone, so the tests exercise the same credential path as the browser.
        import threading

        import session as session_module
        self.sessions = session_module.SessionStore()
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        httpd = server.BoundedHTTPServer(
            ("127.0.0.1", port),
            server.handler_for(self.store, "", self.manager, sessions=self.sessions),
            max_concurrency=32)
        httpd.daemon_threads = True
        self.thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(httpd.shutdown)
        self.base = f"http://127.0.0.1:{port}"
        capability = self.sessions.issue_bootstrap(source="test")
        request = Request(self.base + "/api/session/bootstrap",
                          data=json.dumps({"capability": capability}).encode(),
                          method="POST",
                          headers={"Content-Type": "application/json",
                                   "Host": f"127.0.0.1:{port}"})
        with urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
            cookie = response.headers.get("Set-Cookie", "").split(";", 1)[0]
        self.auth = {"Cookie": cookie, "X-CSRF-Token": payload["csrf"]}
        self.token = ""

    def post(self, path, body, expect=200, headers=None):
        request = Request(self.base + path, data=json.dumps(body).encode(),
                          method="POST", headers={"Content-Type": "application/json",
                                                  **self.auth, **(headers or {})})
        try:
            with urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            payload = json.loads(exc.read().decode("utf-8", "replace"))
            if expect is None:
                return {"status": exc.code, **payload}
            self.assertEqual(expect, exc.code, payload)
            return payload

    def get(self, path):
        request = Request(self.base + path, headers=self.auth)
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))

    def pdf_bytes(self, paper_id):
        return (self.tmp / "papers" / f"{paper_id}.pdf").read_bytes()

    def script(self, *outcomes):
        """Replace the fake's script (the last outcome repeats)."""
        self.fake.script = list(outcomes)
        self.fake._outcome = self.fake.script.pop(0) if self.fake.script else outcomes[-1]

    def drain(self, timeout=60):
        """Run the queue to completion in this thread, whatever the worker is doing."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.manager.process_once()
            depth = self.manager.queue_depth()
            if not depth["queued"] and not depth["running"]:
                return
            time.sleep(0.02)

    def wait_status(self, paper_id, statuses, timeout=20):
        """Wait for a terminal status *and* the final stage.

        `ready` is written together with the answer file while the stage is still
        `saving`; the `done` transition follows immediately after, so a test that looks
        only at the status can catch the job one step early.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            meta = self.store.analysis_meta(paper_id)
            if meta["status"] in statuses:
                if meta["status"] != "ready" or meta.get("stage") in ("done", ""):
                    return meta
            time.sleep(0.05)
        return self.store.analysis_meta(paper_id)


class ImportReturnsImmediately(AnalysisApiCase):
    def test_import_does_not_wait_for_the_model(self):
        """With auto_analyze on, the import queues the job and returns before any model call."""
        self.store.update_ai_settings({"auto_analyze": True})
        blob = self.pdf_bytes("medical-2026-zang")
        started = time.time()
        request = Request(self.base + "/api/import", data=blob, method="POST",
                          headers=dict(self.auth, **{"X-File-Name": "imported.pdf"}))
        with urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
        elapsed = time.time() - started
        self.assertLess(elapsed, 2.0, "import blocked on the model")
        paper_id = payload["paper"]["id"]
        self.assertEqual("queued", payload["paper"]["analysis"]["status"])
        self.assertEqual(0, len(self.fake.calls), "no provider call during import")
        self.assertEqual("queued", self.get(f"/api/analysis/{paper_id}")["analysis"]["status"])

    def test_import_without_explicit_request_waits_for_the_user(self):
        """Default settings: a new paper waits for the user, nothing is sent to a model."""
        blob = self.pdf_bytes("medical-2026-zang")
        request = Request(self.base + "/api/import", data=blob, method="POST",
                          headers=dict(self.auth, **{"X-File-Name": "manual.pdf"}))
        with urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual("waiting_for_user", payload["paper"]["analysis"]["status"])
        self.assertIn("点击", payload["paper"]["analysis"]["message"])
        self.assertEqual(0, len(self.fake.calls))
        self.assertEqual([], self.manager.queue_depth()["queued"])

    def test_import_without_provider_marks_waiting(self):
        self.store.update_ai_settings({"provider": "none", "model": "", "auto_analyze": True})
        blob = self.pdf_bytes("medical-2026-zang")
        request = Request(self.base + "/api/import", data=blob, method="POST",
                          headers=dict(self.auth, **{"X-File-Name": "waiting.pdf"}))
        with urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual("waiting_for_provider", payload["paper"]["analysis"]["status"])
        self.assertIn("尚未配置", payload["paper"]["analysis"]["message"])
        # the paper is still fully readable
        paper_id = payload["paper"]["id"]
        self.assertTrue(self.store.files.has_pdf(paper_id))
        self.assertEqual(13, len(payload["paper"]["fields"]))


class StatusEndpoints(AnalysisApiCase):
    def test_status_list_and_health(self):
        payload = self.get("/api/analysis")
        self.assertIn("statuses", payload)
        self.assertEqual(3, len(payload["statuses"]))
        self.assertIn("queue", payload)
        self.assertIsNotNone(payload["health"])

    def test_an_unconfirmed_target_is_not_probed(self):
        """A health check carries the credential, so it waits for the target to be confirmed.

        The fixture configures the provider through the data layer, which is not the same act
        as typing an address into the settings screen; until the address is confirmed, nothing
        is contacted.
        """
        payload = self.get("/api/analysis")
        self.assertFalse(payload["health"]["ok"])
        self.assertEqual("disabled", payload["health"]["provider"])
        self.assertEqual("consent_required", payload["health"]["category"])
        self.assertEqual([], self.fake.calls)

    def test_a_confirmed_target_is_probed(self):
        self.manager.approve_target()
        payload = self.get("/api/analysis")
        self.assertTrue(payload["health"]["ok"], payload["health"])
        self.assertEqual("fake", payload["health"]["provider"])

    def test_manual_run_and_result(self):
        outcome = self.post("/api/analysis-run", {"paper_id": "medical-2026-zang"})
        self.assertEqual("queued", outcome["run"]["status"])
        self.drain()
        payload = self.get("/api/analysis/medical-2026-zang")
        self.assertEqual("ready", payload["analysis"]["status"])
        self.assertEqual(13, len(payload["answer"]["answers"]))
        self.assertTrue(payload["answer"]["answers"]["question"]["evidence"][0]["verified"])
        self.assertGreaterEqual(payload["answer"]["verification"]["verified_quotes"], 4)
        self.assertEqual("medical-2026-zang.json", payload["analysis"]["answer_file"])

    def test_run_rejects_unknown_paper(self):
        result = self.post("/api/analysis-run", {"paper_id": "nope-nope-nope"}, expect=400)
        self.assertIn("找不到", result["error"])

    def test_cancel_endpoint(self):
        self.post("/api/analysis-run", {"paper_id": "medical-2026-zang"})
        payload = self.post("/api/analysis-cancel", {"paper_id": "medical-2026-zang"})
        self.assertTrue(payload["cancelled"])
        self.assertEqual("cancelled", payload["analysis"]["status"])

    def test_failure_then_success_through_the_api(self):
        self.script("invalid_response", "success")
        self.post("/api/analysis-run", {"paper_id": "medical-2026-zang"})
        self.manager.process_once()
        self.assertEqual("failed", self.store.analysis_meta("medical-2026-zang")["status"])
        self.assertEqual("invalid_response", self.store.analysis_meta("medical-2026-zang")["error_code"])
        self.post("/api/analysis-run", {"paper_id": "medical-2026-zang", "force": True})
        self.manager.process_once()
        self.assertEqual("ready", self.store.analysis_meta("medical-2026-zang")["status"])

    def test_ai_settings_endpoint_reports_health_without_leaking_keys(self):
        payload = self.post("/api/ai-settings", {"ai": {"provider": "ollama", "model": "fake-model",
                                                       "api_key_env": "PAPER_READER_TEST_KEY"}})
        self.assertEqual("ollama", payload["ai"]["provider"])
        self.assertIn("health", payload)
        self.assertNotIn("key", json.dumps(payload["health"]).lower().replace("apikey", ""))


class WorkerIntegration(AnalysisApiCase):
    def test_background_worker_reaches_ready(self):
        self.manager.start()
        self.addCleanup(self.manager.stop)
        self.post("/api/analysis-run", {"paper_id": "medical-2026-zang"})
        meta = self.wait_status("medical-2026-zang", {"ready", "failed"})
        self.assertEqual("ready", meta["status"])
        self.assertEqual("done", meta["stage"])
        self.assertEqual(100, meta["progress"])

    def test_restart_mid_run_needs_a_fresh_approval(self):
        """A crash leaves state, not permission: the paper waits for the user.

        The security release changed this on purpose. An interrupted run whose approval is
        gone must not spend money again on its own; an approval that is still valid for the
        same target does resume, and that case is covered by the recovery tests.
        """
        state = json.loads((self.tmp / "library.json").read_text(encoding="utf-8"))
        for paper in state["papers"]:
            if paper["id"] == "medical-2026-zang":
                paper["analysis"].update(status="running", stage="requesting", progress=55,
                                         started_at="2026-10-10T00:00:00Z",
                                         updated_at="2026-10-10T00:00:00Z")
        (self.tmp / "library.json").write_text(json.dumps(state, ensure_ascii=False, indent=2),
                                               encoding="utf-8")

        store = server.Store(self.tmp)
        manager = analysis.AnalysisManager(store, provider_factory=lambda settings: self.fake,
                                          log=lambda record: None)
        report = manager.recover()
        self.assertIn("medical-2026-zang", report["parked"], report)
        manager.process_once()
        self.assertEqual("waiting_for_user", store.analysis_meta("medical-2026-zang")["status"])
        self.assertEqual(0, len(self.fake.calls), "a crash must not trigger a request")
        # the user's explicit request then completes the job
        manager.grant_approval("medical-2026-zang", model="fake-model", scope="test")
        manager.enqueue("medical-2026-zang", reason="manual")
        manager.process_once()
        self.assertEqual("ready", store.analysis_meta("medical-2026-zang")["status"])

    def test_human_save_survives_a_background_run(self):
        paper_id = "fertility-2026-coffey"
        self.fake.payload = card_for(paper_id)
        self.post("/api/analysis-run", {"paper_id": paper_id})
        self.manager.start()
        self.addCleanup(self.manager.stop)
        paper = self.store.paper(paper_id)
        self.store.save_paper({"id": paper_id, "revision": paper["revision"],
                               "patch": {"fields": dict(paper["fields"], summary="人工复述"),
                                         "steps": [True, True, False, False]}})
        self.wait_status(paper_id, {"ready", "failed"})
        final = self.store.paper(paper_id)
        self.assertEqual("人工复述", final["fields"]["summary"])
        self.assertEqual([True, True, False, False], final["steps"])
        self.assertEqual("ready", final["analysis"]["status"])


class EndToEndWithARealPdf(AnalysisApiCase):
    """Import a synthetic PDF with a real text layer and run the whole pipeline."""

    def test_import_extract_chunk_call_and_answer(self):
        pdf = self.tmp / "synthetic.pdf"
        write_pdf(pdf, zh_document())
        blob = pdf.read_bytes()
        # X-File-Name travels in a header, so non-ASCII names are percent-encoded.
        request = Request(self.base + "/api/import", data=blob, method="POST",
                          headers=dict(self.auth,
                                       **{"X-File-Name": "%E5%90%88%E6%88%90%E8%AE%BA%E6%96%87.pdf"}))
        with urlopen(request, timeout=30) as response:
            paper = json.loads(response.read().decode("utf-8"))["paper"]
        self.assertEqual("合成论文", paper["title"])
        self.assertEqual("zh", paper["language"])
        self.assertEqual("waiting_for_user", paper["analysis"]["status"])
        # the user asks for the reference; only now does the pipeline run
        run = self.post("/api/analysis-run", {"paper_id": paper["id"]})
        self.assertEqual("queued", run["run"]["status"])
        self.drain()
        meta = self.store.analysis_meta(paper["id"])
        self.assertEqual("ready", meta["status"], meta["error_message"])
        self.assertEqual("zh", meta["language"])
        self.assertEqual(1, len(self.fake.calls))
        sent = self.fake.calls[0]
        self.assertGreater(sent["request_chars"], 100, "the prompt must carry real page text")
        payload, error = self.store.analysis_answer(paper["id"])
        self.assertIsNone(error)
        self.assertEqual(13, len(payload["answers"]))
        self.assertTrue(self.store.files.has_extracted(paper["id"]),
                        "the extracted text cache is reused by later phases")


if __name__ == "__main__":
    unittest.main(verbosity=2)
