"""Provider adapters for local model backends.

The manager only ever sees this interface, so request formats stay decoupled from the
library store and a test can drop in a fake:

    adapter.health_check()            -> Health(ok, category, detail, models)
    adapter.complete_json(request, on_start=None) -> AdapterResult(ok, payload, error_*)
    adapter.cancel()                  -> stops the in-flight call, if any
    adapter.list_models()             -> [str]

Adapters shipped here
---------------------
* DisabledAdapter          - no provider configured; never contacts anything
* OllamaAdapter            - POST {base}/api/chat with format=json
* OpenAICompatibleAdapter  - POST {base}/v1/chat/completions, works with LM Studio,
                             llama.cpp server, vLLM, LocalAI, Jan, text-generation-webui

Secrets: a key is only ever read from the environment variable named by
`api_key_env`, or from the credentials store, and it is never logged, never written
into the library and never returned to the browser. All log lines pass through
`redact()` first.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

DEFAULT_OLLAMA_BASE = "http://127.0.0.1:11434"
DEFAULT_OPENAI_BASE = "http://127.0.0.1:1234/v1"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024

# error categories used by callers, logs and metadata
CATEGORY_OK = "ok"
CATEGORY_UNREACHABLE = "unreachable"
CATEGORY_TIMEOUT = "timeout"
CATEGORY_HTTP = "http_error"
CATEGORY_AUTH = "auth_error"
CATEGORY_BAD_RESPONSE = "invalid_response"
CATEGORY_INVALID_JSON = "invalid_json"
CATEGORY_REFUSED = "refused"
CATEGORY_CANCELLED = "cancelled"
CATEGORY_DISABLED = "provider_disabled"
CATEGORY_CONFIG = "config_error"

SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{6,}"),                       # OpenAI-style keys
    re.compile(r"(?i)\b(?:api[_-]?key|apikey|authorization|bearer|token|password|secret)\b"
               r"\s*[:=]?\s*[\"']?([A-Za-z0-9_\-\.\+/]{8,})"),
    re.compile(r"(?i)\bBearer\s+([A-Za-z0-9_\-\.\+/]{8,})"),
)
SECRET_PLACEHOLDER = "[redacted]"


def redact(text):
    """Remove anything that looks like a credential before it can reach a log.

    Deliberately narrow: only key-shaped tokens and values that follow a credential
    keyword are replaced, so ordinary error text stays readable.
    """
    if not text:
        return ""
    cleaned = str(text)
    for pattern in SECRET_PATTERNS:
        if pattern.groups:
            cleaned = pattern.sub(lambda match: match.group(0).replace(match.group(1),
                                                                       SECRET_PLACEHOLDER), cleaned)
        else:
            cleaned = pattern.sub(SECRET_PLACEHOLDER, cleaned)
    return cleaned


def _first_line(text, limit=120):
    """One short line, for messages that end up in the library and the interface."""
    line = " ".join((text or "").split())
    return line[:limit]


@dataclass
class Health:
    ok: bool
    category: str = CATEGORY_OK
    detail: str = ""
    models: list = field(default_factory=list)
    latency_ms: int = 0

    def as_dict(self):
        return {"ok": self.ok, "category": self.category, "detail": redact(self.detail),
                "models": list(self.models), "latency_ms": self.latency_ms}


@dataclass
class AdapterResult:
    ok: bool
    payload: dict | None = None
    category: str = CATEGORY_OK
    message: str = ""
    http_status: int = 0
    duration_ms: int = 0
    model: str = ""
    refused: bool = False
    raw_text: str = ""            # exact model text, needed for the validation failure log
    repair_text: str = ""         # text of the repair call, when one happened

    def as_dict(self):
        return {"ok": self.ok, "category": self.category, "message": redact(self.message),
                "http_status": self.http_status, "duration_ms": self.duration_ms,
                "model": self.model, "refused": self.refused,
                "has_payload": self.payload is not None}


@dataclass
class CompletionRequest:
    """What a provider needs, independent of the library schema."""
    system: str
    user: str
    model: str = ""
    temperature: float = 0.2
    timeout_seconds: int = 120
    max_response_chars: int = 200000
    json_schema: dict | None = None

    def as_dict(self):
        return {"system": self.system, "user": self.user, "model": self.model,
                "temperature": self.temperature, "timeout_seconds": self.timeout_seconds,
                "json_schema": self.json_schema}


class Cancelled(Exception):
    """Raised inside a call that has been cancelled by the user."""


class ProviderAdapter:
    """Interface every adapter implements. `name` is used in logs and metadata."""

    name = "provider"
    requires_key = False

    def health_check(self, timeout=10):
        raise NotImplementedError

    def list_models(self, timeout=10):
        return []

    def complete_json(self, request, on_start=None):
        raise NotImplementedError

    def cancel(self):
        """Best effort: stop the current call. Safe to call at any time."""
        return False

    def describe(self):
        """Non-secret summary used for logs and metadata."""
        raise NotImplementedError


class DisabledAdapter(ProviderAdapter):
    """Used when no provider is configured: everything fails fast and loudly."""

    name = "disabled"

    def __init__(self, reason="尚未配置本地模型"):
        self.reason = reason

    def health_check(self, timeout=10):
        return Health(False, CATEGORY_DISABLED, self.reason)

    def complete_json(self, request, on_start=None):
        return AdapterResult(False, category=CATEGORY_DISABLED, message=self.reason)

    def describe(self):
        return {"provider": self.name, "model": "", "base_url": ""}


class HttpAdapter(ProviderAdapter):
    """Shared HTTP plumbing: cancellation, timeouts, error categories, redaction."""

    def __init__(self, base_url, model="", api_key="", timeout=120, max_concurrency_note=None):
        self.base_url = (base_url or "").rstrip("/")
        self.model = model or ""
        self.api_key = api_key or ""
        self.timeout = timeout
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._current = None
        self._cancelled_result = None

    # -- cancellation ------------------------------------------------------
    def cancel(self):
        with self._lock:
            self._cancel.set()
        return True

    def _reset_cancel(self):
        with self._lock:
            self._cancel.clear()

    def _is_cancelled(self):
        return self._cancel.is_set()

    # -- http --------------------------------------------------------------
    def _headers(self):
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        return headers

    def _post_json(self, url, payload, timeout):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(url, data=body, method="POST", headers=self._headers())
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    return None, AdapterResult(False, category=CATEGORY_BAD_RESPONSE,
                                               message="响应体超过大小上限")
                return raw, None
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read(400).decode("utf-8", "replace")
            except (OSError, ValueError):
                detail = ""
            category = CATEGORY_AUTH if exc.code in (401, 403) else CATEGORY_HTTP
            # Only the first line of the upstream body, tightly capped: the message is
            # stored in the library and shown in the UI, so a chatty gateway must not be
            # able to push its whole response (or an echoed request) into the record.
            return None, AdapterResult(False, category=category, http_status=exc.code,
                                       message=f"HTTP {exc.code} {_first_line(redact(detail))}")
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
                return None, AdapterResult(False, category=CATEGORY_TIMEOUT,
                                           message=f"连接超时：{redact(str(reason))[:160]}")
            return None, AdapterResult(False, category=CATEGORY_UNREACHABLE,
                                       message=f"无法连接：{redact(str(reason))[:160]}")
        except TimeoutError:
            return None, AdapterResult(False, category=CATEGORY_TIMEOUT, message="请求超时")
        except (OSError, ValueError) as exc:
            return None, AdapterResult(False, category=CATEGORY_UNREACHABLE,
                                       message=f"{type(exc).__name__}: {redact(str(exc))[:160]}")

    def _get_json(self, url, timeout):
        request = urllib.request.Request(url, method="GET", headers=self._headers())
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read(MAX_RESPONSE_BYTES).decode("utf-8", "replace")), None
        except urllib.error.HTTPError as exc:
            category = CATEGORY_AUTH if exc.code in (401, 403) else CATEGORY_HTTP
            return None, AdapterResult(False, category=category, http_status=exc.code,
                                       message=f"HTTP {exc.code}")
        except urllib.error.URLError as exc:
            return None, AdapterResult(False, category=CATEGORY_UNREACHABLE,
                                       message=redact(str(getattr(exc, "reason", exc)))[:160])
        except (TimeoutError, OSError, ValueError) as exc:
            return None, AdapterResult(False, category=CATEGORY_TIMEOUT if isinstance(exc, TimeoutError)
                                       else CATEGORY_BAD_RESPONSE,
                                       message=redact(str(exc))[:160])

    # -- json handling -----------------------------------------------------
    @staticmethod
    def _load_json(text, request):
        if not text or not text.strip():
            return None, AdapterResult(False, category=CATEGORY_INVALID_JSON, message="模型返回了空内容")
        candidate = text.strip()
        fence = re.search(r"```(?:json)?\s*(.*?)```", candidate, re.S)
        if fence:
            candidate = fence.group(1).strip()
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError as exc:
            return None, AdapterResult(False, category=CATEGORY_INVALID_JSON,
                                       message=f"返回内容不是合法 JSON：{exc.msg}（位置 {exc.pos}）")
        if not isinstance(payload, dict):
            return None, AdapterResult(False, category=CATEGORY_INVALID_JSON,
                                       message="返回内容不是 JSON 对象")
        if request.max_response_chars and len(candidate) > request.max_response_chars:
            return None, AdapterResult(False, category=CATEGORY_BAD_RESPONSE, message="返回内容过长")
        return payload, None

    @staticmethod
    def _refusal(payload):
        """Detect a model-level refusal so it is not mistaken for a broken answer."""
        if not isinstance(payload, dict):
            return ""
        for key in ("refusal", "refused", "rejected", "declined"):
            value = payload.get(key)
            if value:
                return redact(str(value))[:200]
        text = json.dumps(payload, ensure_ascii=False).lower()
        for marker in ("i cannot", "i can't", "i'm unable", "cannot assist", "拒绝", "无法协助"):
            if marker in text:
                return "模型拒绝了这个请求"
        return ""

    def _finish(self, payload_result, started, model, raw_text=""):
        payload_result.raw_text = raw_text or payload_result.raw_text
        payload_result.duration_ms = int((time.time() - started) * 1000)
        payload_result.model = model
        if not payload_result.ok:
            self._reset_cancel()
            return payload_result
        refusal = self._refusal(payload_result.payload)
        if refusal:
            payload_result.ok = False
            payload_result.refused = True
            payload_result.category = CATEGORY_REFUSED
            payload_result.message = refusal
        if self._is_cancelled():
            payload_result.ok = False
            payload_result.category = CATEGORY_CANCELLED
            payload_result.message = "任务已被取消"
        self._reset_cancel()
        return payload_result


class OllamaAdapter(HttpAdapter):
    """Ollama's native chat API (`/api/chat`) with `format: json`."""

    name = "ollama"

    def __init__(self, base_url="", model="", api_key="", timeout=120):
        super().__init__(base_url or DEFAULT_OLLAMA_BASE, model=model, api_key=api_key, timeout=timeout)

    def describe(self):
        return {"provider": self.name, "model": self.model, "base_url": self.base_url}

    def list_models(self, timeout=10):
        payload, error = self._get_json(self.base_url + "/api/tags", timeout)
        if error or not isinstance(payload, dict):
            return []
        return sorted(str(item.get("name", "")) for item in payload.get("models", []) if item.get("name"))

    def health_check(self, timeout=10):
        started = time.time()
        payload, error = self._get_json(self.base_url + "/api/tags", timeout)
        latency = int((time.time() - started) * 1000)
        if error:
            return Health(False, error.category, error.message, [], latency)
        models = self.list_models(timeout)
        detail = f"可用模型 {len(models)} 个" + (f"，包含 {self.model}" if self.model in models else "")
        if self.model and models and self.model not in models:
            return Health(False, CATEGORY_CONFIG, f"未安装模型 {self.model}；{detail}", models, latency)
        return Health(True, CATEGORY_OK, detail or "已连接", models, latency)

    def complete_json(self, request, on_start=None):
        self._reset_cancel()
        model = request.model or self.model
        if not model:
            return AdapterResult(False, category=CATEGORY_CONFIG, message="没有指定模型名称")
        payload = {
            "model": model,
            "stream": False,
            "format": "json",
            "options": {"temperature": request.temperature},
            "messages": [{"role": "system", "content": request.system},
                         {"role": "user", "content": request.user}],
        }
        if on_start:
            on_start()
        started = time.time()
        raw, error = self._post_json(self.base_url + "/api/chat",
                                     payload, request.timeout_seconds or self.timeout)
        if error:
            return self._finish(error, started, model)
        try:
            envelope = json.loads(raw.decode("utf-8", "replace"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self._finish(AdapterResult(False, category=CATEGORY_INVALID_JSON,
                                              message="响应信封不是合法 JSON"), started, model)
        if envelope.get("error"):
            return self._finish(AdapterResult(False, category=CATEGORY_HTTP,
                                              message=redact(str(envelope["error"]))[:200]), started, model)
        content = (envelope.get("message") or {}).get("content", "")
        parsed, failure = self._load_json(content, request)
        if failure:
            return self._finish(failure, started, model, content)
        return self._finish(AdapterResult(True, payload=parsed), started, model, content)


class OpenAICompatibleAdapter(HttpAdapter):
    """`/v1/chat/completions` from LM Studio, llama.cpp, vLLM, LocalAI, Jan, TGW."""

    name = "openai_compatible"

    def __init__(self, base_url="", model="", api_key="", timeout=120):
        super().__init__(base_url or DEFAULT_OPENAI_BASE, model=model, api_key=api_key, timeout=timeout)

    def describe(self):
        return {"provider": self.name, "model": self.model, "base_url": self.base_url}

    def list_models(self, timeout=10):
        payload, error = self._get_json(self.base_url + "/models", timeout)
        if error or not isinstance(payload, dict):
            return []
        return sorted(str(item.get("id", "")) for item in payload.get("data", []) if item.get("id"))

    def health_check(self, timeout=10):
        started = time.time()
        payload, error = self._get_json(self.base_url + "/models", timeout)
        latency = int((time.time() - started) * 1000)
        if error:
            return Health(False, error.category, error.message, [], latency)
        models = self.list_models(timeout)
        detail = f"可用模型 {len(models)} 个"
        if self.model and models and self.model not in models:
            return Health(False, CATEGORY_CONFIG, f"未加载模型 {self.model}；{detail}", models, latency)
        return Health(True, CATEGORY_OK, detail or "已连接", models, latency)

    def complete_json(self, request, on_start=None):
        self._reset_cancel()
        model = request.model or self.model
        if not model:
            return AdapterResult(False, category=CATEGORY_CONFIG, message="没有指定模型名称")
        payload = {
            "model": model,
            "temperature": request.temperature,
            "stream": False,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": request.system},
                         {"role": "user", "content": request.user}],
        }
        if on_start:
            on_start()
        started = time.time()
        raw, error = self._post_json(self.base_url + "/chat/completions",
                                     payload, request.timeout_seconds or self.timeout)
        if error:
            return self._finish(error, started, model)
        try:
            envelope = json.loads(raw.decode("utf-8", "replace"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self._finish(AdapterResult(False, category=CATEGORY_INVALID_JSON,
                                              message="响应信封不是合法 JSON"), started, model)
        if envelope.get("error"):
            message = envelope["error"]
            if isinstance(message, dict):
                message = message.get("message", "")
            return self._finish(AdapterResult(False, category=CATEGORY_HTTP,
                                              message=redact(str(message))[:200]), started, model)
        choices = envelope.get("choices") or []
        if not choices:
            return self._finish(AdapterResult(False, category=CATEGORY_BAD_RESPONSE,
                                              message="响应里没有 choices"), started, model)
        message = choices[0].get("message") or {}
        content = message.get("content", "")
        parsed, failure = self._load_json(content, request)
        if failure:
            return self._finish(failure, started, model, content)
        return self._finish(AdapterResult(True, payload=parsed), started, model, content)


class FakeProvider(ProviderAdapter):
    """Scriptable adapter for tests: success, timeout, invalid JSON or refusal.

    Results are queued per call; the last entry repeats once the script runs out.
    """

    name = "fake"

    def __init__(self, script=None, model="fake-model", delay=0.0, payload=None,
                 payloads=None, raw_text=None):
        self.script = list(script or ["success"])
        self.model = model
        self.delay = delay
        self.payload = payload if payload is not None else {"fields": {}, "provider": "fake"}
        self.payload_queue = list(payloads or [])
        # Optional hook: build the answer from the prompt the manager actually sent, so a
        # test can answer for whatever document is in play instead of a fixed payload.
        self.payload_for = None
        self.raw_text = raw_text
        self.raw_texts = []
        self.calls = []
        self.health = Health(True, CATEGORY_OK, "fake provider")
        self._outcome = self.script.pop(0) if self.script else "success"
        self._cancel = threading.Event()
        self._started = threading.Event()
        self._release = threading.Event()
        self._lock = threading.Lock()
        self.enter_delay = False          # when True a call blocks until release()/cancel()

    def describe(self):
        return {"provider": self.name, "model": self.model, "base_url": "memory://fake"}

    def health_check(self, timeout=10):
        return self.health

    def list_models(self, timeout=10):
        return [self.model]

    def cancel(self):
        self._cancel.set()
        self._release.set()
        return True

    def next_payload(self):
        """Pop the next scripted payload; the last one repeats."""
        if len(self.payload_queue) > 1:
            return self.payload_queue.pop(0)
        return self.payload_queue[0] if self.payload_queue else self.payload

    def release(self):
        """Let a blocked fake call continue (used with enter_delay)."""
        self._release.set()

    def wait_started(self, timeout=5):
        return self._started.wait(timeout)

    def complete_json(self, request, on_start=None):
        outcome = self._outcome
        self.calls.append({"outcome": outcome, "model": request.model or self.model,
                           "request_chars": len(request.user), "started": time.time()})
        if on_start:
            on_start()
        self._started.set()
        if self.enter_delay:
            self._release.wait(request.timeout_seconds or 30)
        if self._cancel.is_set() or self._release.is_set() and self.enter_delay:
            self._cancel.clear()
            if self.enter_delay:
                self._release.clear()
        if self.delay:
            time.sleep(self.delay)
        with self._lock:
            self._outcome = self.script.pop(0) if self.script else outcome
        started = time.time()
        if outcome == "timeout":
            return AdapterResult(False, category=CATEGORY_TIMEOUT, message="FakeProvider 超时",
                                 duration_ms=int((time.time() - started) * 1000), model=self.model)
        if outcome == "invalid_json":
            return AdapterResult(False, category=CATEGORY_INVALID_JSON,
                                 message="FakeProvider 返回了非法 JSON",
                                 duration_ms=1, model=self.model,
                                 raw_text=self.raw_text or "{not json")
        if outcome == "refused":
            return AdapterResult(False, category=CATEGORY_REFUSED, message="模型拒绝了这个请求",
                                 refused=True, duration_ms=1, model=self.model)
        if outcome == "error":
            return AdapterResult(False, category=CATEGORY_UNREACHABLE, message="FakeProvider 无法连接",
                                 duration_ms=1, model=self.model)
        payload = self.next_payload() if self.payload_queue else self.payload
        if self.payload_for is not None:
            # Tests may derive the answer from the prompt that was actually sent (for
            # example to quote the page text the manager extracted for this document).
            payload = self.payload_for(request, payload)
        return AdapterResult(True, payload=json.loads(json.dumps(payload)),
                             duration_ms=1, model=self.model,
                             raw_text=self.raw_text or json.dumps(payload, ensure_ascii=False))


def resolve_api_key(api_key_env, files=None):
    """Read a key from the named environment variable, or from the credentials file.

    Returns (key, source). The key itself is never logged; `source` is a label only.
    """
    if not api_key_env:
        return "", ""
    value = os.environ.get(api_key_env, "").strip()
    if value:
        return value, f"env:{api_key_env}"
    if files is not None:
        payload, error = files.read_credentials()
        if error is None and isinstance(payload, dict):
            value = str(payload.get(api_key_env, "")).strip()
            if value:
                return value, f"file:{files.credentials.name}"
    return "", ""


def build_adapter(ai_settings, files=None, timeout=None):
    """Create the adapter named by settings.ai, or DisabledAdapter.

    Only the environment variable *name* is stored in the library; the key is read at
    construction time and kept in memory for the lifetime of the adapter.
    """
    provider = (ai_settings or {}).get("provider", "none")
    model = (ai_settings or {}).get("model", "") or ""
    base_url = (ai_settings or {}).get("base_url", "") or ""
    env_name = (ai_settings or {}).get("api_key_env", "") or ""
    key, _source = resolve_api_key(env_name, files)
    if provider == "ollama":
        return OllamaAdapter(base_url=base_url, model=model, timeout=timeout or 120)
    if provider in ("lmstudio", "openai_compatible"):
        return OpenAICompatibleAdapter(base_url=base_url or DEFAULT_OPENAI_BASE, model=model,
                                       api_key=key, timeout=timeout or 120)
    return DisabledAdapter("尚未选择本地模型 Provider（settings.ai.provider = none）")
