"""Background analysis jobs: queue, worker, state machine and idempotency checks.

The manager never blocks an HTTP request: `/api/import` only records a job and returns.
A single worker thread (bounded by `max_concurrent_jobs`) drains the queue, and every
state change is written with the store's atomic write, so closing the app in the middle
of a run leaves a recoverable library.

State machine
-------------
    (import)                 no provider configured
      new ──► queued ──────────────────────────► waiting_for_provider
                 │                                        │ provider configured
                 │ worker picks it up                     ▼
                 ▼                                    queued
              running ──► ready      (answer saved to data/analyses/<id>.json)
                 │
                 ├──► failed     (provider error / invalid JSON / refusal / timeout)
                 ├──► cancelled  (user asked to stop)
                 └──► queued     (recovered after a restart)

    ready ──► stale   when source_sha256 / prompt_version / model / language changed

Stages inside a run: queued → extracting → chunking → requesting → validating → saving → done.
"""
from __future__ import annotations

import hashlib
import json
import sys
import threading
from contextlib import contextmanager
import time
from pathlib import Path

import os as _os
import credentials
import figures
import job_policy
import metadata
import pdftext
import providers
import quotes
import reading_card
import schema
import secret_guard
from chunker import Chunker
from extraction import PdfTextExtractor


def _safe_problems(summary):
    """Keep only the coded part of a validation summary; never free text from the model.

    Accepts either a summary dict or the exception itself, because `_record_failure` is
    called with both.
    """
    if not isinstance(summary, dict):
        summary = reading_card.summarise_problems(summary)
    codes = [str(code)[:60] for code in (summary.get("codes") or [])][:16]
    details = [secret_guard.redact(str(item))[:120] for item in (summary.get("details") or [])][:16]
    return {"codes": codes, "details": details}


def _safe_code(value, fallback=secret_guard.E_UPSTREAM_ERROR):
    """A fixed-vocabulary code only: callers may display it, so it must not carry text."""
    text = str(value or "")
    return text if text in secret_guard.CODES else fallback


def _public_error(result):
    """(code, message) safe to store in the library and show in the interface.

    The code is a fixed vocabulary member chosen from the adapter *category*: the category
    is a closed set defined in providers.py, so it can be stored, while the adapter's own
    message (which may quote an upstream body) is not.
    """
    if getattr(result, "public_code", ""):
        code = result.public_code
        if code in secret_guard.MESSAGES:
            return code, secret_guard.message_for(code)
    by_category = {
        providers.CATEGORY_AUTH: secret_guard.E_AUTH_FAILED,
        providers.CATEGORY_TIMEOUT: secret_guard.E_TIMEOUT,
        providers.CATEGORY_UNREACHABLE: secret_guard.E_UNREACHABLE,
        providers.CATEGORY_HTTP: secret_guard.E_UPSTREAM_ERROR,
        providers.CATEGORY_BAD_RESPONSE: secret_guard.E_INVALID_RESPONSE,
        providers.CATEGORY_INVALID_JSON: secret_guard.E_INVALID_RESPONSE,
        providers.CATEGORY_REFUSED: secret_guard.E_REFUSED,
        providers.CATEGORY_CANCELLED: secret_guard.E_CANCELLED,
        providers.CATEGORY_CONFIG: secret_guard.E_CREDENTIAL_NOT_BOUND,
        providers.CATEGORY_DISABLED: secret_guard.E_CREDENTIAL_NOT_BOUND,
    }
    code = by_category.get(getattr(result, "category", ""), secret_guard.E_UPSTREAM_ERROR)
    return code, secret_guard.message_for(code)


#: Sentences for failures whose *fix* is knowable, keyed by public code. They win over the
#: category sentence, because "the output ceiling cut the answer off" tells the reader what to
#: change while "the model did not answer with JSON" sends them looking in the wrong place.
ADVICE_BY_CODE = {
    secret_guard.E_OUTPUT_TRUNCATED:
        "输出被上限截断：请提高输出上限（settings.ai.max_output_tokens）或更换模型后重试",
    secret_guard.E_REASONING_ONLY:
        "模型只产出了推理内容、没有输出答案：请提高输出上限或更换模型后重试",
}

PROMPT_VERSION = reading_card.PROMPT_VERSION
RUNNING_TIMEOUT_SECONDS = 900
#: The transport deadline for one request is `min(adapter timeout, request timeout)`, so the
#: adapter has to be built with the same room the reading-card requests ask for. With the old
#: 120 s adapter default a slow reasoning call was cut off after the request had already been
#: sent, which reports as "outcome unknown" and is deliberately never retried.
REQUEST_DEADLINE_SECONDS = reading_card.REQUEST_TIMEOUT_SECONDS
STAGE_PROGRESS = {
    "queued": 0,
    "extracting": 15,
    "chunking": 30,
    "requesting": 55,
    "validating": 80,
    "saving": 95,
    "done": 100,
}
TRANSIENT_CATEGORIES = (providers.CATEGORY_UNREACHABLE, providers.CATEGORY_TIMEOUT)
# Reasons that represent an explicit user action, and therefore record an approval.
# import is deliberately absent: importing a PDF must leave the paper waiting for a click.
APPROVAL_REASONS = ("manual", "force", "retry", "request", "auto",
                    # import records an approval only when the user has switched automatic
                    # analysis on for this target: that switch is the explicit authorisation,
                    # and the caller passes import only in that case.
                    "import")
RETRYABLE_HTTP_CODES = (429, 500, 502, 503, 504)
MAX_ATTEMPTS = 2
BACKOFF_BASE_SECONDS = 1.5
BACKOFF_MAX_SECONDS = 20.0
FIELD_KEYS = reading_card.FIELD_KEYS

def _now():
    return schema._utc_now()


def _age_seconds(stamp):
    """Seconds since an ISO-ish timestamp; large number when unknown."""
    if not stamp:
        return 10 ** 9
    try:
        parsed = time.strptime(str(stamp)[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return 10 ** 9
    return max(0.0, time.time() - time.mktime(parsed))


class AnalysisManager:
    """Owns the analysis queue. All mutations of analysis metadata go through here."""

    def __init__(self, store, provider_factory=None, extractor_factory=None,
                 prompt_version=PROMPT_VERSION, running_timeout=RUNNING_TIMEOUT_SECONDS,
                 max_attempts=MAX_ATTEMPTS, log=None, clock=time.time, ledger=None):
        self.store = store
        self.files = store.files
        self.provider_factory = provider_factory or (
            lambda settings: providers.build_adapter(settings, self.files,
                                                     timeout=REQUEST_DEADLINE_SECONDS))
        self.extractor_factory = extractor_factory or (
            lambda: __import__("extraction").IsolatedPdfTextExtractor(files=self.files))
        self.prompt_version = prompt_version
        self.running_timeout = running_timeout
        self.max_attempts = max_attempts
        self.log = log or (lambda event: None)
        self.clock = clock
        # The job ledger holds approvals, frozen job snapshots and the request budget. It
        # lives next to the library and is never part of a backup.
        self.ledger = ledger if ledger is not None else getattr(store, '_job_ledger', None)
        if self.ledger is None:
            self.ledger = job_policy.JobLedger(store.directory, clock=clock, log=self.log)
        store._job_ledger = self.ledger
        self.ledger._lock = self.store.lock
        self.store._analysis_manager = self
        self._execution = threading.local()

        self._queue = []
        self._queue_lock = threading.Lock()
        self._worker = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._current = {}          # job_id -> {"adapter", "cancel", "paper_id"}
        self._current_lock = threading.Lock()
        self._forced_ids = {}       # paper_id -> True while an explicit re-run is pending
        # paper_id -> True while the *user* asked to cancel. A supersede sets the same cancel
        # event, so the two intents are recorded separately; otherwise a forced re-run would
        # make its own replacement report itself as cancelled.
        self._user_cancelled = {}

    # -- introspection -----------------------------------------------------
    def status(self, paper_id):
        meta = self.store.analysis_meta(paper_id)
        return {"paper_id": paper_id, "analysis": meta}

    def statuses(self):
        return {paper["id"]: paper.get("analysis", {}) for paper in self.store.state["papers"]}

    def queue_depth(self):
        with self._queue_lock:
            pending = list(self._queue)
        with self._current_lock:
            # One entry per in-flight job; the queue reports the distinct papers so a
            # superseded job and its replacement are not double-counted as two papers.
            running = sorted({entry.get("paper_id") for entry in self._current.values()})
        return {"queued": pending, "running": running}

    # -- settings helpers --------------------------------------------------
    def ai_settings(self):
        return self.store.ai_settings()

    def provider(self, settings=None):
        return self.provider_factory(settings or self.ai_settings())

    def provider_enabled(self, settings=None):
        """Whether a usable provider is configured.

        Decided by the *settings*, not by the adapter's class, so an injected adapter in
        a test can never turn a disabled configuration into an active one.
        """
        settings = settings or self.ai_settings()
        return bool(settings.get("provider")) and settings.get("provider") != "none"

    def provider_ready(self, settings=None):
        """(ok, adapter, health) - a disabled, paused or unapproved target is not contacted.

        The health check and the model list are outbound requests that carry the credential.
        "Pause all AI" has to stop them too, otherwise a user who has just switched AI off
        still sees requests leaving the machine - and a freshly typed address would receive a
        credentialled probe before anyone approved it.
        """
        settings = settings or self.ai_settings()
        code, message = self.outbound_blocked(settings)
        if not code and not self.target_approved(settings):
            code, message = (secret_guard.E_CONSENT_REQUIRED,
                             secret_guard.message_for(secret_guard.E_CONSENT_REQUIRED))
        if code:
            # The adapter is a refuse-everything stand-in: no connection is attempted.
            return False, providers.DisabledAdapter(message), providers.Health(
                False, category=code, detail=message)
        effective = self.provider(settings) if self.provider_enabled(settings) else \
            providers.DisabledAdapter("尚未选择本地模型 Provider（settings.ai.provider = none）")
        frozen = dict(settings)
        frozen_credential = credentials.live_identity(settings.get('api_key_env', ''))
        @contextmanager
        def health_guard():
            with self.store.lock:
                current = self.ai_settings()
                for key in ('provider', 'mode', 'base_url', 'api_key_env', 'model',
                            'config_revision', 'credential_revision'):
                    if frozen.get(key) != current.get(key):
                        raise providers.SendDenied(secret_guard.E_TARGET_CHANGED)
                if credentials.live_identity(current.get('api_key_env', '')) != frozen_credential:
                    raise providers.SendDenied(secret_guard.E_TARGET_CHANGED)
                blocked, _ = self.outbound_blocked(current)
                if blocked or self.ledger.load_error or self.ledger.write_error:
                    raise providers.SendDenied(blocked or secret_guard.E_CONSENT_REQUIRED)
                if not self.target_approved(current):
                    raise providers.SendDenied(secret_guard.E_CONSENT_REQUIRED)
                yield
        effective.send_guard = health_guard
        health = effective.health_check()
        return bool(health.ok) and not isinstance(effective, providers.DisabledAdapter), effective, health

    def target_approved(self, settings=None):
        """Whether the configured target has been confirmed in this installation."""
        settings = settings if settings is not None else self.ai_settings()
        if not self.provider_enabled(settings):
            # nothing to approve: a local provider with no credential is not a disclosure
            return True
        return self.ledger.target_approved(self.store.endpoint_identity(settings),
                                           credential_ref=credentials.normalise_ref(
                                               settings.get("api_key_env", "")))

    def _effective_provider(self, settings=None):
        settings = settings or self.ai_settings()
        if not self.provider_enabled(settings):
            return providers.DisabledAdapter(
                "尚未选择本地模型 Provider（settings.ai.provider = none）")
        return self.provider(settings)

    # -- startup -----------------------------------------------------------
    def recover(self):
        """Bring the library to a consistent state at start-up.

        * `running`/`queued` left over from a crash or restart
            - an approval is still valid for this paper, target and credential -> re-queued
              (the budget in the ledger carries over, so nothing is re-paid for)
            - no valid approval (for example the library was restored from a backup, which
              never restores approvals) -> parked as `waiting_for_user`
        * `waiting_for_provider`                            -> stays, unless a provider is ready
        * `ready` whose source/prompt/model/language changed -> stale

        A paper row is state, never permission: this method never turns a record into an
        outbound request on its own.
        """
        settings = self.ai_settings()
        adapter = self._effective_provider(settings)
        disabled = isinstance(adapter, providers.DisabledAdapter)
        self._adapter = adapter
        requeue, stale, waiting, parked = [], [], [], []
        with self.store.lock:
            for paper in self.store.state["papers"]:
                meta = paper.setdefault("analysis", schema.default_analysis())
                status = meta.get("status", "none")
                current_hash = self.files.has_pdf(paper["id"]) and \
                    pdftext.file_sha256(self.files.pdf_path(paper["id"])) or ""
                if status in ("running", "queued"):
                    approval = self.ledger.approval_for(paper["id"])
                    identity = self.store.endpoint_identity(settings)
                    reference = credentials.normalise_ref(settings.get("api_key_env", ""))
                    authorized = bool(
                        approval and approval.get("target_identity") == identity
                        and approval.get("credential_ref", "") == reference)
                    if authorized:
                        self._set(paper, meta, "queued", stage="queued",
                                  message="服务重启，已按原批准继续",
                                  error_code="", error_message="")
                        requeue.append(paper["id"])
                    else:
                        self._set(paper, meta, "waiting_for_user", stage="", progress=0,
                                  message="服务重启后需要重新确认才会发送（不会自动请求模型）",
                                  error_code="", error_message="")
                        parked.append(paper["id"])
                elif status == "waiting_for_provider" and not disabled:
                    self._set(paper, meta, "queued", stage="queued",
                              message="检测到可用的本地模型，已重新排队")
                    requeue.append(paper["id"])
                elif status == "ready":
                    if not self.files.has_analysis(paper["id"]):
                        # The answer file is the product; if it is gone the badge must not
                        # keep claiming `ready`, otherwise the UI offers nothing to retry.
                        self._set(paper, meta, "stale", stage="",
                                  message="这份参考的文件已丢失，需要重新生成",
                                  error_code="answer_missing")
                        stale.append(paper["id"])
                    elif schema.analysis_is_stale(meta, source_sha256=current_hash,
                                                  prompt_version=self.prompt_version,
                                                  model=(settings.get("model") or ""),
                                                  language=paper.get("language", "")):
                        self._set(paper, meta, "stale", stage="",
                                  message="原文或设置已变化，这份参考需要重新生成",
                                  error_code="stale")
                        stale.append(paper["id"])
                if not current_hash and meta.get("source_sha256"):
                    # the PDF is gone: the recorded hash can no longer be trusted
                    meta["source_sha256"] = ""
                    meta["updated_at"] = _now()
            self.store.persist()
        for paper_id in requeue:
            # not forced: the approval recorded above is what authorises the request, and the
            # send gate checks it again with the frozen snapshot
            self.enqueue(paper_id, reason="recover")
        self.log(secret_guard.log_record(event="recover", provider=adapter.name,
                                         count=len(requeue), outcome="disabled" if disabled else "ok"))
        return {"requeued": requeue, "stale": stale, "waiting": waiting,
                "parked": parked, "provider": adapter.name, "disabled": disabled}

    # -- queueing ----------------------------------------------------------
    def resume_waiting(self):
        """Queue every paper that was waiting for a provider. Idempotent."""
        resumed = []
        if not self.provider_enabled():
            return resumed
        for paper in list(self.store.state["papers"]):
            if paper.get("analysis", {}).get("status") == "waiting_for_provider":
                self.enqueue(paper["id"], reason="provider_available")
                resumed.append(paper["id"])
        if resumed:
            self.log(secret_guard.log_record(event="resume_waiting", count=len(resumed)))
        return resumed

    def enqueue(self, paper_id, reason="request", force=False, context=None):
        with self.store.lock:
            return self._enqueue_locked(paper_id, reason, force, context)

    def _enqueue_locked(self, paper_id, reason="request", force=False, context=None):
        """Freeze a job, record `queued` and wake the worker. Returns a status dict.

        Enqueueing captures the user's intent: the snapshot (target, model, credential
        reference, PDF hash, revisions) is frozen here and an approval is recorded when the
        caller represents an explicit request. Nothing is sent until the worker passes the
        send gate, which re-checks the approval and takes budget atomically.
        """
        paper = self.store.paper(paper_id)
        settings = self.ai_settings()
        adapter = self._effective_provider(settings)
        disabled = isinstance(adapter, providers.DisabledAdapter)
        self._adapter = adapter
        limits = self.limits(settings)
        approval_reason = str(reason or "")

        existing = None if force else self.ledger.frozen_job(paper_id)
        if force:
            # A forced re-run replaces the job by *taking over the write*: the older job may
            # still finish its call, but its result is refused at the write step, so the newer
            # answer cannot be overwritten no matter which call returns first. Cancelling it
            # instead would race with the new job's own status transitions.
            self.log(secret_guard.log_record(event="supersede", paper_id=paper_id))
        snapshot = self.snapshot_for(paper_id, reason=approval_reason,
                                     approval_id=(self.ledger.approval_for(paper_id) or {}
                                                  ).get("approval_id", ""),
                                     settings=settings)
        if not force and approval_reason in APPROVAL_REASONS:
            # Only a reason that represents an explicit user action records an approval.
            # Importing a PDF is not one: the paper keeps waiting for the user's click, and
            # nothing is sent on the strength of an import.
            record = self.grant_approval(
                paper_id, model=snapshot.model,
                scope="auto" if approval_reason in ("import_auto", "auto") else "single_paper")
            snapshot = self.snapshot_for(paper_id, reason=approval_reason,
                                         approval_id=record.get("approval_id", ""),
                                         generation=snapshot.generation, settings=settings)
        elif existing is not None and force:
            # a forced re-run is a new generation, so a late result from the old one cannot
            # be mistaken for the current answer
            snapshot = self.snapshot_for(paper_id, reason=approval_reason,
                                         approval_id=existing.approval_id, settings=settings)

        with self.store.lock:
            meta = paper.setdefault("analysis", schema.default_analysis())
            status = meta.get("status", "none")
            already = bool(not force and status == "queued")
            if not force and status == "running":
                return {"paper_id": paper_id, "status": status, "queued": False,
                        "reason": "already_pending"}
            if not force and status == "waiting_for_user" and \
                    approval_reason not in APPROVAL_REASONS:
                # Importing a PDF must not turn the paper into a model call: it stays waiting
                # for the user's explicit request. A caller that *is* the user's request
                # (reason in APPROVAL_REASONS) is allowed through, which is what fixes the
                # manual-first-generation path.
                return {"paper_id": paper_id, "status": status, "queued": False,
                        "reason": "waiting_for_user"}
            if not force and status == "ready" and not schema.analysis_is_stale(
                    meta, source_sha256=snapshot.source_sha256,
                    prompt_version=self.prompt_version, model=snapshot.model,
                    language=snapshot.language):
                return {"paper_id": paper_id, "status": "ready", "queued": False,
                        "reason": "already_ready"}
            if disabled and not force:
                self._set(paper, meta, "waiting_for_provider", stage="",
                          message="尚未配置本地模型；配置后会自动开始分析",
                          error_code="", error_message="",
                          source_sha256=snapshot.source_sha256, language=snapshot.language)
                self.store.persist()
                return {"paper_id": paper_id, "status": "waiting_for_provider", "queued": False,
                        "reason": "provider_disabled"}
            if not already and len(self.queue_depth()["queued"]) >= int(
                    limits.get("max_queue_depth", 20)):
                return {"paper_id": paper_id, "status": status, "queued": False,
                        "reason": job_policy.DENY_QUEUE_FULL}
            if not already:
                if force:
                    self._forced_ids[paper_id] = True
                self._set(paper, meta, "queued", stage="queued", progress=0,
                          message="已加入分析队列" if not disabled else "已加入队列（Provider 未就绪）",
                          error_code="", error_message="", requested_at=_now(),
                          source_sha256=snapshot.source_sha256, provider=snapshot.adapter,
                          adapter=adapter.name, model=snapshot.model,
                          prompt_version=self.prompt_version, language=snapshot.language,
                          answer_file="" if force else meta.get("answer_file", ""))
                self.store.persist()
            self.ledger.freeze_job(snapshot)
        # a fresh job starts with a clean slate: only a cancel that arrives *after* this point
        # applies to it
        self._user_cancelled.pop(paper_id, None)
        self._push(paper_id)
        self.log(secret_guard.log_record(event="enqueue", paper_id=paper_id, reason=reason,
                                         provider=adapter.name, model=snapshot.model,
                                         already_recorded=already, job_id=snapshot.job_id,
                                         count=snapshot.generation))
        return {"paper_id": paper_id, "status": "queued", "queued": True, "reason": reason,
                "job_id": snapshot.job_id, "generation": snapshot.generation}

    def _push(self, paper_id):
        with self._queue_lock:
            if paper_id not in self._queue:
                self._queue.append(paper_id)
        self._wake.set()

    def _pop(self):
        with self._queue_lock:
            return self._queue.pop(0) if self._queue else None

    def requeue_all(self):
        return [self.enqueue(paper["id"], reason="manual", force=True)
                for paper in self.store.state["papers"]]

    # -- worker ------------------------------------------------------------
    def start(self):
        if self._worker and self._worker.is_alive():
            return self._worker
        self._stop.clear()
        self._worker = threading.Thread(target=self._loop, name="analysis-worker", daemon=True)
        self._worker.start()
        self.log(secret_guard.log_record(event="worker_started"))
        return self._worker

    def stop(self, timeout=10):
        self._stop.set()
        self._wake.set()
        worker = self._worker
        if worker:
            worker.join(timeout)
        self._worker = None

    def stop_event_set(self):
        return self._stop.is_set()

    def wait_idle(self, timeout=30, poll=0.05):
        """Block until the queue is empty; used by tests and shutdown."""
        deadline = self.clock() + timeout
        while self.clock() < deadline:
            depth = self.queue_depth()
            if not depth["queued"] and not depth["running"]:
                return True
            time.sleep(poll)
        return False

    def _loop(self):
        limit = max(1, int(self.ai_settings().get("max_concurrent_jobs", 1)))
        slots = threading.Semaphore(limit)
        threads = []
        while not self._stop.is_set():
            paper_id = self._pop()
            if paper_id is None:
                self._wake.wait(0.2)
                self._wake.clear()
                threads = [thread for thread in threads if thread.is_alive()]
                continue
            slots.acquire()
            thread = threading.Thread(target=self._run_slot, args=(paper_id, slots),
                                      name=f"analysis-{paper_id[:8]}", daemon=True)
            thread.start()
            threads.append(thread)
        for thread in threads:
            thread.join(5)
        self.log(secret_guard.log_record(event="worker_stopped"))

    def _run_slot(self, paper_id, slots):
        try:
            self.process(paper_id)
        finally:
            slots.release()

    def process_once(self, timeout=30):
        """Process queued jobs synchronously (tests, or a manual drain)."""
        processed = []
        deadline = self.clock() + timeout
        while self.clock() < deadline:
            paper_id = self._pop()
            if paper_id is None:
                break
            self.process(paper_id)
            processed.append(paper_id)
        return processed

    def _cancel_running(self, paper_id):
        """Best-effort stop of the in-flight jobs for one paper. Returns how many were asked."""
        with self._current_lock:
            running = [entry for entry in self._current.values()
                       if entry.get("paper_id") == paper_id]
        for entry in running:
            entry["cancel"].set()
            entry["adapter"].cancel()
        if running:
            self.log(secret_guard.log_record(event="supersede_cancel", paper_id=paper_id,
                                             count=len(running)))
        return len(running)

    def cancel(self, paper_id):
        """Ask every in-flight call for this paper to stop; the job ends as `cancelled`."""
        with self._current_lock:
            # Every running entry for this paper, not only the newest: a superseded job can
            # still hold a paid-for connection, and leaving it running would both waste the
            # call and let its result arrive after the newer one.
            running = [entry for entry in self._current.values()
                       if entry.get("paper_id") == paper_id]
        for entry in running:
            self._user_cancelled[paper_id] = True
            entry["cancel"].set()
            entry["adapter"].cancel()
        if running:
            self.log(secret_guard.log_record(event="cancel_requested", paper_id=paper_id,
                                             count=len(running)))
            return True
        with self.store.lock:
            paper = self.store.paper(paper_id)
            meta = paper.setdefault("analysis", schema.default_analysis())
            if meta.get("status") in ("none", "queued", "running", "waiting_for_provider"):
                self._set(paper, meta, "cancelled", stage="", message="已取消",
                          error_code="cancelled")
                self.store.persist()
                return True
        return False

    # -- one job -----------------------------------------------------------
    def process(self, paper_id):
        """Run one job to a terminal state. Never raises for provider problems."""
        cancel = threading.Event()
        with self.store.lock:
            snapshot = self.ledger.frozen_job(paper_id)
            if snapshot is None or not self._snapshot_valid(snapshot):
                if snapshot is not None and self.ledger.job_is_current(paper_id, snapshot.job_id):
                    paper = self.store.paper(paper_id)
                    meta = paper.setdefault('analysis', schema.default_analysis())
                    if meta.get('status') in ('queued', 'running'):
                        self.ledger.revoke_approval(paper_id, 'job_inputs_changed')
                        self._set(paper, meta, 'waiting_for_user', stage='', progress=0,
                                  message=secret_guard.message_for(secret_guard.E_TARGET_CHANGED),
                                  error_code=secret_guard.E_TARGET_CHANGED)
                        self.store.persist()
                return
            context = self.build_context(paper_id, snapshot=snapshot)
            adapter = self._effective_provider(self._snapshot_settings(snapshot))
        self._execution.snapshot = snapshot
        self._execution.cancel = cancel
        self._adapter = adapter
        job_id = snapshot.job_id if snapshot is not None else paper_id
        with self._current_lock:
            # Keyed by job, not by paper: with concurrency above one, a second job for the
            # same paper used to overwrite this entry, which made the queue report no running
            # job, let wait_idle() return early and left cancel() unable to reach the adapter
            # of the call that was still in flight.
            self._current[job_id] = {"adapter": adapter, "cancel": cancel,
                                     "paper_id": paper_id}
        started = self.clock()
        try:
            if isinstance(adapter, providers.DisabledAdapter):
                self._transition(paper_id, "waiting_for_provider", stage="",
                                 message="尚未配置本地模型；配置后会开始分析")
                return
            current_status = self.store.analysis_meta(paper_id).get("status", "none")
            if current_status == "cancelled":
                # A queue entry can outlive an explicit cancel: honour the cancel instead
                # of resurrecting the job on the next drain.
                self.log(secret_guard.log_record(event="skip_cancelled", paper_id=paper_id))
                return
            if current_status in ("ready", "stale") and not self._forced_ids.get(paper_id):
                self.log(secret_guard.log_record(event="skip_terminal", paper_id=paper_id, status=current_status))
                return
            attempt = self._next_attempt(paper_id)
            self._transition(paper_id, "running", stage="extracting", progress=STAGE_PROGRESS["extracting"],
                             message="正在抽取原文文本", started_at=_now(), attempt=attempt,
                             error_code="", error_message="", duration_ms=0)
            chunks = self._chunks(paper_id, context)
            self._transition(paper_id, "running", stage="chunking", progress=STAGE_PROGRESS["chunking"],
                             message=f"已切分 {len(chunks)} 段原文")
            if not chunks:
                self._transition(paper_id, "failed", stage="", progress=0,
                                 message="这份 PDF 没有可用文本层，无法生成参考（仍可人工阅读）",
                                 error_code="no_text_layer", completed_at=_now(),
                                 duration_ms=int((self.clock() - started) * 1000))
                return
            # One paper may need more than one reading pass: each pass carries as much of the
            # document as the input budget allows and the next continues where it stopped.
            # A paper that fits yields a single pass, which is exactly the request this
            # program sent before multi-pass existed.
            plan = reading_card.plan_passes(context, chunks, context.get("page_count", 0))
            segments = plan["segments"]
            total_passes = len(segments)
            # How many passes a long paper needs is this program's decision, so the job is
            # allowed to pay for the plan it made: every pass plus the single repair, still
            # under job_policy's own ceiling of four transmissions per job. The pre-send gate
            # below gets the same allowance, otherwise a long paper's own plan would look like
            # an exhausted budget before the first pass of a retry.
            pass_limits = dict(self.limits())
            pass_limits["max_requests_per_job"] = min(
                4, max(int(pass_limits.get("max_requests_per_job") or 2), total_passes + 1))
            if self._blocked_before_send(paper_id, context, started, limits=pass_limits):
                return
            reports, result, request = [], None, None
            # Page images are charged to the job as a whole: the budget is spent pass by pass,
            # and a figure page is only rendered when its own pass is the one being sent.
            figure_budget = int(context.get("max_figure_pages") or 0)
            figures_sent = []
            for index, segment in enumerate(segments):
                # The continue prompt needs the *previous* pass's answer, which is what `result`
                # still holds before this pass's dispatch overwrites it.
                previous_raw = result.raw_text if result is not None else ""
                images = self._pass_figures(context, adapter, segment, figure_budget)
                figure_budget -= len(images)
                figure_pages = [item["page"] for item in images]
                figures_sent.extend(figure_pages)
                # Validation needs to know which pages the model has seen as pictures: a quote
                # that exists nowhere in the text layer may simply live inside one of them.
                context["figures_sent"] = list(figures_sent)
                if index == 0:
                    request = self._request(context, segment["chunks"], figure_pages=figure_pages)
                else:
                    request = reading_card.build_continue_request(
                        context, segment["chunks"], context.get("page_count", 0),
                        previous_raw,
                        model=context["model"], pass_index=index + 1,
                        pass_total=total_passes, segment=segment,
                        figure_pages=figure_pages)
                if images:
                    request.images = tuple(images)
                reports.append(getattr(request, "input_report", None))
                figure_note_text = (f"，含 {len(images)} 页图表图片" if images else "")
                if total_passes == 1:
                    message = (f"正在请求 {adapter.name}（{request.model or '默认模型'}）"
                               + (f"，含 {len(images)} 页图表图片" if images else ""))
                else:
                    message = (f"正在读第 {index + 1}/{total_passes} 段"
                               f"（PDF 第 {segment['first_page']}–{segment['last_page']} 页"
                               + figure_note_text + "）")
                self._transition(paper_id, "running", stage="requesting",
                                 progress=min(STAGE_PROGRESS["validating"] - 1,
                                              STAGE_PROGRESS["requesting"]
                                              + int(20 * index / max(1, total_passes))),
                                 message=message)
                result, _reservation = self._dispatch(
                    paper_id, context, adapter, request,
                    kind="first" if index == 0 else "pass", cancel=cancel,
                    limits=pass_limits)
                if (not result.ok and images
                        and result.public_code in (secret_guard.E_OUTPUT_TRUNCATED,
                                                   secret_guard.E_REASONING_ONLY)):
                    # The attached pages pushed this pass past the output ceiling: a reasoning
                    # model spends its budget thinking about the images first. The answer the
                    # reader came for is the text one, so the same pass is sent again without
                    # pictures - the prompt is rebuilt, so nothing claims images that are absent.
                    self.log(secret_guard.log_record(event="figures_dropped", paper_id=paper_id,
                                                     category="fallback",
                                                     public_code=result.public_code,
                                                     pass_index=index + 1))
                    images = []
                    figures_sent = [page for page in figures_sent if page not in figure_pages]
                    figure_budget += len(figure_pages)
                    figure_pages = []
                    if index == 0:
                        request = self._request(context, segment["chunks"])
                    else:
                        request = reading_card.build_continue_request(
                            context, segment["chunks"], context.get("page_count", 0),
                            previous_raw,
                            model=context["model"], pass_index=index + 1,
                            pass_total=total_passes, segment=segment)
                    reports[-1] = getattr(request, "input_report", None)
                    result, _reservation = self._dispatch(
                        paper_id, context, adapter, request, kind="pass-retry-text",
                        cancel=cancel, limits=pass_limits)
                self.log(secret_guard.log_record(event="provider_call", paper_id=paper_id,
                                                 provider=adapter.name,
                                                 model=result.model or request.model,
                                                 duration_ms=result.duration_ms,
                                                 category=result.category, ok=result.ok,
                                                 public_code=result.public_code,
                                                 parse_detail=getattr(result, "parse_detail", ""),
                                                 pass_index=index + 1,
                                                 pass_total=total_passes,
                                                 http_status=result.http_status,
                                                 request_id=result.request_id))
                if cancel.is_set() or result.category == providers.CATEGORY_CANCELLED:
                    if self._user_cancelled.pop(paper_id, False):
                        self._transition(paper_id, "cancelled", stage="", progress=0,
                                         message="已取消", error_code="cancelled",
                                         completed_at=_now(),
                                         duration_ms=int((self.clock() - started) * 1000))
                    else:
                        # the stop came from a newer job taking over, not from the user: record
                        # it and leave the status to the job that is now current
                        snapshot = context.get("job")
                        if snapshot is not None:
                            self.ledger.record_outcome(snapshot.job_id, "cancelled_superseded",
                                                       secret_guard.E_STALE_GENERATION)
                        self.log(secret_guard.log_record(event="result_superseded",
                                                         paper_id=paper_id))
                    return
                if self._superseded(paper_id, context):
                    return
                if not result.ok:
                    if total_passes > 1:
                        # Say how far it got: a later pass failing must not read like the first
                        # one failing, or the reader cannot tell coverage from a broken model.
                        result.message = (f"第 {index + 1}/{total_passes} 段失败："
                                          f"{result.message}")
                    self._fail(paper_id, result, started, adapter.name, request.model,
                               provider=context.get("provider") or adapter.name)
                    return
            # What the model was shown is the union of the passes, not the last pass alone.
            context["input_report"] = reading_card.merge_input_reports(
                reports, context.get("page_count", 0), chunks_total=len(chunks),
                budget=plan.get("budget", 0), covers_all=plan["covers_all"],
                figure_pages=figures_sent)
            context["passes"] = total_passes
            answer, validation = self._accept_or_repair(
                paper_id, context, chunks, adapter, result, cancel, started,
                superseded=lambda: self._superseded(paper_id, context),
                limits=pass_limits)
            if answer is None:
                return
            if not self._generation_still_current(paper_id, context, started):
                # A newer job for this paper replaced this one while the request was in
                # flight. Its result must not overwrite the newer answer: the superseded job
                # is recorded and dropped. The call is already charged - the ledger keeps it,
                # because it did reach the provider.
                return
            verification = answer["verification"]
            input_info = answer.get("input", {})
            if self._superseded(paper_id, context):
                return
            self._transition(paper_id, "running", stage="saving", progress=STAGE_PROGRESS["saving"],
                             message="正在保存参考分析")
            with self.store.lock:
                if not self._snapshot_valid(snapshot) or cancel.is_set():
                    return
                self.store.write_analysis_answer(paper_id, answer)
            passes = int(input_info.get("passes") or 1)
            truncated_note = ""
            if input_info.get("truncated"):
                truncated_note = (f"；原文 {verification['page_count']} 页，"
                                  f"已分 {passes} 段送入第 {input_info.get('first_page')}–"
                                  f"{input_info.get('last_page')} 页，其余未送入")
            elif passes > 1:
                truncated_note = (f"；原文 {verification['page_count']} 页，"
                                  f"已分 {passes} 段读完全文")
            self._transition(paper_id, "ready", stage="done", progress=100,
                             message=(f"参考分析已就绪（{verification['field_count']} 个字段，"
                                      f"{verification['verified_quotes']} 条引文已核对"
                                      + ("，已修复 JSON" if validation.get("repaired") else "")
                                      + truncated_note + "）"),
                             completed_at=_now(), error_code="", error_message="",
                             duration_ms=int((self.clock() - started) * 1000),
                             answer_file=f"{paper_id}.json",
                             source_sha256=context["source_sha256"], model=result.model or request.model,
                             provider=context["provider"], adapter=adapter.name,
                             prompt_version=self.prompt_version, language=context["language"])
        except Exception as exc:                                   # noqa: BLE001 - reported, never raised
            self.log(secret_guard.log_record(event="worker_error", paper_id=paper_id,
                                             category=type(exc).__name__))
            self._transition(paper_id, "failed", stage="", progress=0,
                             message=secret_guard.message_for(secret_guard.E_UPSTREAM_ERROR),
                             error_code=secret_guard.E_UPSTREAM_ERROR, completed_at=_now(),
                             error_message=f"内部错误（{type(exc).__name__}）")
        finally:
            if self.ledger.job_is_current(paper_id, job_id):
                self._forced_ids.pop(paper_id, None)
            self._execution.snapshot = None
            self._execution.cancel = None
            with self._current_lock:
                self._current.pop(job_id, None)

    # -- the send gate -----------------------------------------------------
    def _blocked_before_send(self, paper_id, context, started, limits=None):
        """Stop the job when no outbound call may happen. Returns True when it stopped.

        `limits` is the same allowance the send loop will use, so the pre-check speaks about
        the plan actually about to run rather than about the configured default.
        """
        code, message = self.outbound_blocked()
        if code:
            self._transition(paper_id, "waiting_for_user", stage="", progress=0,
                             message=message, error_code=code, error_message=message,
                             completed_at=_now(),
                             duration_ms=int((self.clock() - started) * 1000))
            self.log(secret_guard.log_record(event="send_blocked", paper_id=paper_id,
                                             code=code))
            return True
        snapshot = context.get("job")
        _probe, code = job_policy.decide_send(self.ledger, snapshot, limits or self.limits(),
                                             kind="precheck", reserve=False)
        if code == job_policy.DENY_OK:
            # the policy is satisfied; the budget is taken later, immediately before the
            # request, so the check here costs nothing
            return False
        message = job_policy.code_message(code)
        if code in (job_policy.DENY_DAY_LIMIT, job_policy.DENY_JOB_LIMIT):
            # A budget refusal is not a transient failure: retrying would spend the same
            # money in a loop. The job is recorded as failed with the fixed code, and the
            # user can raise the limit or wait for the window to move on.
            self._transition(paper_id, "failed", stage="", progress=0, message=message,
                             error_code=(secret_guard.E_DAY_LIMIT
                                         if code == job_policy.DENY_DAY_LIMIT
                                         else secret_guard.E_JOB_LIMIT),
                             error_message=message, completed_at=_now(),
                             duration_ms=int((self.clock() - started) * 1000))
            self.log(secret_guard.log_record(event="send_blocked", paper_id=paper_id, code=code))
            return True
        status = "waiting_for_user" if code in (job_policy.DENY_NO_APPROVAL,
                                                job_policy.DENY_TARGET_CHANGED,
                                                job_policy.DENY_CREDENTIAL_CHANGED,
                                                job_policy.DENY_PAUSED,
                                                job_policy.DENY_PROFILE_DISABLED) else "failed"
        self._transition(paper_id, status, stage="", progress=0, message=message,
                         error_code=code, error_message=message, completed_at=_now(),
                         duration_ms=int((self.clock() - started) * 1000))
        self.log(secret_guard.log_record(event="send_blocked", paper_id=paper_id, code=code))
        return True

    def _dispatch(self, paper_id, context, adapter, request, kind="first", cancel=None,
                  limits=None):
        """Reserve budget, send once, settle. Returns (result, reservation).

        The reservation is taken immediately before the socket is opened, so a refusal costs
        nothing. `outcome_unknown` means the request left the machine and the provider's
        answer was never seen: the budget stays charged and the job is not retried
        automatically, because a retry could be paid for twice.

        `limits` overrides the configured per-job allowance for this one call. The reading
        passes use it: how many passes a long paper needs is this program's decision, so the
        job must be able to pay for the plan it made (still bounded by job_policy's own
        ceiling). Ordinary calls leave it None and get exactly the configured limits.
        """
        limits = limits or self.limits()
        snapshot = context.get("job")
        if cancel is not None and cancel.is_set():
            return providers.AdapterResult(False, category=providers.CATEGORY_CANCELLED,
                                           public_code=secret_guard.E_CANCELLED,
                                           message="已取消"), None
        reservation, code = job_policy.decide_send(self.ledger, snapshot, limits,
                                                   kind=kind)
        if reservation is None:
            message = job_policy.code_message(code)
            result = providers.AdapterResult(
                False, category=providers.CATEGORY_CONFIG,
                public_code=(secret_guard.E_PAUSED if code == job_policy.DENY_PAUSED else
                             secret_guard.E_CONSENT_REQUIRED if code == job_policy.DENY_NO_APPROVAL
                             else secret_guard.E_DAY_LIMIT if code == job_policy.DENY_DAY_LIMIT
                             else secret_guard.E_BUDGET_BLOCKED),
                message=message)
            self.log(secret_guard.log_record(event="send_blocked", paper_id=paper_id, code=code))
            return result, None
        # Every transmission inside this call is charged. A parameter adaptation re-sends the
        # whole prompt, so it takes its own reservation through this hook; when the budget
        # cannot cover it, the adapter refuses to re-send instead of sending for free.
        extra = []

        def on_retransmit(parameter):
            if (cancel is not None and cancel.is_set()) or self._stop.is_set():
                return False
            if not self._snapshot_valid(snapshot):
                return False
            reservation, code = job_policy.decide_send(self.ledger, snapshot, limits,
                                                       kind=f"{kind}:adapt")
            if reservation is None:
                self.log(secret_guard.log_record(event="retransmit_refused", paper_id=paper_id,
                                                 code=code, parameter=str(parameter)[:24]))
                return False
            extra.append(reservation)
            return True

        adapter.send_guard = lambda: self._send_guard(snapshot, cancel)
        adapter.on_retransmit = on_retransmit
        try:
            try:
                result = adapter.complete_json(request, on_start=None)
            except Exception:                                    # noqa: BLE001
                sent = bool(getattr(adapter, "request_sent", True))
                transmissions = getattr(adapter, 'transmissions', None)
                for index, item in enumerate(extra, start=2):
                    self.ledger.settle(item, sent=(True if transmissions is None else
                                                  transmissions >= index), note="adapt")
                self.ledger.settle(reservation, sent=sent, note="exception")
                raise
        finally:
            adapter.on_retransmit = None
        # Fail safe: an adapter that does not report the fact counts as "sent", so a request
        # that may have left the machine is charged rather than refunded.
        sent = bool(getattr(adapter, "request_sent", True))
        # A re-send only happens after the provider replied, so those transmissions did leave
        # the machine: they stay charged whatever happens next.
        transmissions = getattr(adapter, 'transmissions', None)
        for index, item in enumerate(extra, start=2):
            self.ledger.settle(item, sent=(True if transmissions is None else
                                          transmissions >= index), note="adapt")
        if sent and not result.ok and result.category in (
                providers.CATEGORY_TIMEOUT, providers.CATEGORY_UNREACHABLE):
            # the provider may have accepted and billed the call
            result.outcome_unknown = True
        self.ledger.settle(reservation, sent=sent, ok=result.ok,
                           note="" if result.ok else result.category)
        return result, reservation

    def _job_id(self, paper_id):
        snapshot = getattr(self._execution, "snapshot", None) or self.ledger.frozen_job(paper_id)
        return snapshot.job_id if snapshot else ""

    def _backoff_seconds(self, attempts, result):
        """Bounded, jitter-free backoff. A `Retry-After` header wins when it fits the cap."""
        retry_after = float(getattr(result, "retry_after_seconds", 0) or 0)
        if retry_after > 0:
            return min(retry_after, BACKOFF_MAX_SECONDS)
        return min(BACKOFF_BASE_SECONDS ** max(1, attempts), BACKOFF_MAX_SECONDS)

    def _retryable(self, result):
        """Which failures may be retried at all.

        401/403, TLS failures, policy refusals and secret-echo blocks are never retried: the
        answer will not change, and a retry would spend money to learn the same thing.
        """
        if result.public_code in (secret_guard.E_AUTH_FAILED, secret_guard.E_TLS_FAILED,
                                  secret_guard.E_ENDPOINT_BLOCKED,
                                  secret_guard.E_ENDPOINT_INVALID,
                                  secret_guard.E_REDIRECT_BLOCKED,
                                  secret_guard.E_SECRET_ECHO_BLOCKED,
                                  secret_guard.E_CREDENTIAL_MISSING,
                                  secret_guard.E_CREDENTIAL_NOT_BOUND,
                                  secret_guard.E_CREDENTIAL_INVALID,
                                  secret_guard.E_PAUSED, secret_guard.E_CONSENT_REQUIRED,
                                  secret_guard.E_DAY_LIMIT, secret_guard.E_BUDGET_BLOCKED):
            return False
        if result.category in TRANSIENT_CATEGORIES:
            return True
        return result.http_status in RETRYABLE_HTTP_CODES

    def _superseded(self, paper_id, context):
        """True when a newer job for this paper has replaced this one (records it once)."""
        snapshot = context.get("job")
        job_id = snapshot.job_id if snapshot is not None else ""
        if not job_id or self.ledger.job_is_current(paper_id, job_id):
            return False
        self.ledger.record_outcome(job_id, "superseded", secret_guard.E_STALE_GENERATION)
        self.log(secret_guard.log_record(event="result_superseded", paper_id=paper_id,
                                         job_id=job_id))
        return True

    def _generation_still_current(self, paper_id, context, started):
        """False when a newer job for this paper has replaced this one.

        The answer file and the `ready` state describe *the current job*. Writing them from a
        superseded job would replace a newer answer with an older one - and the two calls were
        both paid for, so nothing else would reveal the swap.
        """
        snapshot = context.get("job")
        job_id = snapshot.job_id if snapshot is not None else ""
        if job_id and not self.ledger.job_is_current(paper_id, job_id):
            # This job was replaced while its call was in flight. Its result is dropped and it
            # writes no status: the paper belongs to the current job, which is still working.
            self.ledger.record_outcome(job_id, "superseded", secret_guard.E_STALE_GENERATION)
            self.log(secret_guard.log_record(event="result_superseded", paper_id=paper_id,
                                             job_id=job_id))
            return False
        return True

    def _fail(self, paper_id, result, started, provider_name, model, provider=""):
        executing = getattr(self._execution, "snapshot", None)
        if executing is not None and not self._snapshot_valid(executing):
            self.ledger.record_outcome(executing.job_id, "superseded")
            return
        attempts = self._attempts_so_far(paper_id)
        unknown = bool(getattr(result, "outcome_unknown", False))
        retryable = (not unknown and attempts < self.max_attempts
                     and self._retryable(result))
        duration = int((self.clock() - started) * 1000)
        public_code, public_message = _public_error(result)
        if unknown:
            self.ledger.record_outcome(getattr(result, "job_id", "") or self._job_id(paper_id),
                                       "unknown", public_code)
            self._transition(paper_id, "failed", stage="", progress=0,
                             message=secret_guard.message_for(secret_guard.E_OUTCOME_UNKNOWN),
                             error_code=secret_guard.E_OUTCOME_UNKNOWN,
                             error_message=secret_guard.message_for(
                                 secret_guard.E_OUTCOME_UNKNOWN),
                             completed_at=_now(), duration_ms=duration, model=model,
                             provider=provider or provider_name)
            self.log(secret_guard.log_record(event="outcome_unknown", paper_id=paper_id,
                                             category=result.category,
                                             public_code=public_code, attempt=attempts))
            return
        if retryable:
            # Only fixed text may be stored or shown; the upstream sentence stays in memory.
            self._transition(paper_id, "queued", stage="queued",
                             message=f"{public_message}稍后自动重试",
                             error_code=public_code, error_message=public_message,
                             duration_ms=duration)
            delay = self._backoff_seconds(attempts, result)
            if delay:
                # bounded, cancellable backoff; a 429 with Retry-After is honoured up to the
                # cap, and the wait happens before the retry takes budget
                self._stop.wait(min(delay, BACKOFF_MAX_SECONDS))
            if executing is not None and not self._snapshot_valid(executing):
                return
            self._push(paper_id)
            self.log(secret_guard.log_record(event="retry", paper_id=paper_id,
                                             category=result.category, attempt=attempts,
                                             public_code=public_code))
            return
        advice = ADVICE_BY_CODE.get(public_code) or {
            providers.CATEGORY_UNREACHABLE: "启动本机模型服务后点击重试",
            providers.CATEGORY_TIMEOUT: "模型响应过慢，可换更小的模型或降低段落上限后重试",
            providers.CATEGORY_AUTH: "凭据被拒绝：请核对服务商控制台里的 Key 与绑定",
            providers.CATEGORY_CONFIG: "检查模型名称与接口地址",
            providers.CATEGORY_INVALID_JSON: "模型没有按要求输出 JSON，可重试或更换模型",
            providers.CATEGORY_REFUSED: "模型拒绝了这个请求；请自行阅读原文并记录",
            providers.CATEGORY_HTTP: "服务商返回错误，已按固定分类记录",
        }.get(result.category, "可稍后重试")
        self._transition(paper_id, "failed", stage="", progress=0,
                         message=advice, error_code=public_code,
                         error_message=public_message,
                         completed_at=_now(), duration_ms=duration, model=model,
                         provider=provider or provider_name,
                         adapter=provider_name)
        self.log(secret_guard.log_record(event="failed", paper_id=paper_id,
                                         category=result.category, provider=provider_name,
                                         model=model, duration_ms=duration,
                                         public_code=public_code,
                                         parse_detail=getattr(result, "parse_detail", ""),
                                         http_status=result.http_status,
                                         request_id=result.request_id))

    # -- job identity ------------------------------------------------------
    def current_profile_id(self, settings=None):
        """A stable id for the configured connection: one profile in this version."""
        settings = settings if settings is not None else self.ai_settings()
        identity = self.store.endpoint_identity(settings)
        return "prof-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]

    def _snapshot_settings(self, snapshot):
        return {"provider": snapshot.adapter, "mode": snapshot.mode,
                "base_url": snapshot.base_url, "model": snapshot.model,
                "api_key_env": snapshot.credential_ref,
                "max_output_tokens": snapshot.max_output_tokens,
                "config_revision": snapshot.config_revision,
                "credential_revision": snapshot.credential_revision}

    def _snapshot_valid(self, snapshot):
        if snapshot is None or not self.ledger.job_is_current(snapshot.paper_id, snapshot.job_id):
            return False
        current = self.ai_settings()
        frozen = self._snapshot_settings(snapshot)
        for key in ("provider", "mode", "base_url", "model", "api_key_env",
                    "max_output_tokens", "config_revision", "credential_revision"):
            if current.get(key) != frozen.get(key):
                return False
        if credentials.live_identity(snapshot.credential_ref) != snapshot.credential_identity:
            return False
        if self.prompt_version != snapshot.prompt_version:
            return False
        if self.files.has_pdf(snapshot.paper_id):
            return pdftext.file_sha256(self.files.pdf_path(snapshot.paper_id)) == snapshot.source_sha256
        return not snapshot.source_sha256

    @contextmanager
    def _send_guard(self, snapshot, cancel):
        # The lock covers the last permission check and writing HTTP bytes, but
        # never the response read, so cancellation remains available.
        with self.store.lock:
            if (cancel is not None and cancel.is_set()) or self._stop.is_set():
                raise providers.SendDenied(secret_guard.E_CANCELLED)
            if not self._snapshot_valid(snapshot):
                raise providers.SendDenied(secret_guard.E_STALE_GENERATION)
            _, code = job_policy.decide_send(self.ledger, snapshot, self.limits(), reserve=False)
            if code != job_policy.DENY_OK:
                raise providers.SendDenied(secret_guard.E_CONSENT_REQUIRED)
            yield

    def snapshot_for(self, paper_id, reason="", approval_id="", generation=None, settings=None):
        """Freeze everything a job may rely on. Never contains a credential value."""
        settings = settings if settings is not None else self.ai_settings()
        paper = self.store.paper(paper_id)
        digest = pdftext.file_sha256(self.files.pdf_path(paper_id)) \
            if self.files.has_pdf(paper_id) else ""
        previous = self.ledger.frozen_job(paper_id)
        generation = int(generation if generation is not None
                         else ((previous.generation + 1) if previous else 1))
        return job_policy.JobSnapshot(
            job_id=job_policy.new_id("job-"), paper_id=paper_id, generation=generation,
            source_sha256=digest, profile_id=self.current_profile_id(settings),
            adapter=str(settings.get("provider") or "none"),
            mode=str(settings.get("mode") or ""),
            target_identity=self.store.endpoint_identity(settings),
            model=str(settings.get("model") or ""),
            credential_ref=credentials.normalise_ref(settings.get("api_key_env", "")),
            credential_revision=int(settings.get("credential_revision") or 1),
            config_revision=int(settings.get("config_revision") or 1),
            policy_version=job_policy.POLICY_VERSION, prompt_version=self.prompt_version,
            language=str(paper.get("language") or ""), approval_id=approval_id,
            budget_id=job_policy.new_id("bud-"), created_at=job_policy._utc(self.clock()),
            reason=reason, base_url=str(settings.get("base_url") or ""),
            max_output_tokens=int(settings.get("max_output_tokens") or 16384),
            vision=bool(settings.get("vision")),
            max_figure_pages=int(settings.get("max_figure_pages") or 0),
            credential_identity=credentials.live_identity(settings.get('api_key_env', '')))

    def approve_target(self, settings=None):
        """Record the user's confirmation of the configured target.

        This is target-level consent, produced by the user saving that address: it is what
        lets a health check or model list reach it. It authorises no paper, and changing the
        address or the credential reference replaces it.
        """
        settings = settings if settings is not None else self.ai_settings()
        if not self.provider_enabled(settings):
            return {}
        identity = self.store.endpoint_identity(settings)
        reference = credentials.normalise_ref(settings.get("api_key_env", ""))
        self.ledger.approve_target(identity, credential_ref=reference,
                                   model=str(settings.get("model") or ""))
        try:
            return self.store.approve_target(settings)
        except (OSError, ValueError) as exc:
            self.log(secret_guard.log_record(event="outbound_state_write_failed",
                                             outcome=type(exc).__name__))
            return {}

    def grant_approval(self, paper_id, model="", scope="single_paper"):
        with self.store.lock:
            return self._grant_approval_locked(paper_id, model, scope)

    def _grant_approval_locked(self, paper_id, model="", scope="single_paper"):
        """Record the user's explicit go-ahead for this paper and the current target."""
        settings = self.ai_settings()
        identity = self.store.endpoint_identity(settings)
        reference = credentials.normalise_ref(settings.get("api_key_env", ""))
        record = self.ledger.grant_approval(
            paper_id, {"target_identity": identity, "credential_ref": reference,
                       "config_revision": settings.get("config_revision", 1),
                       "credential_revision": settings.get("credential_revision", 1),
                       "credential_identity": credentials.live_identity(reference),
                       "source_sha256": pdftext.file_sha256(self.files.pdf_path(paper_id))
                            if self.files.has_pdf(paper_id) else ""},
            model=model or str(settings.get("model") or ""), scope=scope)
        self.ledger.approve_target(identity, credential_ref=reference,
                                   model=model or str(settings.get("model") or ""))
        return record

    def revoke_approvals(self, reason=""):
        """Called when the target, credential or model changes."""
        revoked = 0
        for paper in self.store.state.get("papers", []):
            paper_id = paper.get("id", "")
            if paper_id and self.ledger.revoke_approval(paper_id, reason):
                revoked += 1
        self.ledger.revoke_targets()
        return revoked

    def limits(self, settings=None):
        return job_policy.limits_from_settings(settings if settings is not None
                                               else self.ai_settings())

    def budget_status(self, paper_id=""):
        limits = self.limits()
        job = self.ledger.frozen_job(paper_id) if paper_id else None
        return {"limits": limits, "spent_last_24h": self.ledger.spend_today(),
                "job_spent": self.ledger.job_spend(job.job_id) if job else 0,
                "journal": self.ledger.entries_for(job.job_id)[-8:] if job else [],
                "ledger": self.ledger.status()}

    # -- pauses and profile switches ---------------------------------------
    def pause(self, paused=True, reason="user"):
        return self.ledger.set_paused(paused, reason)

    def paused(self):
        return self.ledger.paused()

    def set_profile_disabled(self, profile_id, disabled=True):
        return self.ledger.set_profile_disabled(profile_id, disabled)

    def outbound_blocked(self, settings=None):
        """(code, message) when no outbound call may happen at all, else ('', '')."""
        if self.ledger.paused():
            return (secret_guard.E_PAUSED, secret_guard.message_for(secret_guard.E_PAUSED))
        profile_id = self.current_profile_id(settings)
        if self.ledger.profile_disabled(profile_id):
            return (secret_guard.E_PROFILE_DISABLED,
                    secret_guard.message_for(secret_guard.E_PROFILE_DISABLED))
        return "", ""

    # -- context and prompts ----------------------------------------------
    def build_context(self, paper_id, snapshot=None):
        """Everything the request builder needs, taken from ONE frozen snapshot.

        `snapshot` is passed by the worker: reading the settings again here is what used to
        let a job run with a mixed identity after the user changed something mid-flight.
        """
        paper = self.store.paper(paper_id)
        snapshot = snapshot or self.ledger.frozen_job(paper_id) or self.snapshot_for(paper_id)
        digest = snapshot.source_sha256
        if not digest and self.files.has_pdf(paper_id):
            digest = pdftext.file_sha256(self.files.pdf_path(paper_id))
        return {
            "paper_id": paper_id,
            "title": paper.get("title", ""),
            "language": paper.get("language", "") or snapshot.language,
            "paper_type": paper.get("type", ""),
            "model": snapshot.model,
            "provider": snapshot.adapter,
            "prompt_version": snapshot.prompt_version or self.prompt_version,
            "source_sha256": digest,
            "pdf_path": self.files.pdf_path(paper_id) if self.files.has_pdf(paper_id) else None,
            "job": snapshot,
            "job_id": snapshot.job_id,
            "generation": snapshot.generation,
            "target_identity": snapshot.target_identity,
            "approval_id": snapshot.approval_id,
            # Frozen with the job: whether page images may be sent, and how many.
            "vision": bool(getattr(snapshot, "vision", False)),
            "max_figure_pages": int(getattr(snapshot, "max_figure_pages", 0) or 0),
            "figure_pages": [],
        }

    def _chunks(self, paper_id, context):
        """Return (chunks, page_index, page_count); page_index verifies quotes."""
        if not context["pdf_path"]:
            context["page_index"] = quotes.PageTextIndex([])
            context["page_count"] = 0
            context["prompt_pages"] = []
            return []
        extractor = self.extractor_factory()
        result = extractor.extract(context["pdf_path"], paper_id=paper_id)
        context["page_count"] = getattr(result, "page_count", 0) or 0
        context["page_index"] = quotes.PageTextIndex(getattr(result, "pages", []) or [])
        if not result.ok:
            self.last_extraction = {"code": result.code, "message": result.message}
            return []
        self.last_extraction = {"code": result.code, "message": result.message,
                                "pages": result.page_count, "backend": result.backend}
        self._fill_metadata(paper_id, getattr(result, "pages", []) or [])
        # Which pages carry figures/charts: rendering them is decided per pass, right before a
        # request goes out, so a job only pays for the images it actually sends.
        try:
            context["figure_pages"] = figures.figure_pages(getattr(result, "pages", []) or [])
        except Exception:                                     # noqa: BLE001 - never fail a job
            context["figure_pages"] = []
        return extractor.chunks(result, document_id=paper_id)

    def _pass_figures(self, context, adapter, segment, budget):
        """Page renders for the figure pages inside one pass, bounded by the job's budget.

        Empty when the reader left vision off, when the adapter cannot carry images (Ollama's
        own API cannot), when no renderer is installed, or when the pages are too large - in
        every one of those cases the request simply goes out as text.
        """
        if budget <= 0 or not context.get("vision"):
            return []
        if not getattr(adapter, "supports_images", False):
            return []
        inside = [page for page in context.get("figure_pages", [])
                  if page in set(segment.get("pages_used") or ())]
        if not inside:
            return []
        images, note = figures.render_pages(context.get("pdf_path"), inside[:budget])
        paper_id = context.get("job").paper_id if context.get("job") else ""
        if note:
            self.log(secret_guard.log_record(event="figures_skipped", paper_id=paper_id,
                                             category="render", count=len(inside) - len(images)))
        if images:
            self.log(secret_guard.log_record(event="figures_attached", paper_id=paper_id,
                                             count=len(images)))
        return images

    def _fill_metadata(self, paper_id, pages):
        """Fill still-empty bibliographic fields from the text we just extracted.

        Free: this extraction already happened for the analysis, so authors/year/venue/DOI are
        filled without a second pass over the PDF and without any AI call. Only empty fields
        are written (see Store.apply_metadata), and the title is left to the import path.
        """
        try:
            parsed = metadata.parse_metadata(pages)
            changed = self.store.apply_metadata(paper_id, parsed)
        except Exception as exc:                             # noqa: BLE001 - never fail a job
            self.log(secret_guard.log_record(event="metadata_failed", paper_id=paper_id,
                                             category=type(exc).__name__))
            return
        if changed:
            self.log(secret_guard.log_record(event="metadata_filled", paper_id=paper_id,
                                             count=len(changed)))

    def _request(self, context, chunks, figure_pages=()):
        request = reading_card.build_request(context, chunks, context.get("page_count", 0),
                                            model=context["model"], figure_pages=figure_pages)
        context["input_report"] = getattr(request, "input_report", None)
        return request

    def _repair_request(self, context, chunks, bad_text, problems):
        return reading_card.build_repair_request(context, chunks, context.get("page_count", 0),
                                                 bad_text, problems, model=context["model"])

    def _validate(self, context, payload):
        """Normalise a model answer; raises reading_card.ValidationError on any problem."""
        return reading_card.validate_reading_card(
            payload, context.get("page_index") or quotes.PageTextIndex([]),
            context.get("page_count", 0), paper_id=context["paper_id"],
            prompt_version=self.prompt_version, provider=context["provider"],
            model=context["model"], language=context["language"],
            source_sha256=context["source_sha256"], created_at=_now(),
            input_report=context.get("input_report"),
            figure_pages=tuple(context.get("figures_sent") or ()))

    # -- validation, one repair attempt ------------------------------------
    def _secret_echo(self, paper_id, result):
        """Refuse a model answer that carries a credential, before it is used anywhere.

        The check covers the parsed payload and the raw text, so a secret in any field
        (overall, terms, warnings, …) blocks the whole response instead of just the answer
        string. A blocked response is never saved, never shown and never sent back for
        repair, because the repair prompt would carry it to the provider again.
        """
        if secret_guard.contains_secret(result.raw_text) or secret_guard.contains_secret(result.payload):
            self.log(secret_guard.log_record(event="secret_echo_blocked", paper_id=paper_id,
                                             public_code=secret_guard.E_SECRET_ECHO_BLOCKED))
            self._transition(paper_id, "failed", stage="", progress=0,
                             message=secret_guard.message_for(secret_guard.E_SECRET_ECHO_BLOCKED),
                             error_code=secret_guard.E_SECRET_ECHO_BLOCKED,
                             completed_at=_now())
            return True
        return False

    def _accept_or_repair(self, paper_id, context, chunks, adapter, result, cancel, started,
                          superseded=None, limits=None):
        """Validate, allow exactly one repair request, then fail.

        `limits` is the same allowance the passes ran under. A long paper legitimately spends
        more than the configured per-job default (one request per pass), and the repair is the
        step that turns a mis-shaped answer into a usable one - leaving it on the *configured*
        ceiling blocked that repair with `budget_blocked` on a three-pass job.

        Returns (answer, validation) or (None, None) after recording a failure.
        """
        if superseded is not None and superseded():
            # A newer job took over while this response was in flight. It gets no further
            # status writes and no repair call: the paper now belongs to the current job, and
            # this older job's own transitions would otherwise land on top of the newer
            # job's `ready`.
            return None, None
        self._transition(paper_id, "running", stage="validating",
                         progress=STAGE_PROGRESS["validating"], message="正在校验模型返回的内容")
        if self._secret_echo(paper_id, result):
            return None, None
        first_error = None
        try:
            answer = self._validate(context, result.payload)
            if secret_guard.contains_secret(answer):
                self.log(secret_guard.log_record(event="secret_echo_blocked", paper_id=paper_id,
                                                 stage="answer",
                                                 public_code=secret_guard.E_SECRET_ECHO_BLOCKED))
                self._transition(paper_id, "failed", stage="", progress=0,
                                 message=secret_guard.message_for(
                                     secret_guard.E_SECRET_ECHO_BLOCKED),
                                 error_code=secret_guard.E_SECRET_ECHO_BLOCKED,
                                 completed_at=_now())
                return None, None
            return answer, {"repaired": False, "attempts": 1}
        except reading_card.ValidationError as exc:
            first_error = exc
        summary = reading_card.summarise_problems(first_error)
        self.log(secret_guard.log_record(event="validation_failed", paper_id=paper_id, attempt=1,
                                         codes=summary["codes"], provider=adapter.name))
        if cancel.is_set():
            self._transition(paper_id, "cancelled", stage="", progress=0, message="已取消",
                             error_code="cancelled", completed_at=_now(),
                             duration_ms=int((self.clock() - started) * 1000))
            return None, None

        # exactly one repair request, carrying the coded problems and the bad JSON.
        # It goes through the same gate: approval, target identity and budget are re-checked,
        # and the repair counts against the same per-job budget as the first call.
        if superseded is not None and superseded():
            # a newer job took over while this response was being checked: stop here rather
            # than spend another call and write another status
            return None, None
        self._transition(paper_id, "running", stage="requesting",
                         progress=STAGE_PROGRESS["requesting"],
                         message="模型返回内容没有通过校验，正在请求修复（仅一次）")
        repair, _reservation = self._dispatch(
            paper_id, context, adapter,
            self._repair_request(context, chunks, result.raw_text, first_error.details),
            kind="repair", cancel=cancel, limits=limits)
        result.repair_text = repair.raw_text
        self.log(secret_guard.log_record(event="repair_call", paper_id=paper_id,
                                         provider=adapter.name,
                                         model=repair.model or result.model,
                                         duration_ms=repair.duration_ms,
                                         category=repair.category, ok=repair.ok,
                                         public_code=repair.public_code,
                                         parse_detail=getattr(repair, "parse_detail", "")))
        if cancel.is_set() or repair.category == providers.CATEGORY_CANCELLED:
            if self._user_cancelled.pop(paper_id, False):
                self._transition(paper_id, "cancelled", stage="", progress=0, message="已取消",
                                 error_code="cancelled", completed_at=_now(),
                                 duration_ms=int((self.clock() - started) * 1000))
            else:
                self._generation_still_current(paper_id, context, started)
            return None, None
        if repair.ok:
            if self._secret_echo(paper_id, repair):
                return None, None
            try:
                answer = self._validate(context, repair.payload)
                if secret_guard.contains_secret(answer):
                    self.log(secret_guard.log_record(event="secret_echo_blocked",
                                                     paper_id=paper_id, stage="repair_answer",
                                                     public_code=secret_guard.E_SECRET_ECHO_BLOCKED))
                    self._transition(paper_id, "failed", stage="", progress=0,
                                     message=secret_guard.message_for(
                                         secret_guard.E_SECRET_ECHO_BLOCKED),
                                     error_code=secret_guard.E_SECRET_ECHO_BLOCKED,
                                     completed_at=_now())
                    return None, None
                return answer, {"repaired": True, "attempts": 2,
                                "first_error": summary["codes"]}
            except reading_card.ValidationError as exc:
                second_error = exc
            second_summary = reading_card.summarise_problems(second_error)
        else:
            # the repair call itself failed: map its category to the fixed vocabulary
            repair_code, repair_message = _public_error(repair)
            second_summary = {"codes": [repair_code], "message": repair_message,
                              "details": [f"repair_call:{repair_code}"]}
        self.log(secret_guard.log_record(event="validation_failed", paper_id=paper_id, attempt=2,
                                         codes=second_summary["codes"], provider=adapter.name))
        self._record_failure(paper_id, context, adapter, result, first_error, second_summary,
                            started)
        return None, None

    def _record_failure(self, paper_id, context, adapter, result, first_error, second_summary,
                        started):
        """Write data/analyses/<id>.failed.json and mark the job failed.

        Phase 1: the raw model output is **not** persisted. The record keeps fixed codes,
        safe counters and status only, so a failure file can never become a plaintext copy
        of the model text (which may quote the prompt or an upstream error).
        """
        record = secret_guard.failure_record(
            schema=reading_card.SCHEMA_VERSION,
            paper_id=paper_id,
            prompt_version=self.prompt_version,
            provider=context["provider"],
            adapter=adapter.name,
            model=result.model,
            source_sha256=context["source_sha256"],
            created_at=_now(),
            error_code="validation_failed",
            errors=[_safe_problems(first_error), _safe_problems(second_summary)],
            duration_ms=int((self.clock() - started) * 1000),
            attempt=self._attempts_so_far(paper_id),
            http_status=result.http_status,
            request_id=result.request_id,
        )
        file_name = None
        try:
            with self.store.lock:
                if not self._snapshot_valid(context.get("job")):
                    return
                file_name = self.store.write_failed_answer(paper_id, record)
        except (OSError, ValueError) as exc:
            self.log(secret_guard.log_record(event="failure_log_error", paper_id=paper_id,
                                             error=type(exc).__name__))
        self._transition(paper_id, "failed", stage="", progress=0,
                         message="模型返回的内容两次都没有通过校验，未展示任何未校验文本",
                         error_code="validation_failed",
                         error_message="；".join(second_summary["codes"])[:500],
                         completed_at=_now(),
                         duration_ms=int((self.clock() - started) * 1000),
                         provider=context["provider"], adapter=adapter.name,
                         model=result.model)
        self.log(secret_guard.log_record(event="failed", paper_id=paper_id,
                                         category="validation_failed",
                                         codes=second_summary["codes"], provider=adapter.name,
                                         model=result.model,
                                         duration_ms=int((self.clock() - started) * 1000)))
        return file_name

    # -- state writes ------------------------------------------------------
    def _set(self, paper, meta, status, **changes):
        """Mutate analysis metadata in place. Caller holds store.lock."""
        if "progress" not in changes and changes.get("stage") in STAGE_PROGRESS:
            changes["progress"] = STAGE_PROGRESS[changes["stage"]]
        meta.update(changes)
        meta["status"] = status
        meta["updated_at"] = _now()
        paper["analysis"] = schema.validate_analysis(meta)

    def _transition(self, paper_id, status, **changes):
        if status in ("queued", "running") and "completed_at" not in changes:
            # A new run must not inherit the previous run's completion time.
            changes["completed_at"] = ""
        with self.store.lock:
            snapshot = getattr(self._execution, "snapshot", None)
            if snapshot is not None and not self._snapshot_valid(snapshot):
                self.ledger.record_outcome(snapshot.job_id, "superseded")
                return False
            paper = self.store.paper(paper_id)
            meta = paper.setdefault("analysis", schema.default_analysis())
            self._set(paper, meta, status, **changes)
            self.store.persist()
        self.log(secret_guard.log_record(event="transition", paper_id=paper_id, status=status,
                                         stage=changes.get("stage", ""),
                                         progress=changes.get("progress")))

    def _next_attempt(self, paper_id):
        meta = self.store.analysis_meta(paper_id)
        return int(meta.get("attempt", 0)) + 1

    def _attempts_so_far(self, paper_id):
        return int(self.store.analysis_meta(paper_id).get("attempt", 0))
