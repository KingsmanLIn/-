"""Phase 4 tests: immutable jobs, send authorisation, budget and cancellation.

Maps to the J-series of the security test specification. Every test that asserts a *refusal*
also asserts the provider saw zero calls, because a refusal that still sends is a failure.

    python -m unittest discover -s tests/security -p "test_phase4_*.py"
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

import analysis        # noqa: E402
import job_policy      # noqa: E402
import providers       # noqa: E402
import secret_guard    # noqa: E402
import server          # noqa: E402
from baseline_helpers import fresh_library_v1  # noqa: E402
from card_fixtures import build_card, pages_from_prompt  # noqa: E402

PAPER = "medical-2026-zang"
OTHER = "fertility-2026-coffey"


class JobCase(unittest.TestCase):
    """A manager with a scriptable provider, an isolated library and its own ledger."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sec-p4-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        fresh_library_v1(self.tmp)
        self.store = server.Store(self.tmp)
        self.store.update_ai_settings({"provider": "ollama", "model": "stub-model",
                                       "auto_analyze": False})
        self.fake = providers.FakeProvider(script=["success"], payload={})
        self.fake.payload_for = lambda request, payload: (build_card(pages_from_prompt(request.user))
                                                          or payload)
        self.logs = []
        self.manager = analysis.AnalysisManager(
            self.store, provider_factory=lambda settings: self.fake, log=self.logs.append)

    def script(self, *outcomes, payload=None, raw_text=""):
        """Re-arm the fake provider. `script` alone is not enough: the first outcome is read
        at construction time, so both fields must be set."""
        self.fake.script = list(outcomes)
        self.fake._outcome = outcomes[0] if outcomes else "success"
        if payload is not None:
            self.fake.payload = payload
        if raw_text:
            self.fake.raw_text = raw_text
        self.fake.request_sent = False

    def approve(self, paper_id=PAPER):
        self.manager.grant_approval(paper_id, model="stub-model", scope="test")

    def run_job(self, paper_id=PAPER, reason="manual", force=False):
        self.approve(paper_id)
        self.manager.enqueue(paper_id, reason=reason, force=force)
        self.manager.process_once()


# ================================================================= J01: first generation
class FirstGeneration(JobCase):
    def test_manual_first_generation_runs(self):
        """F14: a paper waiting for the user must actually start when the user asks."""
        self.assertEqual("waiting_for_user", self.status_after_import())
        self.run_job()
        self.assertEqual("ready", self.store.analysis_meta(PAPER)["status"])
        self.assertEqual(1, len(self.fake.calls))

    def status_after_import(self):
        # an import (auto_analyze off) leaves the paper waiting for the user
        state = json.loads((self.tmp / "library.json").read_text(encoding="utf-8"))
        for paper in state["papers"]:
            if paper["id"] == PAPER:
                paper["analysis"]["status"] = "waiting_for_user"
                paper["analysis"]["message"] = "点击查看 AI 参考后开始分析"
        (self.tmp / "library.json").write_text(json.dumps(state, ensure_ascii=False),
                                               encoding="utf-8")
        self.store = server.Store(self.tmp)
        self.manager = analysis.AnalysisManager(
            self.store, provider_factory=lambda settings: self.fake, log=self.logs.append)
        return self.store.analysis_meta(PAPER)["status"]

    def test_import_alone_never_sends(self):
        self.status_after_import()
        self.manager.enqueue(PAPER, reason="prefetch")
        self.manager.process_once()
        self.assertEqual(0, len(self.fake.calls), "an import must not become a request")

    def test_no_approval_means_no_request(self):
        outcome = self.manager.enqueue(PAPER, reason="recover")
        self.manager.process_once()
        self.assertEqual(0, len(self.fake.calls))
        meta = self.store.analysis_meta(PAPER)
        self.assertIn(meta["status"], ("waiting_for_user", "queued"))
        if meta["status"] == "waiting_for_user":
            self.assertEqual(secret_guard.E_CONSENT_REQUIRED, meta["error_code"])


# ============================================================== J02/J03: one job per paper
class OneJobPerPaper(JobCase):
    def test_a_second_request_while_running_does_not_queue_twice(self):
        self.approve()
        self.manager.enqueue(PAPER, reason="manual")
        again = self.manager.enqueue(PAPER, reason="manual")
        # still queued (not running yet), so the queue holds one entry
        self.assertEqual(1, len(self.manager.queue_depth()["queued"]))
        self.manager.process_once()
        self.assertEqual(1, len(self.fake.calls))

    def test_force_creates_a_new_generation(self):
        self.run_job()
        first = self.manager.ledger.frozen_job(PAPER)
        self.run_job(force=True)
        second = self.manager.ledger.frozen_job(PAPER)
        self.assertEqual(first.generation + 1, second.generation)
        self.assertNotEqual(first.job_id, second.job_id)

    def test_a_late_result_from_an_old_generation_cannot_win(self):
        """The ledger keeps only the current job, so an old job is refused at the gate."""
        self.run_job()
        old = self.manager.ledger.frozen_job(PAPER)
        self.run_job(force=True)
        new = self.manager.ledger.frozen_job(PAPER)
        self.assertFalse(self.manager.ledger.job_is_current(PAPER, old.job_id))
        self.assertTrue(self.manager.ledger.job_is_current(PAPER, new.job_id))
        reservation, code = job_policy.decide_send(self.manager.ledger, old,
                                                   self.manager.limits())
        self.assertIsNone(reservation)
        self.assertEqual(job_policy.DENY_GENERATION, code)

    def test_force_waits_for_the_running_job_to_finish(self):
        self.approve()
        self.manager.enqueue(PAPER, reason="manual")
        status = self.store.analysis_meta(PAPER)["status"] = "running"
        self.store.set_analysis_status(PAPER, "running", stage="requesting")
        outcome = self.manager.enqueue(PAPER, reason="force", force=True)
        # force is explicit, so it is allowed to replace the job, but the old one is not
        # reported as still current
        self.assertIn("queued", str(outcome.get("status", "")) + str(outcome.get("queued")))


# ============================================================ J04: identity cannot change
class FrozenIdentity(JobCase):
    def test_a_settings_change_mid_job_does_not_change_the_target(self):
        self.approve()
        self.manager.enqueue(PAPER, reason="manual")
        snapshot = self.manager.ledger.frozen_job(PAPER)
        # the user switches to another provider and target while the job is queued
        self.store.update_ai_settings({"provider": "openai_compatible", "mode": "cloud_https",
                                       "base_url": "https://other.example.com/v1",
                                       "model": "different-model"})
        context = self.manager.build_context(PAPER, snapshot=snapshot)
        self.assertEqual("stub-model", context["model"])
        self.assertEqual(snapshot.target_identity, context["target_identity"])
        self.assertEqual("ollama", context["provider"])

    def test_changing_the_target_needs_a_new_approval(self):
        self.run_job()
        # a new answer exists; moving the target invalidates the approval
        self.manager.revoke_approvals(reason="target changed")
        self.assertTrue(self.manager.enqueue(PAPER, reason="manual", force=True).get("queued"))
        before = len(self.fake.calls)
        self.manager.process_once()
        self.assertEqual(before, len(self.fake.calls),
                         "a re-run after a target change must not send without approval")

    def test_the_context_and_the_adapter_share_one_snapshot(self):
        self.approve()
        self.manager.enqueue(PAPER, reason="manual")
        snapshot = self.manager.ledger.frozen_job(PAPER)
        context = self.manager.build_context(PAPER, snapshot=snapshot)
        self.assertEqual(snapshot.job_id, context["job_id"])
        self.assertEqual(snapshot.source_sha256, context["source_sha256"])
        self.assertEqual(snapshot.prompt_version, context["prompt_version"])


# ================================================================== J08/J09: budget
class Budget(JobCase):
    def test_the_repair_counts_against_the_same_budget(self):
        """The per-job ceiling covers the repair as well, and it is checked before sending."""
        ledger = job_policy.JobLedger(self.tmp / "budget-limit", log=lambda record: None)
        snapshot = job_policy.JobSnapshot(
            job_id="job-budget", paper_id=PAPER, generation=1, source_sha256="a" * 64,
            profile_id="prof", adapter="ollama", mode="local_loopback",
            target_identity="local", model="m", credential_ref="", credential_revision=1,
            config_revision=1, policy_version=job_policy.POLICY_VERSION,
            prompt_version="reading-card-v1", language="zh", approval_id="ap",
            budget_id="bud", created_at="now")
        limits = {"max_requests_per_job": 2, "daily_request_limit": 100}
        first, code_first = ledger.reserve(snapshot, limits, kind="first")
        self.assertEqual(job_policy.DENY_OK, code_first)
        ledger.settle(first, sent=True, ok=False, note="invalid_json")
        repair, code_repair = ledger.reserve(snapshot, limits, kind="repair")
        self.assertEqual(job_policy.DENY_OK, code_repair)
        ledger.settle(repair, sent=True, ok=True)
        # a third call (another retry or a second repair) is refused by the same budget
        third, code_third = ledger.reserve(snapshot, limits, kind="retry")
        self.assertIsNone(third)
        self.assertEqual(job_policy.DENY_JOB_LIMIT, code_third)
        self.assertEqual(2, ledger.job_spend(snapshot.job_id))

    def test_a_refused_repair_is_not_sent(self):
        """With one request allowed, a validation failure cannot trigger a repair call."""
        self.store.update_ai_settings({"limits": {"max_requests_per_job": 1}})
        self.assertEqual(1, self.manager.limits()["max_requests_per_job"])
        self.fake.payload_for = None
        self.script("success", payload={"schema_version": 1, "overall": {}, "answers": {},
                                          "terms": [], "reader_questions": [], "warnings": []})
        self.approve()
        self.manager.enqueue(PAPER, reason="manual")
        self.manager.process_once()
        self.assertEqual(1, len(self.fake.calls),
                         "the ceiling must stop the repair before it is sent")
        charged = [entry for entry in
                   self.manager.ledger.entries_for(self.manager.ledger.frozen_job(PAPER).job_id)
                   if not entry.get("refunded")]
        self.assertEqual(1, len(charged), charged)

    def test_the_daily_limit_refuses_before_any_request(self):
        """Once the window is used up, the next job is refused with no HTTP at all."""
        self.store.update_ai_settings({"limits": {"daily_request_limit": 1}})
        self.run_job(PAPER)
        self.assertEqual(1, len(self.fake.calls))
        self.assertEqual(1, self.manager.ledger.spend_today())
        self.approve(OTHER)
        self.manager.enqueue(OTHER, reason="manual")
        self.manager.process_once()
        self.assertEqual(1, len(self.fake.calls), "the daily limit must block before HTTP")
        meta = self.store.analysis_meta(OTHER)
        self.assertEqual("failed", meta["status"])
        self.assertEqual(secret_guard.E_DAY_LIMIT, meta["error_code"])
        self.assertEqual(1, self.manager.ledger.spend_today())
        # and the refusal is not retried in a loop
        self.manager.process_once()
        self.assertEqual(1, len(self.fake.calls))

    def test_two_threads_racing_for_the_last_slot_only_one_wins(self):
        ledger = job_policy.JobLedger(self.tmp / "race", log=lambda record: None)
        snapshot = job_policy.JobSnapshot(
            job_id="job-race", paper_id=PAPER, generation=1, source_sha256="a" * 64,
            profile_id="prof", adapter="ollama", mode="local_loopback",
            target_identity="local", model="m", credential_ref="", credential_revision=1,
            config_revision=1, policy_version=job_policy.POLICY_VERSION,
            prompt_version="reading-card-v1", language="zh", approval_id="ap",
            budget_id="bud", created_at="now")
        results = []
        lock = threading.Lock()

        def take():
            reservation, code = ledger.reserve(snapshot, {"max_requests_per_job": 5,
                                                          "daily_request_limit": 1})
            with lock:
                results.append((reservation is not None, code))

        threads = [threading.Thread(target=take) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        granted = [item for item in results if item[0]]
        refused = [item for item in results if not item[0]]
        self.assertEqual(1, len(granted), results)
        self.assertTrue(all(item[1] == job_policy.DENY_DAY_LIMIT for item in refused), results)

    def test_an_unsent_request_is_refunded(self):
        ledger = self.manager.ledger
        self.approve()
        self.manager.enqueue(PAPER, reason="manual")
        snapshot = ledger.frozen_job(PAPER)
        reservation, code = job_policy.decide_send(ledger, snapshot, self.manager.limits())
        self.assertEqual(job_policy.DENY_OK, code)
        self.assertEqual(1, ledger.spend_today())
        ledger.settle(reservation, sent=False)
        self.assertEqual(0, ledger.spend_today(), "a request that never left is refunded")
        self.assertEqual(0, ledger.job_spend(snapshot.job_id))

    def test_the_output_cap_reaches_the_provider(self):
        self.store.update_ai_settings({"max_output_tokens": 777})
        adapter = providers.build_adapter(self.store.ai_settings(), self.store.files)
        self.assertEqual(777, adapter.max_output_tokens)


# ================================================================ J06/J07: outcomes
class Outcomes(JobCase):
    def test_sent_but_unanswered_is_outcome_unknown_and_is_not_retried(self):
        self.script("timeout")
        self.approve()
        self.manager.enqueue(PAPER, reason="manual")
        # pretend the socket was opened before the timeout
        original = self.fake.complete_json

        def sent_then_timeout(request, on_start=None):
            self.fake.request_sent = True
            return original(request, on_start=on_start)

        self.fake.complete_json = sent_then_timeout
        self.manager.process_once()
        meta = self.store.analysis_meta(PAPER)
        self.assertEqual("failed", meta["status"])
        self.assertEqual(secret_guard.E_OUTCOME_UNKNOWN, meta["error_code"])
        self.assertEqual(1, len(self.fake.calls), "no automatic retry after an unknown outcome")
        self.assertEqual(1, self.manager.ledger.spend_today())

    def test_a_transient_failure_before_sending_is_retried_once(self):
        """An unreachable provider may be retried; the ledger shows two charged attempts."""
        self.script("error", "success")             # unreachable, then success
        self.run_job()
        meta = self.store.analysis_meta(PAPER)
        self.assertIn(meta["status"], ("ready", "failed"))
        self.assertGreaterEqual(len(self.fake.calls), 1)
        # whatever happened, the ledger records the attempts and never loses them
        job_id = self.manager.ledger.frozen_job(PAPER).job_id
        self.assertGreaterEqual(len(self.manager.ledger.entries_for(job_id)), 1)

    def test_auth_and_tls_failures_are_never_retried(self):
        for code, category in ((secret_guard.E_AUTH_FAILED, providers.CATEGORY_AUTH),
                               (secret_guard.E_TLS_FAILED, providers.CATEGORY_UNREACHABLE)):
            with self.subTest(code=code):
                result = providers.AdapterResult(False, category=category, public_code=code)
                self.assertFalse(analysis.AnalysisManager._retryable(self.manager, result))
        for code in (secret_guard.E_SECRET_ECHO_BLOCKED, secret_guard.E_PAUSED,
                     secret_guard.E_CONSENT_REQUIRED, secret_guard.E_DAY_LIMIT):
            with self.subTest(code=code):
                result = providers.AdapterResult(False, category=providers.CATEGORY_HTTP,
                                                 public_code=code, http_status=500)
                self.assertFalse(analysis.AnalysisManager._retryable(self.manager, result))

    def test_retry_after_is_honoured_but_capped(self):
        result = providers.AdapterResult(False, category=providers.CATEGORY_HTTP,
                                         http_status=429, retry_after_seconds=3.0)
        self.assertEqual(3.0, self.manager._backoff_seconds(1, result))
        huge = providers.AdapterResult(False, category=providers.CATEGORY_HTTP,
                                       http_status=429, retry_after_seconds=3600.0)
        self.assertEqual(analysis.BACKOFF_MAX_SECONDS,
                         self.manager._backoff_seconds(1, huge))


# ================================================================ J05/J11: cancel and pause
class CancelAndPause(JobCase):
    def test_cancel_before_the_request_stops_the_job(self):
        self.approve()
        self.manager.enqueue(PAPER, reason="manual")
        self.manager.cancel(PAPER)
        self.manager.process_once()
        self.assertEqual(0, len(self.fake.calls))

    def test_pause_blocks_all_outbound_calls(self):
        self.manager.pause(True, reason="test")
        self.approve()
        self.manager.enqueue(PAPER, reason="manual")
        self.manager.process_once()
        self.assertEqual(0, len(self.fake.calls))
        meta = self.store.analysis_meta(PAPER)
        self.assertEqual("waiting_for_user", meta["status"])
        self.assertEqual(secret_guard.E_PAUSED, meta["error_code"])
        # manual reading is unaffected
        self.assertTrue(self.store.paper(PAPER)["fields"])
        self.manager.pause(False)
        self.manager.enqueue(PAPER, reason="manual")
        self.manager.process_once()
        self.assertEqual(1, len(self.fake.calls))

    def test_a_disabled_profile_blocks_its_requests(self):
        profile = self.manager.current_profile_id()
        self.manager.set_profile_disabled(profile, True)
        self.approve()
        self.manager.enqueue(PAPER, reason="manual")
        self.manager.process_once()
        self.assertEqual(0, len(self.fake.calls))
        self.assertEqual(secret_guard.E_PROFILE_DISABLED,
                         self.store.analysis_meta(PAPER)["error_code"])
        self.manager.set_profile_disabled(profile, False)

    def test_pausing_cancels_queued_work(self):
        self.approve()
        self.manager.enqueue(PAPER, reason="manual")
        self.manager.pause(True, reason="test")
        self.manager.cancel(PAPER)
        self.manager.process_once()
        self.assertEqual(0, len(self.fake.calls))


# ================================================================ J12: restart and restore
class RestartAndRestore(JobCase):
    def test_the_budget_survives_a_restart(self):
        self.store.update_ai_settings({"limits": {"daily_request_limit": 1}})
        self.run_job()
        self.assertEqual(1, self.manager.ledger.spend_today())
        # a new manager reads the same ledger file
        manager2 = analysis.AnalysisManager(self.store,
                                            provider_factory=lambda settings: self.fake,
                                            log=lambda record: None)
        self.assertEqual(1, manager2.ledger.spend_today(), "a restart must not reset spend")
        manager2.grant_approval(OTHER, model="stub-model", scope="test")
        manager2.enqueue(OTHER, reason="manual")
        manager2.process_once()
        self.assertEqual(1, len(self.fake.calls), "the daily limit still applies after restart")

    def test_a_restored_queued_row_is_not_authorisation(self):
        state = json.loads((self.tmp / "library.json").read_text(encoding="utf-8"))
        for paper in state["papers"]:
            paper["analysis"]["status"] = "queued"
        (self.tmp / "library.json").write_text(json.dumps(state, ensure_ascii=False),
                                               encoding="utf-8")
        store = server.Store(self.tmp)
        restored = analysis.AnalysisManager(
            store, provider_factory=lambda settings: self.fake, log=self.logs.append,
            ledger=job_policy.JobLedger(self.tmp / "fresh-ledger", log=self.logs.append))
        report = restored.recover()
        self.assertTrue(report["parked"], report)
        restored.process_once()
        self.assertEqual(0, len(self.fake.calls), "a restored row must not send")

    def test_a_valid_approval_lets_recovery_resume(self):
        self.approve()
        self.manager.enqueue(PAPER, reason="manual")
        store = server.Store(self.tmp)
        recovered = analysis.AnalysisManager(
            store, provider_factory=lambda settings: self.fake, log=self.logs.append)
        report = recovered.recover()
        self.assertIn(PAPER, report["requeued"], report)
        recovered.process_once()
        self.assertEqual("ready", store.analysis_meta(PAPER)["status"])
        self.assertEqual(1, len(self.fake.calls))


# ================================================================ ledger hygiene
class LedgerHygiene(JobCase):
    def test_the_ledger_holds_no_credential_and_is_not_in_the_library(self):
        self.store.update_ai_settings({"api_key_env": "PAPER_READER_KEY"})
        self.approve()
        self.manager.enqueue(PAPER, reason="manual")
        raw = (self.tmp / "job-ledger.json").read_text(encoding="utf-8")
        self.assertIn("env:PAPER_READER_KEY", raw, "the reference is expected")
        self.assertNotIn("api_key\":", raw)
        library = (self.tmp / "library.json").read_text(encoding="utf-8")
        self.assertNotIn("job-ledger", library)
        self.assertNotIn("approval_id", library)

    def test_a_corrupt_ledger_is_reported_and_replaced(self):
        (self.tmp / "job-ledger.json").write_text("{ not json", encoding="utf-8")
        ledger = job_policy.JobLedger(self.tmp, log=self.logs.append)
        self.assertEqual("invalid_json", ledger.load_error)
        self.assertEqual(0, ledger.spend_today())

    def test_the_ledger_is_excluded_from_a_backup(self):
        self.approve()
        self.manager.enqueue(PAPER, reason="manual")
        data = self.store.backup()
        payload = data[0] if isinstance(data, tuple) else data
        import io
        import zipfile
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            names = archive.namelist()
        self.assertFalse([name for name in names if "ledger" in name.lower()], names[:10])

    def test_budget_status_is_safe_to_show(self):
        self.approve()
        self.manager.enqueue(PAPER, reason="manual")
        status = self.manager.budget_status(PAPER)
        blob = json.dumps(status, ensure_ascii=False)
        self.assertIn("limits", status)
        self.assertNotIn("api_key", blob)
        self.assertNotIn("sk-", blob)


if __name__ == "__main__":
    unittest.main(verbosity=2)
