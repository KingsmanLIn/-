"""Phase 3: provider adapters and the analysis job state machine.

Uses FakeProvider for the four outcomes (success / timeout / invalid JSON / refusal) and
never contacts a real model.

    python -m unittest discover -s tests -p "test_analysis_jobs.py" -v
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from baseline_helpers import PROJECT_ROOT, fresh_library_v1

sys.path.insert(0, str(PROJECT_ROOT))

import analysis        # noqa: E402
import credentials     # noqa: E402
import job_policy      # noqa: E402
import providers       # noqa: E402
import schema          # noqa: E402
import secret_guard    # noqa: E402
import server          # noqa: E402
from chunker import Chunker              # noqa: E402
from pdftext import Page                 # noqa: E402
from card_fixtures import build_card     # noqa: E402
import endpoint_policy as endpoint_policy_mod  # noqa: E402
from transport_double import RecordingTransport, error_response, json_response  # noqa: E402

PAPER_A = "medical-2026-zang"


def fake_pages(pages=3, chars=1500):
    return [Page(pdf_page=number, text=f"第 {number} 页正文内容。" * (chars // 10),
                 char_count=chars, has_text=True) for number in range(1, pages + 1)]


class FakeExtractor:
    """Stands in for PdfTextExtractor: no PDF parsing in the state-machine tests."""

    def __init__(self, pages=None, ok=True, code="ok"):
        self.pages = pages or fake_pages()
        self.ok = ok
        self.code = code
        self.calls = 0

    def extract(self, path, paper_id=None, use_cache=None):
        self.calls += 1
        return type("Result", (), {
            "ok": self.ok, "code": self.code, "message": "fake extraction",
            "page_count": len(self.pages), "pages": self.pages, "backend": "fake",
            "text_pages": self.pages, "source_sha256": "ab" * 32, "cached": False,
        })()

    def chunks(self, result, max_chars=6000, overlap_ratio=0.1, cleaned=False, document_id=""):
        return Chunker(max_chars=max_chars).chunk(self.pages, document_id)


def answer_payload(pages=None, not_found=("robustness",)):
    """A phase-4 reading card built from the fake pages used by the manager tests."""
    return build_card(pages or fake_pages(), not_found=not_found)


class ManagerCase(unittest.TestCase):
    """Shared fixture: a real Store on a temporary copy of the shipped v1 library."""

    provider_script = ["success"]

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jobs-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        fresh_library_v1(self.tmp)
        self.store = server.Store(self.tmp)
        self.extractor = FakeExtractor()
        self.fake = providers.FakeProvider(script=list(self.provider_script),
                                           payload=answer_payload())
        self.manager = analysis.AnalysisManager(
            self.store,
            provider_factory=lambda settings: self.fake,
            extractor_factory=lambda: self.extractor,
            running_timeout=30, max_attempts=2,
            log=lambda record: None)
        self.store.update_ai_settings({"provider": "ollama", "model": "fake-model"})

    def meta(self, paper_id=PAPER_A):
        return self.store.analysis_meta(paper_id)

    def status(self, paper_id=PAPER_A):
        return self.meta(paper_id)["status"]


class QueueingStates(ManagerCase):
    def test_new_paper_starts_without_analysis(self):
        self.assertEqual("none", self.meta("fertility-2026-coffey")["status"])

    def test_enqueue_sets_queued_and_returns_immediately(self):
        started = time.time()
        outcome = self.manager.enqueue(PAPER_A, reason="manual")
        elapsed = time.time() - started
        self.assertTrue(outcome["queued"])
        self.assertEqual("queued", self.status())
        self.assertLess(elapsed, 0.5, "enqueue must not wait for the model")
        self.assertIn(PAPER_A, self.manager.queue_depth()["queued"])

    def test_duplicate_enqueue_is_idempotent(self):
        self.manager.enqueue(PAPER_A, reason="manual")
        again = self.manager.enqueue(PAPER_A, reason="manual")
        self.assertEqual("queued", again["status"])
        self.assertEqual(1, self.manager.queue_depth()["queued"].count(PAPER_A),
                         "the id may only appear once in the worker queue")
        self.manager.process_once()
        self.assertEqual("ready", self.status())
        self.assertEqual(1, len(self.fake.calls), "a duplicate enqueue must not run twice")

    def test_ready_is_not_requeued_unless_forced(self):
        self.manager.enqueue(PAPER_A, reason="manual")
        self.manager.process_once()
        self.assertEqual("ready", self.status())
        again = self.manager.enqueue(PAPER_A, reason="manual")
        self.assertEqual("ready", again["status"])
        self.assertEqual("already_ready", again["reason"])
        forced = self.manager.enqueue(PAPER_A, reason="manual", force=True)
        self.assertTrue(forced["queued"])

    def test_import_records_state_without_waiting(self):
        """Default settings: the imported paper waits for the user, and import is fast."""
        pdf = PROJECT_ROOT / "data" / "papers" / "finance-2021-zhang.pdf"
        started = time.time()
        paper = self.store.import_pdf(pdf.read_bytes(), "新导入的论文.pdf")
        elapsed = time.time() - started
        self.assertLess(elapsed, 2.0)
        self.assertEqual("waiting_for_user", paper["analysis"]["status"])
        self.assertIn("点击", paper["analysis"]["message"])
        self.assertEqual(64, len(paper["analysis"]["source_sha256"]))
        self.assertEqual(set(schema.ANALYSIS_DEFAULTS), set(paper["analysis"]))


class NoProvider(ManagerCase):
    def setUp(self):
        super().setUp()
        self.store.update_ai_settings({"provider": "none", "model": ""})

    def test_disabled_provider_yields_waiting_for_provider(self):
        outcome = self.manager.enqueue(PAPER_A, reason="manual")
        self.assertEqual("waiting_for_provider", outcome["status"])
        self.assertFalse(outcome["queued"])
        self.assertIn("尚未配置", self.meta()["message"])

    def test_reading_still_works_without_a_provider(self):
        """With no provider the reader is fully usable; only analysis waits."""
        paper = self.store.paper(PAPER_A)
        paper["fields"]["question"] = "人工写下的研究问题"
        paper["steps"][0] = True
        saved = self.store.save_paper({"id": PAPER_A, "revision": paper["revision"],
                                       "patch": {"fields": dict(paper["fields"]),
                                                 "steps": list(paper["steps"])}})
        self.assertEqual("人工写下的研究问题", saved["paper"]["fields"]["question"])
        self.assertTrue(self.store.files.has_pdf(PAPER_A))
        self.assertEqual("none", self.status(), "saving reading records never starts analysis")
        outcome = self.manager.enqueue(PAPER_A, reason="manual")
        self.assertEqual("waiting_for_provider", outcome["status"])
        self.assertEqual("人工写下的研究问题", self.store.paper(PAPER_A)["fields"]["question"])

    def test_worker_keeps_waiting_when_provider_disappears(self):
        self.store.update_ai_settings({"provider": "ollama", "model": "fake-model"})
        self.manager.enqueue(PAPER_A, reason="manual")
        self.store.update_ai_settings({"provider": "none", "model": ""})
        self.manager.process_once()
        self.assertEqual("waiting_for_provider", self.status())
        self.assertEqual(0, len(self.fake.calls), "no model call while disabled")


class SuccessPath(ManagerCase):
    def test_success_transitions_and_answer_file(self):
        seen = []
        original = self.manager._transition

        def spy(paper_id, status, **changes):
            seen.append((status, changes.get("stage")))
            return original(paper_id, status, **changes)

        self.manager._transition = spy
        self.manager.enqueue(PAPER_A, reason="manual")
        self.manager.process_once()
        self.assertEqual("ready", self.status())
        self.assertEqual([("running", "extracting"), ("running", "chunking"),
                          ("running", "requesting"), ("running", "validating"),
                          ("running", "saving"), ("ready", "done")], seen)
        meta = self.meta()
        self.assertEqual(100, meta["progress"])
        self.assertEqual("done", meta["stage"])
        self.assertEqual(f"{PAPER_A}.json", meta["answer_file"])
        self.assertEqual("ollama", meta["provider"])
        self.assertEqual("fake", meta["adapter"])
        self.assertEqual("fake-model", meta["model"])
        self.assertEqual(analysis.PROMPT_VERSION, meta["prompt_version"])
        self.assertEqual("zh", meta["language"])
        self.assertEqual(1, meta["attempt"])
        self.assertGreaterEqual(meta["duration_ms"], 0)
        payload, error = self.store.analysis_answer(PAPER_A)
        self.assertIsNone(error)
        self.assertEqual(13, len(payload["answers"]))
        self.assertEqual(["robustness"], payload["verification"]["not_found_fields"])
        self.assertEqual("not_found", payload["answers"]["robustness"]["status"])
        self.assertEqual([], payload["answers"]["robustness"]["evidence"])
        self.assertIn("原文未说明", payload["answers"]["robustness"]["answer"])
        self.assertEqual("found", payload["answers"]["question"]["status"])
        self.assertTrue(payload["answers"]["question"]["evidence"][0]["verified"])

    def test_every_state_change_is_persisted(self):
        self.manager.enqueue(PAPER_A, reason="manual")
        on_disk = json.loads((self.tmp / "library.json").read_text(encoding="utf-8"))
        self.assertEqual("queued", on_disk["papers"][0]["analysis"]["status"])
        self.manager.process(PAPER_A)
        on_disk = json.loads((self.tmp / "library.json").read_text(encoding="utf-8"))
        self.assertEqual("ready", on_disk["papers"][0]["analysis"]["status"])
        self.assertTrue((self.tmp / "analyses" / f"{PAPER_A}.json").is_file())

    def test_idempotency_after_success(self):
        self.manager.enqueue(PAPER_A, reason="manual")
        self.manager.process_once()
        calls = len(self.fake.calls)
        self.manager.enqueue(PAPER_A, reason="manual")
        self.manager.process_once()
        self.assertEqual(calls, len(self.fake.calls), "a ready answer must not be regenerated")
        self.assertEqual("ready", self.status())


class FailurePaths(ManagerCase):
    def test_a_sent_timeout_is_unknown_and_not_retried(self):
        """A request that reached the provider must not be re-sent automatically.

        Its outcome is unknown: the provider may have accepted and billed the call, so the
        budget stays charged and the user decides whether to spend again.
        """
        self.fake = providers.FakeProvider(script=["timeout", "timeout"], payload=answer_payload())
        self.manager.provider_factory = lambda settings: self.fake
        self.manager.enqueue(PAPER_A, reason="manual")
        self.manager.process_once()
        self.assertEqual("failed", self.status())
        self.assertEqual("outcome_unknown", self.meta()["error_code"])
        self.assertEqual(1, len(self.fake.calls), "no automatic retry after an unknown outcome")
        self.assertIn("结果未知", self.meta()["message"])
        self.assertFalse(self.store.files.has_analysis(PAPER_A))
        self.assertEqual(1, self.manager.ledger.spend_today())

    def test_invalid_json_fails_without_retry(self):
        self.fake = providers.FakeProvider(script=["invalid_json"], payload=answer_payload())
        self.manager.provider_factory = lambda settings: self.fake
        self.manager.enqueue(PAPER_A, reason="manual")
        self.manager.process_once()
        self.assertEqual("failed", self.status())
        self.assertEqual("invalid_response", self.meta()["error_code"])
        self.assertEqual(1, len(self.fake.calls))

    def test_refusal_is_recorded_as_refused(self):
        self.fake = providers.FakeProvider(script=["refused"], payload=answer_payload())
        self.manager.provider_factory = lambda settings: self.fake
        self.manager.enqueue(PAPER_A, reason="manual")
        self.manager.process_once()
        self.assertEqual("failed", self.status())
        self.assertEqual("refused", self.meta()["error_code"])
        self.assertIn("拒绝", self.meta()["message"])

    def test_an_unsent_transport_failure_is_retried_once(self):
        """An unreachable provider received nothing, so the retry is safe and bounded."""
        self.fake = providers.FakeProvider(script=["error", "error"], payload=answer_payload())
        self.manager.provider_factory = lambda settings: self.fake
        self.manager.enqueue(PAPER_A, reason="manual")
        self.manager.process_once()
        self.assertEqual("failed", self.status())
        self.assertEqual(2, len(self.fake.calls), "one retry, and only while unsent")
        self.assertEqual("unreachable", self.meta()["error_code"])
        self.assertIn("启动本机模型服务", self.meta()["message"])

    def test_failed_can_be_retried_manually(self):
        self.fake = providers.FakeProvider(script=["invalid_json"], payload=answer_payload())
        self.manager.provider_factory = lambda settings: self.fake
        self.manager.enqueue(PAPER_A, reason="manual")
        self.manager.process_once()
        self.assertEqual("failed", self.status())
        self.fake.script = ["success"]
        self.fake._outcome = "success"
        outcome = self.manager.enqueue(PAPER_A, reason="manual", force=True)
        self.assertTrue(outcome["queued"])
        self.manager.process_once()
        self.assertEqual("ready", self.status())

    def test_pdf_without_text_layer_fails_with_actionable_message(self):
        self.manager.extractor_factory = lambda: FakeExtractor(pages=[], ok=False, code="no_text_layer")
        self.manager.enqueue(PAPER_A, reason="manual")
        self.manager.process_once()
        self.assertEqual("failed", self.status())
        self.assertEqual("no_text_layer", self.meta()["error_code"])
        self.assertIn("人工阅读", self.meta()["message"])
        self.assertEqual(0, len(self.fake.calls), "no model call without usable text")


class RecoveryAndStale(ManagerCase):
    def _rewrite_analysis(self, paper_id, **changes):
        state = json.loads((self.tmp / "library.json").read_text(encoding="utf-8"))
        for paper in state["papers"]:
            if paper["id"] == paper_id:
                paper["analysis"].update(changes)
        (self.tmp / "library.json").write_text(json.dumps(state, ensure_ascii=False, indent=2),
                                               encoding="utf-8")

    def test_running_job_from_a_crash_waits_for_the_user(self):
        """Security release: a crash must not become permission to send again."""
        self._rewrite_analysis(PAPER_A, status="running", stage="requesting", progress=55,
                               started_at="2026-10-10T00:00:00Z", updated_at="2026-10-10T00:00:00Z")
        store = server.Store(self.tmp)
        manager = analysis.AnalysisManager(store, provider_factory=lambda s: self.fake,
                                           extractor_factory=lambda: self.extractor)
        report = manager.recover()
        self.assertIn(PAPER_A, report["parked"])
        self.assertNotIn(PAPER_A, report["requeued"])
        self.assertEqual("waiting_for_user", store.analysis_meta(PAPER_A)["status"])
        self.assertTrue(store.analysis_meta(PAPER_A)["message"])
        # no request may happen on its own
        manager.process_once()
        self.assertEqual(0, len(self.fake.calls))

    def test_recovered_job_waits_for_the_user_without_a_new_approval(self):
        self._rewrite_analysis(PAPER_A, status="running", updated_at="2026-10-10T00:00:00Z")
        store = server.Store(self.tmp)
        manager = analysis.AnalysisManager(store, provider_factory=lambda s: self.fake,
                                           extractor_factory=lambda: self.extractor)
        manager.recover()
        # only an explicit request (which records an approval) may start the job again
        manager.process_once()
        self.assertEqual(0, len(self.fake.calls))
        self.assertEqual("waiting_for_user", store.analysis_meta(PAPER_A)["status"])
        manager.enqueue(PAPER_A, reason="manual")
        manager.process_once()
        self.assertEqual("ready", store.analysis_meta(PAPER_A)["status"])
        self.assertEqual(1, len(self.fake.calls))

    def test_a_restored_queued_row_is_not_an_authorisation(self):
        """A backup restores paper state, never an approval: nothing is sent."""
        self._rewrite_analysis(PAPER_A, status="queued", updated_at="2026-10-10T00:00:00Z")
        store = server.Store(self.tmp)
        # a fresh ledger, as a restore would leave behind
        manager = analysis.AnalysisManager(
            store, provider_factory=lambda s: self.fake,
            extractor_factory=lambda: self.extractor,
            ledger=job_policy.JobLedger(self.tmp / "restored", log=lambda record: None))
        report = manager.recover()
        self.assertIn(PAPER_A, report["parked"])
        manager.process_once()
        self.assertEqual(0, len(self.fake.calls))
        self.assertEqual("waiting_for_user", store.analysis_meta(PAPER_A)["status"])

    def test_queued_job_survives_a_restart(self):
        self.manager.enqueue(PAPER_A, reason="manual")
        store = server.Store(self.tmp)
        manager = analysis.AnalysisManager(store, provider_factory=lambda s: self.fake,
                                           extractor_factory=lambda: self.extractor)
        report = manager.recover()
        self.assertIn(PAPER_A, report["requeued"])
        manager.process_once()
        self.assertEqual("ready", store.analysis_meta(PAPER_A)["status"])

    def test_waiting_for_provider_becomes_queued_once_a_provider_exists(self):
        self.store.update_ai_settings({"provider": "none", "model": ""})
        self.manager.enqueue(PAPER_A, reason="manual")
        self.assertEqual("waiting_for_provider", self.status())
        self.store.update_ai_settings({"provider": "ollama", "model": "fake-model"})
        store = server.Store(self.tmp)
        manager = analysis.AnalysisManager(store, provider_factory=lambda s: self.fake,
                                           extractor_factory=lambda: self.extractor)
        report = manager.recover()
        self.assertIn(PAPER_A, report["requeued"])
        manager.process_once()
        self.assertEqual("ready", store.analysis_meta(PAPER_A)["status"])

    def test_ready_becomes_stale_when_the_source_changes(self):
        self.manager.enqueue(PAPER_A, reason="manual")
        self.manager.process_once()
        self.assertEqual("ready", self.status())
        with (self.tmp / "papers" / f"{PAPER_A}.pdf").open("ab") as handle:
            handle.write(b"\n% modified PDF\n")
        store = server.Store(self.tmp)
        manager = analysis.AnalysisManager(store, provider_factory=lambda s: self.fake,
                                           extractor_factory=lambda: self.extractor)
        report = manager.recover()
        self.assertIn(PAPER_A, report["stale"])
        meta = store.analysis_meta(PAPER_A)
        self.assertEqual("stale", meta["status"])
        self.assertEqual("stale", meta["error_code"])
        answer, error = store.analysis_answer(PAPER_A)
        self.assertIsNone(error, "the old answer file is kept for reference")
        self.assertIsNotNone(answer)

    def test_ready_becomes_stale_when_the_model_changes(self):
        self.manager.enqueue(PAPER_A, reason="manual")
        self.manager.process_once()
        self.store.update_ai_settings({"model": "另一个模型"})
        store = server.Store(self.tmp)
        manager = analysis.AnalysisManager(store, provider_factory=lambda s: self.fake,
                                           extractor_factory=lambda: self.extractor)
        report = manager.recover()
        self.assertIn(PAPER_A, report["stale"])

    def test_ready_becomes_stale_when_the_prompt_version_changes(self):
        self.manager.enqueue(PAPER_A, reason="manual")
        self.manager.process_once()
        store = server.Store(self.tmp)
        manager = analysis.AnalysisManager(store, provider_factory=lambda s: self.fake,
                                           extractor_factory=lambda: self.extractor,
                                           prompt_version="p2")
        report = manager.recover()
        self.assertIn(PAPER_A, report["stale"])


class Cancellation(ManagerCase):
    def test_cancel_while_running_marks_cancelled(self):
        self.fake = providers.FakeProvider(script=["success"], payload=answer_payload(),
                                           delay=0.8)
        self.manager.provider_factory = lambda settings: self.fake
        self.manager.enqueue(PAPER_A, reason="manual")
        worker = threading.Thread(target=self.manager.process, args=(PAPER_A,))
        worker.start()
        self.assertTrue(self.fake.wait_started(5), "provider call should have started")
        deadline = time.time() + 5
        while self.status() != "running" and time.time() < deadline:
            time.sleep(0.02)
        self.assertTrue(self.manager.cancel(PAPER_A))
        worker.join(10)
        self.assertEqual("cancelled", self.status())
        self.assertEqual("cancelled", self.meta()["error_code"])

    def test_cancel_before_start_marks_cancelled(self):
        self.manager.enqueue(PAPER_A, reason="manual")
        self.assertTrue(self.manager.cancel(PAPER_A))
        self.assertEqual("cancelled", self.status())
        self.manager.process_once()
        self.assertEqual("cancelled", self.status())


class ConcurrencyAndIsolation(ManagerCase):
    def test_worker_thread_processes_the_queue(self):
        self.manager.start()
        try:
            self.manager.enqueue(PAPER_A, reason="manual")
            self.assertTrue(self.manager.wait_idle(timeout=20))
        finally:
            self.manager.stop()
        self.assertEqual("ready", self.status())

    def test_two_papers_in_parallel_do_not_clobber_a_human_save(self):
        """A human save during a running worker must survive (worker writes analysis only)."""
        second = "finance-2021-zhang"
        self.fake = providers.FakeProvider(script=["success"], payload=answer_payload(), delay=0.4)
        self.manager.provider_factory = lambda settings: self.fake
        paper = self.store.paper(second)
        human_text = "人工写下的研究问题（不能被 worker 覆盖）"
        results = {}

        def human_save():
            current = self.store.paper(second)
            saved = self.store.save_paper({
                "id": second, "revision": current["revision"],
                "patch": {"fields": dict(current["fields"], question=human_text),
                          "steps": [True, False, False, False]}})
            results["revision"] = saved["paper"]["revision"]

        self.manager.enqueue(PAPER_A, reason="manual")
        self.manager.enqueue(second, reason="manual")
        worker = threading.Thread(target=self.manager.process_once)
        worker.start()
        time.sleep(0.15)
        human_save()
        worker.join(30)

        saved_paper = self.store.paper(second)
        self.assertEqual(human_text, saved_paper["fields"]["question"],
                         "the worker overwrote a human field")
        self.assertEqual([True, False, False, False], saved_paper["steps"])
        for paper_id in (PAPER_A, second):
            self.assertEqual("ready", self.store.analysis_meta(paper_id)["status"])
            self.assertTrue(self.store.files.has_analysis(paper_id))
        self.assertGreaterEqual(saved_paper["revision"], results["revision"])

    def test_worker_never_touches_reader_fields(self):
        paper = self.store.paper(PAPER_A)
        paper["fields"]["summary"] = "人工复述"
        paper["annotations"].append({"id": "a1", "type": "argument", "page": 2, "quote": "引文",
                                     "note": "我的理解", "rects": [], "created": schema._utc_now()})
        self.store.save_paper({"id": PAPER_A, "revision": paper["revision"],
                               "patch": {"fields": dict(paper["fields"]),
                                         "annotations": list(paper["annotations"])}})
        before = json.dumps({key: self.store.paper(PAPER_A)[key]
                             for key in ("fields", "steps", "annotations", "lastPage")},
                            ensure_ascii=False, sort_keys=True)
        self.manager.enqueue(PAPER_A, reason="manual")
        self.manager.process_once()
        after = json.dumps({key: self.store.paper(PAPER_A)[key]
                            for key in ("fields", "steps", "annotations", "lastPage")},
                           ensure_ascii=False, sort_keys=True)
        self.assertEqual(before, after)


class Adapters(unittest.TestCase):
    """Requirement 1 and 9: interface, health checks and the four fake outcomes."""

    def test_disabled_adapter_fails_fast(self):
        adapter = providers.DisabledAdapter()
        health = adapter.health_check()
        self.assertFalse(health.ok)
        self.assertEqual("provider_disabled", health.category)
        result = adapter.complete_json(providers.CompletionRequest(system="s", user="u"))
        self.assertFalse(result.ok)
        self.assertEqual("provider_disabled", result.category)

    def _adapter(self, kind, respond, **kwargs):
        """An adapter wired to the recording double; the endpoint policy still runs first.

        Production always uses `secure_transport.SecureTransport`; this seam exists so the
        payload-shape tests need no socket. Negative-path tests additionally assert that the
        double received nothing, which is the same evidence the real transport would give.
        """
        factory = (lambda policy, **inner: RecordingTransport(policy, respond=respond, **inner))
        if kind == "ollama":
            return providers.OllamaAdapter(base_url=kwargs.pop("base_url",
                                                               "http://127.0.0.1:11434"),
                                           model=kwargs.pop("model", "qwen2.5:7b"),
                                           transport_factory=factory, **kwargs)
        return providers.OpenAICompatibleAdapter(
            base_url=kwargs.pop("base_url", "http://127.0.0.1:1234/v1"),
            model=kwargs.pop("model", "local-model"),
            transport_factory=factory, **kwargs)

    def test_ollama_payload_shape(self):
        captured = {}

        def respond(method, path, body):
            captured.update({"method": method, "path": path, "body": body})
            return json_response({"message": {"content": '{"fields": {"question": {}}}'}})

        adapter = self._adapter("ollama", respond)
        result = adapter.complete_json(providers.CompletionRequest(system="sys", user="usr"))
        self.assertTrue(result.ok)
        self.assertEqual("POST", captured["method"])
        self.assertEqual("/api/chat", captured["path"], "the adapter sends a relative path")
        self.assertEqual("json", captured["body"]["format"])
        self.assertFalse(captured["body"]["stream"])
        self.assertEqual("qwen2.5:7b", captured["body"]["model"])
        self.assertEqual([{"role": "system", "content": "sys"},
                          {"role": "user", "content": "usr"}], captured["body"]["messages"])

    def test_openai_compatible_payload_shape_and_key_header(self):
        captured = {}

        def respond(method, path, body):
            captured.update({"method": method, "path": path, "body": body})
            return json_response({"choices": [{"message": {"content": '{"fields": {}}'}}]})

        adapter = self._adapter("openai", respond, api_key="sk-test-key-value-123456")
        result = adapter.complete_json(providers.CompletionRequest(system="s", user="u"))
        self.assertTrue(result.ok)
        # the adapter sends the relative path; the base path comes from the approved target
        self.assertEqual("/chat/completions", captured["path"])
        self.assertEqual({"type": "json_object"}, captured["body"]["response_format"])
        transport = adapter._transport_obj
        self.assertTrue(transport.last["auth_present"], "the credential must reach the target")
        self.assertEqual("Bearer", transport.last["auth_scheme"])
        self.assertEqual("http://127.0.0.1:1234/v1/chat/completions", transport.last["url"])
        self.assertNotIn("sk-test-key-value-123456", json.dumps(transport.requests))

    def test_path_traversal_in_a_relative_path_is_refused(self):
        """A path that tries to escape the approved base path never reaches the transport."""
        adapter = self._adapter("openai", lambda method, path, body: json_response({}))
        for bad in ("/../../etc/passwd", "/a%2f..%2fb", "relative", "/a\\b"):
            with self.assertRaises(endpoint_policy_mod.PolicyError, msg=bad):
                adapter._post_json(bad, {"a": 1}, 5)
        # the target was approved, but nothing may have been sent
        self.assertEqual([], adapter._transport_obj.requests if adapter._transport_obj else [])

    def test_invalid_json_is_categorised(self):
        adapter = self._adapter("ollama", lambda method, path, body: json_response(
            {"message": {"content": "not json at all"}}))
        result = adapter.complete_json(providers.CompletionRequest(system="s", user="u"))
        self.assertFalse(result.ok)
        self.assertEqual("invalid_json", result.category)
        self.assertTrue(result.message)

    def test_json_inside_a_fence_is_accepted(self):
        adapter = self._adapter("ollama", lambda method, path, body: json_response(
            {"message": {"content": '```json\n{"fields": {"a": 1}}\n```'}}))
        result = adapter.complete_json(providers.CompletionRequest(system="s", user="u"))
        self.assertTrue(result.ok)
        self.assertEqual({"a": 1}, result.payload["fields"])

    def test_http_auth_error_is_categorised(self):
        adapter = self._adapter("openai", lambda method, path, body: error_response(
            secret_guard.E_AUTH_FAILED, status=401), api_key="sk-secret-value-1234567890")
        result = adapter.complete_json(providers.CompletionRequest(system="s", user="u"))
        self.assertFalse(result.ok)
        self.assertEqual("auth_error", result.category)
        self.assertEqual(secret_guard.E_AUTH_FAILED, result.public_code)
        self.assertEqual(401, result.http_status)
        self.assertNotIn("sk-secret-value-1234567890", json.dumps(result.as_dict()))

    def test_unreachable_is_categorised(self):
        adapter = self._adapter("ollama", lambda method, path, body: error_response(
            secret_guard.E_UNREACHABLE))
        result = adapter.complete_json(providers.CompletionRequest(system="s", user="u"))
        self.assertEqual("unreachable", result.category)

    def test_health_check_reports_missing_model(self):
        adapter = self._adapter("openai", lambda method, path, body: json_response(
            {"data": [{"id": "another-model"}]}), model="wanted-model")
        health = adapter.health_check()
        self.assertFalse(health.ok)
        self.assertEqual("config_error", health.category)
        self.assertIn("wanted-model", health.detail)

    def test_log_lines_only_carry_provider_model_duration_and_category(self):
        record = secret_guard.log_record(provider="ollama", model="qwen2.5:7b",
                                         duration_ms=1234, category="ok", ok=True,
                                         api_key="sk-should-be-dropped")
        line = json.dumps(record, ensure_ascii=False)
        self.assertEqual({"provider", "model", "duration_ms", "category", "ok"},
                         set(json.loads(line)))

    def test_redaction_removes_keys(self):
        for verbatim, other in (("Authorization: Bearer sk-live-abcdef123456",
                                 "sk-live-abcdef123456"),
                                ('api_key="sk-live-abcdef123456"', "sk-live-abcdef123456"),
                                ("token=abcdef1234567890abcdef", "abcdef1234567890abcdef"),
                                ("failed with sk-live-abcdef123456", "sk-live-abcdef123456")):
            cleaned = secret_guard.redact(verbatim)
            # report the channel, never the value
            self.assertNotIn(other, cleaned, "redaction missed a credential-shaped string")
        self.assertEqual("connection refused on port 11434",
                         secret_guard.redact("connection refused on port 11434"))
        # the narrow patterns must not eat ordinary text
        for benign in ("https://api.example.com/v1/chat/completions",
                       "model qwen2.5:7b returned 200 in 4210 ms",
                       "-webkit-mask-position:center"):
            self.assertEqual(benign, secret_guard.redact(benign))

    def test_key_is_read_from_a_registered_environment_name_only(self):
        import os
        os.environ["PAPER_READER_TEST_KEY"] = "sk-from-env-1234567890"
        self.addCleanup(os.environ.pop, "PAPER_READER_TEST_KEY", None)
        secret, code, ref = providers.resolve_api_key("PAPER_READER_TEST_KEY")
        self.assertEqual(credentials.CODE_OK, code, code)
        self.assertEqual("PAPER_READER_TEST_KEY", credentials.name_of(ref))
        self.assertTrue(secret)
        self.assertEqual("sk-from-env-1234567890", secret.reveal())
        # the wrapper must not print the value
        self.assertNotIn("sk-from-env", repr(secret))
        self.assertNotIn("sk-from-env", str(secret))

        secret, code, _ref = providers.resolve_api_key("")
        self.assertIsNone(secret)
        self.assertEqual(credentials.CODE_NOT_BOUND, code)

        # an environment name this application does not own is refused, not forwarded
        os.environ["AWS_SECRET_ACCESS_KEY"] = "not-our-credential-123456"
        self.addCleanup(os.environ.pop, "AWS_SECRET_ACCESS_KEY", None)
        secret, code, _ref = providers.resolve_api_key("AWS_SECRET_ACCESS_KEY")
        self.assertIsNone(secret)
        self.assertEqual(credentials.CODE_UNREGISTERED, code)

    def test_build_adapter_follows_settings(self):
        disabled = providers.build_adapter({"provider": "none"})
        self.assertIsInstance(disabled, providers.DisabledAdapter)
        ollama = providers.build_adapter({"provider": "ollama", "model": "m"})
        self.assertIsInstance(ollama, providers.OllamaAdapter)
        local = providers.build_adapter({"provider": "lmstudio", "model": "m",
                                         "base_url": "http://127.0.0.1:9999/v1"})
        self.assertIsInstance(local, providers.OpenAICompatibleAdapter)
        self.assertEqual("http://127.0.0.1:9999/v1", local.base_url)


if __name__ == "__main__":
    unittest.main(verbosity=2)
