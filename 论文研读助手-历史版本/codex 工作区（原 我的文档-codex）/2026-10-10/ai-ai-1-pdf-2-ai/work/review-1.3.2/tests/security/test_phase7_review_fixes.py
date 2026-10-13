"""Phase 7 tests: the two defects the independent review found, and their guards.

Both are about a control that existed on one path but not on the path that mattered:

* the send gate checked "is this still the current job", but the answer writer did not, so a
  superseded job could overwrite the newer answer (and both calls were paid for);
* the analysis endpoint checked "is AI paused", but the health check and model list did not,
  so requests carrying the credential left the machine while the user believed AI was off.

    python -m unittest discover -s tests/security -p "test_phase7_*.py"
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

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tests" / "security"))

import analysis      # noqa: E402
import credentials   # noqa: E402
import job_policy    # noqa: E402
import providers     # noqa: E402
import secret_guard  # noqa: E402
import server        # noqa: E402
from baseline_helpers import fresh_library_v1  # noqa: E402
from card_fixtures import build_card, pages_from_prompt  # noqa: E402

PAPER = "medical-2026-zang"


class SlowProvider(providers.FakeProvider):
    """A provider whose first call blocks until it is released."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.gate = threading.Event()
        self.entered = threading.Event()
        self.block_first = True
        self._lock = threading.Lock()

    def complete_json(self, request, on_start=None):
        with self._lock:
            first = self.block_first
            self.block_first = False
        if first:
            self.entered.set()
            self.gate.wait(20)
        return super().complete_json(request, on_start=on_start)


class LateGeneration(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sec-p7-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        fresh_library_v1(self.tmp)
        self.store = server.Store(self.tmp)
        self.store.update_ai_settings({"provider": "ollama", "model": "stub-model",
                                       "auto_analyze": False,
                                       "limits": {"max_requests_per_job": 4,
                                                  "daily_request_limit": 50}})
        self.fake = SlowProvider(script=["success", "success"], payload={})
        self.fake.payload_for = lambda request, payload: (build_card(pages_from_prompt(request.user))
                                                          or payload)
        self.logs = []
        self.manager = analysis.AnalysisManager(
            self.store, provider_factory=lambda settings: self.fake, log=self.logs.append)

    def _approve(self, paper_id=PAPER):
        self.manager.grant_approval(paper_id, model="stub-model", scope="test")

    def test_a_superseded_result_does_not_overwrite_the_newer_answer(self):
        """Run two jobs for one paper, with concurrency 2, and check who owns the answer."""
        self.store.update_ai_settings({"max_concurrent_jobs": 2})
        manager = analysis.AnalysisManager(
            self.store, provider_factory=lambda settings: self.fake, log=self.logs.append)
        self._approve()
        self._approve()          # the approval is per paper and per target, as before

        # freeze the first job before the worker starts, so the second one replaces it
        manager.enqueue(PAPER, reason="manual")
        first = threading.Thread(target=self._run, args=(manager,))
        first.start()
        self.assertTrue(self.fake.entered.wait(10), "the slow call never started")

        # the second job is frozen here, which is what replaces the first
        manager.enqueue(PAPER, reason="force", force=True)
        second = threading.Thread(target=self._run, args=(manager,))
        second.start()
        time.sleep(0.4)
        self.fake.gate.set()
        first.join(30)
        second.join(30)

        frozen = manager.ledger.frozen_job(PAPER)
        payload = json.loads(self.store.files.analysis_path(PAPER).read_text(encoding="utf-8"))
        # the stored answer must belong to the job that is still current
        self.assertTrue(manager.ledger.job_is_current(PAPER, frozen.job_id))
        self.assertEqual("ready", self.store.analysis_meta(PAPER)["status"],
                         self.store.analysis_meta(PAPER))
        self.assertTrue(payload.get("source_sha256"))
        # the replaced job is recorded rather than silently discarded, and it does not own
        # the stored answer any more
        superseded = [entry for entry in self.logs
                      if isinstance(entry, dict) and entry.get("event") == "result_superseded"]
        self.assertTrue(superseded, "the replaced job must be recorded")
        self.assertNotEqual(superseded[0].get("job_id"), frozen.job_id)
        # both calls happened, and both were charged: the new answer is the one on disk
        self.assertGreaterEqual(len(self.fake.calls), 1)
        charged = [entry for entry in manager.ledger.entries_for(frozen.job_id)
                   if not entry.get("refunded")]
        self.assertTrue(charged, "the current job's call is charged")

    def _run(self, manager):
        """Run whichever job is currently frozen for this paper."""
        try:
            manager.process(PAPER)
        except Exception as exc:                                  # noqa: BLE001
            self.logs.append({"event": "test_worker_error", "code": type(exc).__name__})

    def test_every_transmission_is_charged(self):
        """Two calls for one paper cost two requests, and the ledger says so."""
        self._approve()
        manager = analysis.AnalysisManager(
            self.store, provider_factory=lambda settings: self.fake, log=self.logs.append)
        manager.enqueue(PAPER, reason="manual")
        manager.process(PAPER)
        job = manager.ledger.frozen_job(PAPER)
        charged = [entry for entry in manager.ledger.entries_for(job.job_id)
                   if not entry.get("refunded")]
        self.assertEqual(len(self.fake.calls), len(charged),
                         f"{len(self.fake.calls)} calls but {len(charged)} charged")

    def test_cancel_reaches_every_in_flight_call_for_the_paper(self):
        self._approve()
        self.manager.enqueue(PAPER, reason="manual")
        runner = threading.Thread(target=self.manager.process, args=(PAPER,))
        runner.start()
        self.assertTrue(self.fake.entered.wait(10))
        self.assertTrue(self.manager.cancel(PAPER), "cancel must find the running call")
        self.fake.gate.set()
        runner.join(30)
        self.assertEqual("cancelled", self.store.analysis_meta(PAPER)["status"])


class PausedTarget(unittest.TestCase):
    """The health check and the model list are outbound requests and obey the same switches."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sec-p7-pause-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        fresh_library_v1(self.tmp)
        self.store = server.Store(self.tmp)
        self.store.update_ai_settings({"provider": "ollama", "model": "stub-model",
                                       "base_url": "http://127.0.0.1:1"})
        self.calls = []

        class Counting(providers.ProviderAdapter):
            name = "counting"

            def health_check(self, timeout=10):
                self_outer.calls.append("health")
                return providers.Health(False, providers.CATEGORY_UNREACHABLE, "not used")

            def complete_json(self, request, on_start=None):
                self_outer.calls.append("complete")
                return providers.AdapterResult(False)

            def describe(self):
                return {"provider": self.name, "model": "", "base_url": ""}

        self_outer = self
        self.adapter = Counting()
        self.manager = analysis.AnalysisManager(
            self.store, provider_factory=lambda settings: self.adapter, log=lambda record: None)

    def test_a_paused_service_does_not_probe_the_provider(self):
        self.manager.pause(True, reason="test")
        ok, adapter, health = self.manager.provider_ready()
        self.assertFalse(ok)
        self.assertEqual([], self.calls, "a paused service must not contact the provider")
        self.assertEqual(secret_guard.E_PAUSED, health.category)

    def test_an_unapproved_target_is_not_probed(self):
        # a real credential reference, so the target needs approval
        self.store.update_ai_settings({"provider": "openai_compatible", "mode": "cloud_https",
                                       "base_url": "https://api.example.com/v1",
                                       "model": "m", "api_key_env": "env:PAPER_READER_KEY"})
        ledger = job_policy.JobLedger(self.tmp / "fresh-ledger", log=lambda record: None)
        manager = analysis.AnalysisManager(
            self.store, provider_factory=lambda settings: self.adapter,
            ledger=ledger, log=lambda record: None)
        ok, adapter, health = manager.provider_ready()
        self.assertFalse(ok, "an unapproved target must not receive a credentialled probe")
        self.assertEqual([], self.calls)
        self.assertEqual(secret_guard.E_CONSENT_REQUIRED, health.category)

    def test_approving_the_target_lets_the_health_check_run(self):
        self.store.update_ai_settings({"provider": "openai_compatible", "mode": "cloud_https",
                                       "base_url": "https://api.example.com/v1",
                                       "model": "m", "api_key_env": "env:PAPER_READER_KEY"})
        ledger = job_policy.JobLedger(self.tmp / "approved-ledger", log=lambda record: None)
        manager = analysis.AnalysisManager(
            self.store, provider_factory=lambda settings: self.adapter,
            ledger=ledger, log=lambda record: None)
        manager.approve_target()
        self.assertTrue(manager.target_approved())
        manager.provider_ready()
        self.assertEqual(["health"], self.calls)

    def test_changing_the_address_revokes_the_target_approval(self):
        self.store.update_ai_settings({"provider": "openai_compatible", "mode": "cloud_https",
                                       "base_url": "https://api.example.com/v1",
                                       "model": "m", "api_key_env": "env:PAPER_READER_KEY"})
        ledger = job_policy.JobLedger(self.tmp / "moved-ledger", log=lambda record: None)
        manager = analysis.AnalysisManager(
            self.store, provider_factory=lambda settings: self.adapter,
            ledger=ledger, log=lambda record: None)
        manager.approve_target()
        self.store.update_ai_settings({"base_url": "https://api.other.example.com/v1"})
        self.assertFalse(manager.target_approved(), "a new address needs a new confirmation")

    def test_a_disabled_profile_is_not_probed(self):
        self.manager.set_profile_disabled(self.manager.current_profile_id(), True)
        ok, adapter, health = self.manager.provider_ready()
        self.assertFalse(ok)
        self.assertEqual([], self.calls)
        self.assertEqual(secret_guard.E_PROFILE_DISABLED, health.category)


class AnswerVisibility(unittest.TestCase):
    """The stored card already distinguishes verbatim from approximate; check the contract."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sec-p7-ui-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        fresh_library_v1(self.tmp)

    def test_the_panel_renders_the_verification_state(self):
        source = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("e.verified===false", source,
                      "the panel must tell a candidate from a verbatim quotation")
        self.assertIn("ai-unverified", source)
        self.assertIn("ai-verified", source)
        self.assertIn("s.answer.warnings", source, "the card's warnings must be shown")
        self.assertNotIn("由本机模型生成", source,
                         "the provider line must not claim a local model under an external API")
        css = (ROOT / "static" / "ai-reference.css").read_text(encoding="utf-8")
        for name in (".ai-verified", ".ai-unverified", ".ai-warnings"):
            self.assertIn(name, css, name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
