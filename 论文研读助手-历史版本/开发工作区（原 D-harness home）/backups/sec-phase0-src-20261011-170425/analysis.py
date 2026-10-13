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

import json
import threading
import time
from pathlib import Path

import pdftext
import providers
import quotes
import reading_card
import schema
from chunker import Chunker
from extraction import PdfTextExtractor

PROMPT_VERSION = reading_card.PROMPT_VERSION
RUNNING_TIMEOUT_SECONDS = 900
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
MAX_ATTEMPTS = 2
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
                 max_attempts=MAX_ATTEMPTS, log=None, clock=time.time):
        self.store = store
        self.files = store.files
        self.provider_factory = provider_factory or (
            lambda settings: providers.build_adapter(settings, self.files))
        self.extractor_factory = extractor_factory or (
            lambda: PdfTextExtractor(files=self.files))
        self.prompt_version = prompt_version
        self.running_timeout = running_timeout
        self.max_attempts = max_attempts
        self.log = log or (lambda event: None)
        self.clock = clock

        self._queue = []
        self._queue_lock = threading.Lock()
        self._worker = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._current = {}          # paper_id -> {"adapter":…, "cancel": Event}
        self._current_lock = threading.Lock()
        self._forced_ids = {}       # paper_id -> True while an explicit re-run is pending

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
            running = list(self._current)
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
        """(ok, adapter, health) - a disabled provider is reported, not used."""
        settings = settings or self.ai_settings()
        effective = self.provider(settings) if self.provider_enabled(settings) else \
            providers.DisabledAdapter("尚未选择本地模型 Provider（settings.ai.provider = none）")
        return not isinstance(effective, providers.DisabledAdapter), effective, effective.health_check()

    def _effective_provider(self, settings=None):
        settings = settings or self.ai_settings()
        if not self.provider_enabled(settings):
            return providers.DisabledAdapter(
                "尚未选择本地模型 Provider（settings.ai.provider = none）")
        return self.provider(settings)

    # -- startup -----------------------------------------------------------
    def recover(self):
        """Bring the library to a consistent state at start-up.

        * `running` older than the timeout, or left over from a crash  -> queued
        * `queued`                                                     -> queued (re-enqueued)
        * `waiting_for_provider`                                       -> stays, unless a provider is ready
        * `ready` whose source/prompt/model/language changed            -> stale
        """
        settings = self.ai_settings()
        adapter = self._effective_provider(settings)
        disabled = isinstance(adapter, providers.DisabledAdapter)
        self._adapter = adapter
        requeue, stale, waiting = [], [], []
        with self.store.lock:
            for paper in self.store.state["papers"]:
                meta = paper.setdefault("analysis", schema.default_analysis())
                status = meta.get("status", "none")
                current_hash = self.files.has_pdf(paper["id"]) and \
                    pdftext.file_sha256(self.files.pdf_path(paper["id"])) or ""
                if status == "running":
                    aged = _age_seconds(meta.get("updated_at") or meta.get("started_at"))
                    if aged > self.running_timeout:
                        self._set(paper, meta, "queued", stage="queued",
                                  message="上次运行未完成，已重新排队",
                                  error_code="", error_message="")
                        requeue.append(paper["id"])
                    else:
                        self._set(paper, meta, "queued", stage="queued",
                                  message="服务重启，运行中的任务已重新排队")
                        requeue.append(paper["id"])
                elif status == "queued":
                    requeue.append(paper["id"])
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
            self.enqueue(paper_id, reason="recover", force=True)
        self.log({"event": "recover", "requeued": requeue, "stale": stale,
                  "provider": adapter.name, "disabled": disabled})
        return {"requeued": requeue, "stale": stale, "waiting": waiting,
                "provider": adapter.name, "disabled": disabled}

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
            self.log({"event": "resume_waiting", "papers": resumed})
        return resumed

    def enqueue(self, paper_id, reason="request", force=False, context=None):
        """Record `queued` (or waiting_for_provider) and wake the worker. Returns status dict."""
        paper = self.store.paper(paper_id)
        if context is None:
            context = self.build_context(paper_id)
        settings = self.ai_settings()
        adapter = self._effective_provider(settings)
        disabled = isinstance(adapter, providers.DisabledAdapter)
        self._adapter = adapter
        pushed = False
        with self.store.lock:
            meta = paper.setdefault("analysis", schema.default_analysis())
            status = meta.get("status", "none")
            if not force and status == "queued":
                # Already recorded as queued: a caller such as /api/import must still be
                # able to make sure the id really is in the worker queue.
                pushed = True
                already = True
            else:
                already = False
            if not force and status == "running":
                return {"paper_id": paper_id, "status": status, "queued": False,
                        "reason": "already_pending"}
            if not force and status == "waiting_for_user":
                # The AI reference is only fetched on an explicit request: importing a PDF
                # must never turn into a model call by itself.
                return {"paper_id": paper_id, "status": status, "queued": False,
                        "reason": "waiting_for_user"}
            if not force and status == "ready" and not schema.analysis_is_stale(
                    meta, source_sha256=context["source_sha256"],
                    prompt_version=self.prompt_version, model=context["model"],
                    language=context["language"]):
                return {"paper_id": paper_id, "status": "ready", "queued": False,
                        "reason": "already_ready"}
            if disabled and not force:
                self._set(paper, meta, "waiting_for_provider", stage="",
                          message="尚未配置本地模型；配置后会自动开始分析",
                          error_code="", error_message="",
                          source_sha256=context["source_sha256"], language=context["language"])
                self.store.persist()
                return {"paper_id": paper_id, "status": "waiting_for_provider", "queued": False,
                        "reason": "provider_disabled"}
            if not already:
                if force:
                    self._forced_ids[paper_id] = True
                self._set(paper, meta, "queued", stage="queued", progress=0,
                          message="已加入分析队列" if not disabled else "已加入队列（Provider 未就绪）",
                          error_code="", error_message="", requested_at=_now(),
                          source_sha256=context["source_sha256"], provider=context["provider"],
                          adapter=adapter.name,
                          model=context["model"], prompt_version=self.prompt_version,
                          language=context["language"],
                          answer_file="" if force else meta.get("answer_file", ""))
                self.store.persist()
            pushed = True
        self._push(paper_id)
        self.log({"event": "enqueue", "paper_id": paper_id, "reason": reason,
                  "provider": adapter.name, "model": context["model"],
                  "already_recorded": already})
        return {"paper_id": paper_id, "status": "queued", "queued": True, "reason": reason}

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
        self.log({"event": "worker_started"})
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
        self.log({"event": "worker_stopped"})

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

    def cancel(self, paper_id):
        """Ask the running adapter to stop; the job ends as `cancelled`."""
        with self._current_lock:
            current = self._current.get(paper_id)
        if current:
            current["cancel"].set()
            current["adapter"].cancel()
            self.log({"event": "cancel_requested", "paper_id": paper_id})
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
        settings = self.ai_settings()
        adapter = self._effective_provider(settings)
        self._adapter = adapter
        with self._current_lock:
            self._current[paper_id] = {"adapter": adapter, "cancel": cancel}
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
                self.log({"event": "skip_cancelled", "paper_id": paper_id})
                return
            if current_status in ("ready", "stale") and not self._forced_ids.get(paper_id):
                self.log({"event": "skip_terminal", "paper_id": paper_id, "status": current_status})
                return
            context = self.build_context(paper_id)
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
            request = self._request(context, chunks)
            self._transition(paper_id, "running", stage="requesting",
                             progress=STAGE_PROGRESS["requesting"],
                             message=f"正在请求 {adapter.name}（{request.model or '默认模型'}）")
            result = adapter.complete_json(request, on_start=None)
            self.log({"event": "provider_call", "paper_id": paper_id, "provider": adapter.name,
                      "model": result.model or request.model, "duration_ms": result.duration_ms,
                      "category": result.category, "ok": result.ok})
            if cancel.is_set() or result.category == providers.CATEGORY_CANCELLED:
                self._transition(paper_id, "cancelled", stage="", progress=0, message="已取消",
                                 error_code="cancelled", completed_at=_now(),
                                 duration_ms=int((self.clock() - started) * 1000))
                return
            if not result.ok:
                self._fail(paper_id, result, started, adapter.name, request.model)
                return
            answer, validation = self._accept_or_repair(paper_id, context, chunks, adapter,
                                                        result, cancel, started)
            if answer is None:
                return
            verification = answer["verification"]
            input_info = answer.get("input", {})
            self._transition(paper_id, "running", stage="saving", progress=STAGE_PROGRESS["saving"],
                             message="正在保存参考分析")
            self.store.write_analysis_answer(paper_id, answer)
            truncated_note = ""
            if input_info.get("truncated"):
                truncated_note = (f"；原文 {verification['page_count']} 页，"
                                  f"本次只送入第 {input_info.get('first_page')}–"
                                  f"{input_info.get('last_page')} 页")
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
            self.log({"event": "worker_error", "paper_id": paper_id,
                      "error": providers.redact(f"{type(exc).__name__}: {exc}")})
            self._transition(paper_id, "failed", stage="", progress=0,
                             message="分析过程中出现意外错误",
                             error_code=type(exc).__name__, completed_at=_now(),
                             error_message=providers.redact(str(exc))[:500])
        finally:
            self._forced_ids.pop(paper_id, None)
            with self._current_lock:
                self._current.pop(paper_id, None)

    def _fail(self, paper_id, result, started, provider_name, model):
        attempts = self._attempts_so_far(paper_id)
        retryable = result.category in TRANSIENT_CATEGORIES and attempts < self.max_attempts
        duration = int((self.clock() - started) * 1000)
        if retryable:
            self._transition(paper_id, "queued", stage="queued",
                             message=f"{result.category}：{result.message}；稍后自动重试",
                             error_code=result.category, error_message=result.message,
                             duration_ms=duration)
            self._push(paper_id)
            self.log({"event": "retry", "paper_id": paper_id, "category": result.category,
                      "attempt": attempts})
            return
        advice = {
            providers.CATEGORY_UNREACHABLE: "启动本机模型服务后点击重试",
            providers.CATEGORY_TIMEOUT: "模型响应过慢，可换更小的模型或降低段落上限后重试",
            providers.CATEGORY_AUTH: "检查 api_key_env 指向的环境变量是否已设置",
            providers.CATEGORY_CONFIG: "检查模型名称与 base_url",
            providers.CATEGORY_INVALID_JSON: "模型没有按要求输出 JSON，可重试或更换模型",
            providers.CATEGORY_REFUSED: "模型拒绝了这个请求；请自行阅读原文并记录",
            providers.CATEGORY_HTTP: "检查本地模型服务日志",
        }.get(result.category, "可稍后重试")
        self._transition(paper_id, "failed", stage="", progress=0,
                         message=f"{advice}", error_code=result.category,
                         error_message=result.message or result.category,
                         completed_at=_now(), duration_ms=duration, model=model,
                         provider=self.ai_settings().get("provider", "") or "none",
                         adapter=provider_name)
        self.log({"event": "failed", "paper_id": paper_id, "category": result.category,
                  "provider": provider_name, "model": model, "duration_ms": duration})

    # -- context and prompts ----------------------------------------------
    def build_context(self, paper_id):
        paper = self.store.paper(paper_id)
        settings = self.ai_settings()
        digest = ""
        if self.files.has_pdf(paper_id):
            digest = pdftext.file_sha256(self.files.pdf_path(paper_id))
        return {
            "paper_id": paper_id,
            "title": paper.get("title", ""),
            "language": paper.get("language", ""),
            "paper_type": paper.get("type", ""),
            "model": settings.get("model") or "",
            "provider": settings.get("provider") or "none",
            "prompt_version": self.prompt_version,
            "source_sha256": digest,
            "pdf_path": self.files.pdf_path(paper_id) if self.files.has_pdf(paper_id) else None,
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
        return extractor.chunks(result, document_id=paper_id)

    def _request(self, context, chunks):
        request = reading_card.build_request(context, chunks, context.get("page_count", 0),
                                            model=context["model"])
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
            input_report=context.get("input_report"))

    # -- validation, one repair attempt ------------------------------------
    def _accept_or_repair(self, paper_id, context, chunks, adapter, result, cancel, started):
        """Validate, allow exactly one repair request, then fail.

        Returns (answer, validation) or (None, None) after recording a failure.
        """
        self._transition(paper_id, "running", stage="validating",
                         progress=STAGE_PROGRESS["validating"], message="正在校验模型返回的内容")
        first_error = None
        try:
            answer = self._validate(context, result.payload)
            return answer, {"repaired": False, "attempts": 1}
        except reading_card.ValidationError as exc:
            first_error = exc
        summary = reading_card.summarise_problems(first_error)
        self.log({"event": "validation_failed", "paper_id": paper_id, "attempt": 1,
                  "codes": summary["codes"], "details": summary["details"],
                  "provider": adapter.name})
        if cancel.is_set():
            self._transition(paper_id, "cancelled", stage="", progress=0, message="已取消",
                             error_code="cancelled", completed_at=_now(),
                             duration_ms=int((self.clock() - started) * 1000))
            return None, None

        # exactly one repair request, carrying the coded problems and the bad JSON
        self._transition(paper_id, "running", stage="requesting",
                         progress=STAGE_PROGRESS["requesting"],
                         message="模型返回内容没有通过校验，正在请求修复（仅一次）")
        repair = adapter.complete_json(
            self._repair_request(context, chunks, result.raw_text, first_error.details),
            on_start=None)
        result.repair_text = repair.raw_text
        self.log({"event": "repair_call", "paper_id": paper_id, "provider": adapter.name,
                  "model": repair.model or result.model, "duration_ms": repair.duration_ms,
                  "category": repair.category, "ok": repair.ok})
        if cancel.is_set() or repair.category == providers.CATEGORY_CANCELLED:
            self._transition(paper_id, "cancelled", stage="", progress=0, message="已取消",
                             error_code="cancelled", completed_at=_now(),
                             duration_ms=int((self.clock() - started) * 1000))
            return None, None
        if repair.ok:
            try:
                answer = self._validate(context, repair.payload)
                return answer, {"repaired": True, "attempts": 2,
                                "first_error": summary["codes"]}
            except reading_card.ValidationError as exc:
                second_error = exc
            second_summary = reading_card.summarise_problems(second_error)
        else:
            second_summary = {"codes": [repair.category], "message": repair.message,
                              "details": [f"repair_call:{repair.category}"]}
        self.log({"event": "validation_failed", "paper_id": paper_id, "attempt": 2,
                  "codes": second_summary["codes"], "details": second_summary["details"],
                  "provider": adapter.name})
        self._record_failure(paper_id, context, adapter, result, first_error, second_summary,
                            started)
        return None, None

    def _record_failure(self, paper_id, context, adapter, result, first_error, second_summary,
                        started):
        """Write data/analyses/<id>.failed.json and mark the job failed."""
        record = {
            "schema": reading_card.SCHEMA_VERSION,
            "paper_id": paper_id,
            "prompt_version": self.prompt_version,
            "provider": context["provider"],
            "adapter": adapter.name,
            "model": result.model,
            "source_sha256": context["source_sha256"],
            "created_at": _now(),
            "error_code": "validation_failed",
            "errors": [reading_card.summarise_problems(first_error), second_summary],
            "raw_model_output": providers.redact((result.raw_text or "")[:20000]),
            "raw_repair_output": providers.redact((result.repair_text or "")[:20000]),
            "duration_ms": int((self.clock() - started) * 1000),
        }
        file_name = None
        try:
            file_name = self.store.write_failed_answer(paper_id, record)
        except (OSError, ValueError) as exc:
            self.log({"event": "failure_log_error", "paper_id": paper_id,
                      "error": providers.redact(str(exc))})
        self._transition(paper_id, "failed", stage="", progress=0,
                         message="模型返回的内容两次都没有通过校验，未展示任何未校验文本",
                         error_code="validation_failed",
                         error_message="；".join(second_summary["codes"])[:500],
                         completed_at=_now(),
                         duration_ms=int((self.clock() - started) * 1000),
                         provider=context["provider"], adapter=adapter.name,
                         model=result.model)
        self.log({"event": "failed", "paper_id": paper_id, "category": "validation_failed",
                  "codes": second_summary["codes"], "provider": adapter.name,
                  "model": result.model,
                  "duration_ms": int((self.clock() - started) * 1000)})
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
            paper = self.store.paper(paper_id)
            meta = paper.setdefault("analysis", schema.default_analysis())
            self._set(paper, meta, status, **changes)
            self.store.persist()
        self.log({"event": "transition", "paper_id": paper_id, "status": status,
                  "stage": changes.get("stage", ""), "progress": changes.get("progress")})

    def _next_attempt(self, paper_id):
        meta = self.store.analysis_meta(paper_id)
        return int(meta.get("attempt", 0)) + 1

    def _attempts_so_far(self, paper_id):
        return int(self.store.analysis_meta(paper_id).get("attempt", 0))
