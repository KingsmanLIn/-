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

Secrets: a key is read only from a *registered* environment variable (see credentials.py);
the plaintext credentials.json fallback was removed in the security release. The value is
wrapped in a `credentials.Secret` that refuses to print itself, is revealed only when the
Authorization header is built, and is registered with `secret_guard` so an echo can be
detected. All outbound text passes through `secret_guard.redact` first.

Phase 1 also removes upstream error bodies from messages: only a fixed code, an HTTP status
and an optional provider request id may reach the log, the library or the browser.
"""
from __future__ import annotations

import json
import os
import re
import threading
import reading_card
from contextlib import nullcontext
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

import credentials
import endpoint_policy
import secret_guard
import secure_transport

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
CATEGORY_SECRET_ECHO = "secret_echo_blocked"

SECRET_PLACEHOLDER = secret_guard.PLACEHOLDER
_REQUEST_ID_HEADERS = ("x-request-id", "request-id", "x-amzn-requestid", "cf-ray",
                       "x-ms-request-id", "x-goog-request-id")


def redact(text):
    """Kept for compatibility; the real implementation lives in secret_guard."""
    return secret_guard.redact(text)


def _request_id(headers):
    """A provider request id is safe to keep: it identifies the call, not the payload."""
    return secure_transport.request_id_of(headers or {})


def _retry_after_seconds(headers):
    """Seconds a provider asked us to wait, from a `Retry-After` header.

    Only the numeric form is honoured and it is capped by the caller: a provider cannot make
    this software sleep indefinitely, and a date-form value is ignored rather than parsed.
    """
    if not headers:
        return 0.0
    for name, value in headers.items():
        if str(name).lower() != "retry-after":
            continue
        try:
            seconds = float(str(value).strip())
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, min(seconds, 3600.0))
    return 0.0



def _tool_call_denial(payload):
    """Return a short reason when a response asks for an executable action, else "".

    Covers the shapes an OpenAI-compatible provider uses: a `tool_calls` array on the
    message, a legacy `function_call`, or the whole body being one.
    """
    if not isinstance(payload, dict):
        return ""
    choices = payload.get("choices")
    candidates = []
    if isinstance(choices, list):
        for choice in choices:
            if isinstance(choice, dict):
                candidates.append(choice)
                message = choice.get("message")
                if isinstance(message, dict):
                    candidates.append(message)
    candidates.append(payload)
    message = payload.get("message")
    if isinstance(message, dict):
        candidates.append(message)
    for node in candidates:
        if not isinstance(node, dict):
            continue
        for key in ("tool_calls", "function_call"):
            if node.get(key):
                return key
        if node.get("finish_reason") == "tool_calls":
            return "finish_reason"
    return ""


def _user_content(request):
    """The user turn: plain text, or text plus image parts for a multimodal model.

    OpenAI-compatible endpoints take a list of parts for a multimodal turn. The images are page
    renders of the paper's figure pages, produced locally by `figures.py`; the text block that
    explains them is added by `reading_card` when the request was built.
    """
    images = tuple(getattr(request, "images", ()) or ())
    if not images:
        return request.user
    parts = [{"type": "text", "text": request.user}]
    for item in images:
        mime = str(item.get("mime") or "image/png")
        data = str(item.get("data") or "")
        if not data:
            continue
        parts.append({"type": "image_url",
                      "image_url": {"url": f"data:{mime};base64,{data}"}})
    return parts if len(parts) > 1 else request.user


def _parse_diagnosis(exc):
    """A log-safe fingerprint of a JSON parse failure: a fixed word plus an optional offset.

    `reading_card.parse_model_json` reports only its own vocabulary plus, when parsing really
    stopped somewhere, a character offset. Anything that does not look like that shape is
    replaced rather than trusted: an unexpected exception must not smuggle upstream text into
    the log.
    """
    code = ""
    offset = ""
    for problem in getattr(exc, "details", None) or ():
        if not isinstance(problem, dict):
            continue
        candidate = str(problem.get("code", "") or "")
        if not code and re.fullmatch(r"[a-z_]{1,32}", candidate):
            code = candidate
        detail = str(problem.get("detail", "") or "")
        if detail.startswith("pos=") and detail[4:].isdigit():
            offset = detail[4:]
    if not code:
        candidate = str(getattr(exc, "code", "") or "")
        code = candidate if re.fullmatch(r"[a-z_]{1,32}", candidate) else "bad_json"
    return f"{code}@{offset}" if offset else code


def _result_from_transport(response):
    """Map a transport outcome onto an adapter result with a fixed public code."""
    if response.code == secret_guard.E_AUTH_FAILED:
        category = CATEGORY_AUTH
    elif response.code == secret_guard.E_REDIRECT_BLOCKED:
        category = CATEGORY_CONFIG
    elif response.code in (secret_guard.E_ENDPOINT_BLOCKED, secret_guard.E_ENDPOINT_INVALID,
                           secret_guard.E_PROXY_BLOCKED):
        category = CATEGORY_CONFIG
    elif response.code == secret_guard.E_TLS_FAILED:
        category = CATEGORY_UNREACHABLE
    elif response.code == secret_guard.E_TIMEOUT:
        category = CATEGORY_TIMEOUT
    elif response.code == secret_guard.E_UNREACHABLE:
        category = CATEGORY_UNREACHABLE
    elif response.code == secret_guard.E_INVALID_RESPONSE:
        category = CATEGORY_BAD_RESPONSE
    else:
        category = CATEGORY_HTTP
    return AdapterResult(False, category=category, http_status=response.status,
                         rejected_parameters=tuple(response.rejected_parameters or ()),
                         public_code=response.code, request_id=response.request_id,
                         retry_after_seconds=_retry_after_seconds(response.headers),
                         message=f"HTTP {response.status}" if response.status else response.code)


class SendDenied(secure_transport.OutboundDenied):
    def __init__(self, code):
        super().__init__(code)


def safe_models(items, field):
    if not isinstance(items, list) or len(items) > 1000:
        return []
    models = []
    for item in items:
        value = item.get(field) if isinstance(item, dict) else None
        if not isinstance(value, str):
            continue
        if secret_guard.contains_secret(value):
            raise SendDenied(secret_guard.E_SECRET_ECHO_BLOCKED)
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/+@-]{0,199}", value):
            models.append(value)
    return sorted(set(models))


@dataclass
class Health:
    ok: bool
    category: str = CATEGORY_OK
    detail: str = ""
    models: list = field(default_factory=list)
    latency_ms: int = 0

    def as_dict(self):
        if secret_guard.contains_secret({"detail": self.detail, "models": self.models}):
            return {"ok": False, "category": CATEGORY_SECRET_ECHO,
                    "code": secret_guard.E_SECRET_ECHO_BLOCKED,
                    "detail": secret_guard.message_for(secret_guard.E_SECRET_ECHO_BLOCKED),
                    "models": [], "latency_ms": self.latency_ms}
        return {"ok": self.ok, "category": self.category, "detail": redact(self.detail),
                "models": safe_models([{"id": v} for v in self.models], "id"),
                "latency_ms": self.latency_ms}



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
    raw_text: str = ""            # in memory only: never persisted, never returned
    repair_text: str = ""         # in memory only
    public_code: str = ""         # fixed vocabulary member, safe to store and show
    request_id: str = ""          # provider-side request id; contains no payload
    outcome_unknown: bool = False      # sent, but the answer was never seen
    retry_after_seconds: float = 0.0   # from a 429 response, when the provider sends one
    rejected_parameters: tuple = ()    # optional parameters a 4xx reply named, fixed set only
    #: Log-safe fingerprint of why a reply could not be used: a fixed vocabulary word plus an
    #: optional numeric character offset ("bad_json@12345", "empty_content@length"). Built only
    #: by `_parse_diagnosis` / `_reply_diagnosis`, never shown as user text, and deliberately
    #: left out of `as_dict` so it cannot reach the page or the library.
    parse_detail: str = ""

    def as_dict(self):
        return {"ok": self.ok, "category": self.category,
                 "public_code": self.public_code or self.category,
                 "message": secret_guard.redact(self.message),
                 "http_status": self.http_status, "duration_ms": self.duration_ms,
                 "model": self.model, "refused": self.refused,
                 "request_id": self.request_id,
                 "has_payload": self.payload is not None}

    def public_message(self):
        """The sentence a user may see: fixed vocabulary only, never the upstream text."""
        if self.public_code:
            return secret_guard.message_for(self.public_code)
        return secret_guard.redact(self.message) or secret_guard.message_for(
            secret_guard.E_UPSTREAM_ERROR)


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
    #: Page images for a multimodal model: ({"page": int, "mime": str, "data": base64}, ...).
    #: They belong to the *prompt*: a text-only endpoint simply rejects a request that carries
    #: them, which is why the caller only fills this when the reader switched vision on.
    images: tuple = ()

    def as_dict(self):
        return {"system": self.system, "user": self.user, "model": self.model,
                "temperature": self.temperature, "timeout_seconds": self.timeout_seconds,
                "json_schema": self.json_schema,
                "image_pages": [item.get("page") for item in self.images]}




class Cancelled(Exception):
    """Raised inside a call that has been cancelled by the user."""


class ProviderAdapter:
    """Interface every adapter implements. `name` is used in logs and metadata."""

    name = "provider"
    requires_key = False
    #: True when the adapter can carry page images (a multimodal turn). The OpenAI-compatible
    #: protocol has content parts for that; Ollama's own API does not, and a text-only endpoint
    #: rejects a request that carries images, so the caller checks this before rendering any.
    supports_images = False

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


class CapabilityError(Exception):
    """A parameter the server refuses and this program will not silently drop."""

    def __init__(self, parameter):
        super().__init__(parameter)
        self.parameter = parameter


class HttpAdapter(ProviderAdapter):
    """Shared HTTP plumbing: cancellation, timeouts, error categories, redaction.

    Phase 1: the credential is a `credentials.Secret` (never a bare string), upstream error
    bodies are not read into messages, and messages come from the fixed vocabulary in
    `secret_guard`.
    Phase 2: every request goes through `SecureTransport` on a target approved by
    `EndpointPolicy`; the adapter passes *relative* paths only, so a response, a model name
    or a redirect can never introduce a new destination.
    """

    def __init__(self, base_url, model="", api_key=None, timeout=120, max_concurrency_note=None,
                 mode=endpoint_policy.DEFAULT_MODE, ssl_context=None, connect_timeout=None,
                 transport_factory=None):
        self.base_url = (base_url or "").rstrip("/")
        self.model = model or ""
        self.mode = mode
        self.ssl_context = ssl_context
        self.connect_timeout = connect_timeout or 10.0
        # Testing seam: production always uses SecureTransport. A test may pass its own
        # factory to observe the exact request, but the policy still runs first, so a double
        # cannot be used to bypass address checks.
        self.transport_factory = transport_factory or secure_transport.SecureTransport
        # Accept a Secret, a plain string from a caller that already validated it, or None.
        if api_key is None or api_key == "":
            self.api_key = None
        elif isinstance(api_key, credentials.Secret):
            self.api_key = api_key
        else:
            self.api_key = credentials.Secret(str(api_key))
        if self.api_key:
            secret_guard.register(self.api_key, "adapter")
        # Set by build_adapter when the configured reference could not be resolved. The
        # adapter then fails closed before any connection instead of reading a 401 as the
        # explanation.
        self.credential_code = ""
        self.credential_missing = False
        # Flipped immediately before the socket is opened. The caller can then tell
        # "never sent" (refund the budget) from "sent, answer unknown" (charge it, do not
        # retry automatically).
        self.request_sent = False
        # Output cap is a real request parameter, not only a limit on what is accepted back:
        # it bounds what one call can cost. Set from settings.ai.max_output_tokens.
        # Frame the fallback the same way schema.AI_SETTINGS_DEFAULTS does: a reasoning model
        # spends output tokens on reasoning first, so a small ceiling can leave no answer.
        self.max_output_tokens = 16384
        #: Called before an adaptation re-sends the prompt. Returns True when the caller has
        #: reserved the extra transmission. None means "no accounting available", in which
        #: case a re-send is refused rather than performed for free.
        self.on_retransmit = None
        self.send_guard = None
        self._policy = None
        self._transport_obj = None
        self.last_transport = None
        self.timeout = timeout
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._current = None
        self._cancelled_result = None

    # -- cancellation ------------------------------------------------------
    def cancel(self):
        with self._lock:
            self._cancel.set()
        transport = self._transport_obj
        if transport is not None and hasattr(transport, 'abort'):
            transport.abort()
        return True

    def _reset_cancel(self):
        with self._lock:
            self._cancel.clear()

    def _is_cancelled(self):
        return self._cancel.is_set()

    # -- credential gate ---------------------------------------------------
    def _credential_block(self):
        """Return a refusal when the bound credential could not be resolved."""
        if not self.credential_missing:
            return None
        code = self.credential_code or credentials.CODE_MISSING
        public = {credentials.CODE_MISSING: secret_guard.E_CREDENTIAL_MISSING,
                  credentials.CODE_LEGACY_FILE: secret_guard.E_CREDENTIAL_MISSING,
                  credentials.CODE_UNREGISTERED: secret_guard.E_CREDENTIAL_NOT_BOUND,
                  credentials.CODE_INVALID: secret_guard.E_CREDENTIAL_INVALID,
                  credentials.CODE_NOT_BOUND: secret_guard.E_CREDENTIAL_NOT_BOUND,
                  }.get(code, secret_guard.E_CREDENTIAL_MISSING)
        return AdapterResult(False, category=CATEGORY_CONFIG, public_code=public,
                             message=f"credential:{code}")

    # -- http --------------------------------------------------------------
    def _headers(self):
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            # reveal() happens here and nowhere else: the value exists only in this header.
            headers["Authorization"] = "Bearer " + self.api_key.reveal()
        return headers

    def _transport(self):
        """Build (or rebuild) the transport for the approved target.

        Authorization runs first: the policy resolves the host and refuses forbidden
        addresses before any socket exists. Only then is the transport created, and the
        credential is only ever read inside `_headers`, which runs after this.
        """
        policy = endpoint_policy.EndpointPolicy.for_settings(
            self.base_url, getattr(self, "mode", endpoint_policy.DEFAULT_MODE))
        policy.authorize(resolver=lambda host, port: endpoint_policy.bounded_resolve(
            host, port, timeout=min(self.connect_timeout, self.timeout)))
        self._policy = policy
        self._transport_obj = self.transport_factory(
            policy, ssl_context=getattr(self, "ssl_context", None),
            connect_timeout=self.connect_timeout, read_timeout=self.timeout)
        self._transport_obj.send_guard = self.send_guard
        self._transport_obj.cancel_event = self._cancel
        return self._transport_obj

    def _send(self, method, path, payload=None, timeout=None):
        if method == "POST" and self._is_cancelled():
            return secure_transport.TransportResponse(False, code=secret_guard.E_CANCELLED)
        if self.credential_missing:
            blocked = self._credential_block()
            return secure_transport.TransportResponse(False, code=blocked.public_code)
        request_started = time.monotonic()
        previous_timeout = self.timeout
        if timeout is not None:
            self.timeout = min(self.timeout, timeout)
        try:
            transport = self._transport()
        except (SendDenied, endpoint_policy.PolicyError) as exc:
            return secure_transport.TransportResponse(False, code=exc.code)
        finally:
            self.timeout = previous_timeout
        request_limit = min(previous_timeout, timeout) if timeout is not None else previous_timeout
        remaining = request_limit - (time.monotonic() - request_started)
        if remaining <= 0:
            return secure_transport.TransportResponse(False, code=secret_guard.E_TIMEOUT)
        transport.read_timeout = remaining
        headers = self._headers() if self.api_key else {}
        if method == "POST":
            response = transport.post_json(path, payload, headers=headers)
        else:
            response = transport.get_json(path, headers=headers)
        written = bool(getattr(transport, 'request_written', True))
        self.request_sent = self.request_sent or written
        if written:
            self.transmissions = getattr(self, 'transmissions', 0) + 1
        self.last_transport = response
        return response

    def _post_json(self, path, payload, timeout):
        """POST a *relative* path on the approved target. A URL is never accepted here."""
        response = self._send("POST", path, payload, timeout=timeout)
        if response.ok:
            if len(response.body) > MAX_RESPONSE_BYTES:
                return None, AdapterResult(False, category=CATEGORY_BAD_RESPONSE,
                                           public_code=secret_guard.E_INVALID_RESPONSE,
                                           message="响应体超过大小上限")
            return response.body, None
        return None, _result_from_transport(response)

    def _get_json_path(self, path, timeout=10):
        response = self._send("GET", path, timeout=timeout)
        if response.ok:
            try:
                payload = reading_card.parse_model_json(response.body.decode("utf-8"))
                if secret_guard.contains_secret(payload):
                    return None, AdapterResult(False, category=CATEGORY_SECRET_ECHO,
                        public_code=secret_guard.E_SECRET_ECHO_BLOCKED)
                return payload, None
            except (UnicodeDecodeError, reading_card.ValidationError, TypeError):
                return None, AdapterResult(False, category=CATEGORY_BAD_RESPONSE,
                                           public_code=secret_guard.E_INVALID_RESPONSE,
                                           message="响应不是合法 JSON")
        return None, _result_from_transport(response)

    # -- json handling -----------------------------------------------------
    @staticmethod
    def _reply_diagnosis(choice, message, content):
        """Name why a reply carries no usable JSON, or return None when content exists.

        A reasoning ("thinking") model can spend the entire output budget on its reasoning
        channel and hand back an empty `content` with `finish_reason == "length"`. Reported
        as a plain JSON failure that reads as "the model did not answer with JSON", which is
        both untrue and unactionable: the fix is a higher output ceiling, not a retry. The
        two codes below say which of the two happened, and `parse_detail` records it in the
        log without any upstream text.
        """
        if isinstance(content, str) and content.strip():
            return None
        if not isinstance(message, dict):
            return None
        reasoning = message.get("reasoning_content") or message.get("reasoning") or ""
        finish = str((choice or {}).get("finish_reason") or "")
        if finish == "length":
            return AdapterResult(False, category=CATEGORY_INVALID_JSON,
                                 public_code=secret_guard.E_OUTPUT_TRUNCATED,
                                 message=secret_guard.message_for(
                                     secret_guard.E_OUTPUT_TRUNCATED),
                                 parse_detail="empty_content")
        if isinstance(reasoning, str) and reasoning.strip():
            return AdapterResult(False, category=CATEGORY_INVALID_JSON,
                                 public_code=secret_guard.E_REASONING_ONLY,
                                 message=secret_guard.message_for(secret_guard.E_REASONING_ONLY),
                                 parse_detail="reasoning_only")
        return None

    @staticmethod
    def _load_json(text, request):
        if not isinstance(text, str) or not text.strip():
            return None, AdapterResult(False, category=CATEGORY_INVALID_JSON,
                                      public_code=secret_guard.E_INVALID_RESPONSE,
                                      message=secret_guard.message_for(secret_guard.E_INVALID_RESPONSE),
                                      parse_detail="empty_content")
        if request.max_response_chars and len(text) > request.max_response_chars:
            return None, AdapterResult(False, category=CATEGORY_BAD_RESPONSE,
                                      public_code=secret_guard.E_INVALID_RESPONSE,
                                      message=secret_guard.message_for(secret_guard.E_INVALID_RESPONSE),
                                      parse_detail="response_too_long")
        try:
            return reading_card.parse_model_json(text), None
        except (reading_card.ValidationError, TypeError) as exc:
            return None, AdapterResult(False, category=CATEGORY_INVALID_JSON,
                                      public_code=secret_guard.E_INVALID_RESPONSE,
                                      message=secret_guard.message_for(secret_guard.E_INVALID_RESPONSE),
                                      parse_detail=_parse_diagnosis(exc))

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


    # -- capability adaptation ---------------------------------------------------------
    # Two groups, and the split is the safety property:
    #   ESSENTIAL  - never removed. A server that refuses one of these gets no request at all,
    #                because dropping the output cap would mean an unbounded, unbudgeted call.
    #   OPTIONAL   - may be dropped, one at a time, when a server says it does not know it.
    ESSENTIAL_PARAMETERS = ("model", "messages", "max_tokens", "num_predict", "stream")
    OPTIONAL_PARAMETERS = ("response_format", "format", "temperature")

    def parameter_capabilities(self):
        """Optional parameters this adapter believes the configured provider accepts."""
        return {name: True for name in self.OPTIONAL_PARAMETERS}

    def _post_with_adaptation(self, path, payload, timeout, started, model, dropped=()):
        """Send the payload, dropping rejected *optional* parameters one at a time by name.

        A parameter is only dropped when the reply names it, and only when the adapter
        classifies it as optional. An essential parameter - the token cap above all - is never
        removed: the job stops with a configuration error instead of being sent unbounded.

        A dropped parameter means the prompt is transmitted *again*. That second transmission
        is a second call to the provider and has to be paid for, so the caller is told about it
        through `on_retransmit` and every extra send takes its own reservation.
        """
        if self._is_cancelled():
            return None, AdapterResult(False, category=CATEGORY_CANCELLED,
                public_code=secret_guard.E_CANCELLED), tuple(dropped)
        if dropped:
            # A re-send happens only when the caller can account for the second transmission.
            # No hook, or a hook that reports the budget is used up, means no re-send: an
            # unaccounted transmission is exactly the hole this closes.
            allowed = self.on_retransmit is not None and self.on_retransmit(dropped[-1])
            if not allowed:
                return None, AdapterResult(
                    False, category=CATEGORY_CONFIG,
                    public_code=secret_guard.E_CAPABILITY_UNSUPPORTED,
                    message="服务商拒绝了可选参数，但本次调用已无剩余额度，未重发",
                    http_status=0), tuple(dropped)
        raw, error = self._post_json(path, payload, timeout)
        if error is None:
            return raw, None, tuple(dropped)
        named = self._named_parameter(error, payload)
        if not named or named in dropped:
            return None, error, tuple(dropped)
        if named in self.ESSENTIAL_PARAMETERS:
            return None, AdapterResult(
                False, category=CATEGORY_CONFIG,
                public_code=secret_guard.E_CAPABILITY_UNSUPPORTED,
                message=f"服务商不支持必需的参数 {named}，为避免无上限请求已停止",
                http_status=error.http_status), tuple(dropped)
        if named not in self.OPTIONAL_PARAMETERS:
            return None, error, tuple(dropped)
        reduced = {key: value for key, value in payload.items() if key != named}
        self.log_dropped_parameter(named)
        return self._post_with_adaptation(path, reduced, timeout, started, model,
                                          dropped + (named,))

    def _named_parameter(self, error, payload):
        """The first rejected parameter that is still in this payload, or "".

        A server often lists several unknown parameters in one reply, so the caller drops
        them one at a time: each round removes the first name the reply mentions that the
        current payload still carries. A name that is already gone is skipped, otherwise the
        loop would stall on a parameter that is no longer being sent.
        """
        if not error or getattr(error, "http_status", 0) not in (400, 404, 415, 422):
            return ""
        last = getattr(self, "last_transport", None)
        named = tuple(getattr(last, "rejected_parameters", ()) or ())
        if not named:
            named = tuple(getattr(error, "rejected_parameters", ()) or ())
        for name in named:
            if name in payload:
                return name
        return ""

    def log_dropped_parameter(self, name):
        """Record the adaptation without any payload text."""
        event = getattr(self, "log", None)
        if callable(event):
            event({"event": "parameter_dropped", "provider": self.name,
                   "parameter": name})

class OllamaAdapter(HttpAdapter):
    """Ollama's native chat API (`/api/chat`) with `format: json`."""

    name = "ollama"

    def __init__(self, base_url="", model="", api_key="", timeout=120, mode=None,
                 ssl_context=None, transport_factory=None):
        super().__init__(base_url or DEFAULT_OLLAMA_BASE, model=model, api_key=api_key,
                         timeout=timeout, mode=mode or endpoint_policy.DEFAULT_MODE,
                         ssl_context=ssl_context, transport_factory=transport_factory)

    def describe(self):
        return {"provider": self.name, "model": self.model, "base_url": self.base_url}

    def list_models(self, timeout=10):
        payload, error = self._get_json_path("/api/tags", timeout=timeout)
        if error or not isinstance(payload, dict):
            return []
        return safe_models(payload.get("models", []), "name")

    def health_check(self, timeout=10):
        started = time.time()
        payload, error = self._get_json_path("/api/tags", timeout=timeout)
        latency = int((time.time() - started) * 1000)
        if error:
            return Health(False, error.category, error.message, [], latency)
        try:
            models = safe_models(payload.get('data', []), 'id') if self.name == 'openai_compatible' \
                else safe_models(payload.get('models', []), 'name')
        except SendDenied as exc:
            return Health(False, CATEGORY_SECRET_ECHO, secret_guard.message_for(exc.code), [], latency)
        detail = f"可用模型 {len(models)} 个" + (f"，包含 {self.model}" if self.model in models else "")
        if self.model and models and self.model not in models:
            return Health(False, CATEGORY_CONFIG, f"未安装模型 {self.model}；{detail}", models, latency)
        return Health(True, CATEGORY_OK, detail or "已连接", models, latency)

    def complete_json(self, request, on_start=None):
        self.request_sent = False
        self.transmissions = 0
        blocked = self._credential_block()
        if blocked is not None:
            return blocked
        model = request.model or self.model
        if not model:
            return AdapterResult(False, category=CATEGORY_CONFIG,
                                 public_code=secret_guard.E_CREDENTIAL_NOT_BOUND,
                                 message="没有指定模型名称")
        payload = {
            "model": model,
            "stream": False,
            "format": "json",
            "options": {"temperature": request.temperature,
                        "num_predict": self.max_output_tokens},
            "messages": [{"role": "system", "content": request.system},
                         {"role": "user", "content": request.user}],
        }
        if on_start:
            on_start()
        started = time.time()
        if secret_guard.contains_secret(payload):
            return self._finish(AdapterResult(False, category=CATEGORY_SECRET_ECHO,
                                              public_code=secret_guard.E_SECRET_ECHO_BLOCKED,
                                              message="请求内容包含凭据特征，已阻止发送"),
                                started, model)
        raw, error, _dropped = self._post_with_adaptation(
            "/api/chat", payload, request.timeout_seconds or self.timeout, started, model)
        if error:
            return self._finish(error, started, model)
        try:
            envelope = reading_card.parse_model_json(raw.decode("utf-8"))
        except (UnicodeDecodeError, reading_card.ValidationError, TypeError):
            return self._finish(AdapterResult(False, category=CATEGORY_INVALID_JSON,
                                              public_code=secret_guard.E_INVALID_RESPONSE,
                                              message="响应信封不是合法 JSON"), started, model)
        if envelope.get("error"):
            return self._finish(AdapterResult(False, category=CATEGORY_HTTP,
                                              public_code=secret_guard.E_UPSTREAM_ERROR,
                                              message="服务商返回 error 字段"), started, model)
        denial = _tool_call_denial(envelope)
        if denial:
            # The model answered with an executable action instead of data. Nothing in this
            # program has the authority to run it, so the reply is refused outright.
            return self._finish(AdapterResult(False, category=CATEGORY_INVALID_JSON,
                                              public_code=secret_guard.E_TOOL_CALL_BLOCKED,
                                              message="模型返回了工具调用，已拒绝"), started, model)
        envelope_message = envelope.get("message") or {}
        content = envelope_message.get("content", "")
        diagnosis = self._reply_diagnosis(envelope, envelope_message, content)
        if diagnosis is not None:
            return self._finish(diagnosis, started, model, content)
        parsed, failure = self._load_json(content, request)
        if failure:
            return self._finish(failure, started, model, content)
        return self._finish(AdapterResult(True, payload=parsed), started, model, content)


class OpenAICompatibleAdapter(HttpAdapter):
    """`/v1/chat/completions` from LM Studio, llama.cpp, vLLM, LocalAI, Jan, TGW."""

    name = "openai_compatible"
    supports_images = True

    def __init__(self, base_url="", model="", api_key="", timeout=120, mode=None,
                 ssl_context=None, transport_factory=None):
        super().__init__(base_url or DEFAULT_OPENAI_BASE, model=model, api_key=api_key,
                         timeout=timeout, mode=mode or endpoint_policy.DEFAULT_MODE,
                         ssl_context=ssl_context, transport_factory=transport_factory)

    def describe(self):
        return {"provider": self.name, "model": self.model, "base_url": self.base_url}

    def list_models(self, timeout=10):
        payload, error = self._get_json_path("/models", timeout=timeout)
        if error or not isinstance(payload, dict):
            return []
        return safe_models(payload.get("data", []), "id")

    def health_check(self, timeout=10):
        started = time.time()
        payload, error = self._get_json_path("/models", timeout=timeout)
        latency = int((time.time() - started) * 1000)
        if error:
            return Health(False, error.category, error.message, [], latency)
        try:
            models = safe_models(payload.get('data', []), 'id') if self.name == 'openai_compatible' \
                else safe_models(payload.get('models', []), 'name')
        except SendDenied as exc:
            return Health(False, CATEGORY_SECRET_ECHO, secret_guard.message_for(exc.code), [], latency)
        detail = f"可用模型 {len(models)} 个"
        if self.model and models and self.model not in models:
            return Health(False, CATEGORY_CONFIG, f"未加载模型 {self.model}；{detail}", models, latency)
        return Health(True, CATEGORY_OK, detail or "已连接", models, latency)

    def complete_json(self, request, on_start=None):
        self.request_sent = False
        self.transmissions = 0
        blocked = self._credential_block()
        if blocked is not None:
            return blocked
        model = request.model or self.model
        if not model:
            return AdapterResult(False, category=CATEGORY_CONFIG,
                                 public_code=secret_guard.E_CREDENTIAL_NOT_BOUND,
                                 message="没有指定模型名称")
        payload = {
            "model": model,
            "temperature": request.temperature,
            "stream": False,
            "max_tokens": self.max_output_tokens,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": request.system},
                         {"role": "user", "content": _user_content(request)}],
        }
        if on_start:
            on_start()
        started = time.time()
        if secret_guard.contains_secret(payload):
            # The prompt must never carry a credential; refuse instead of sending it.
            return self._finish(AdapterResult(False, category=CATEGORY_SECRET_ECHO,
                                              public_code=secret_guard.E_SECRET_ECHO_BLOCKED,
                                              message="请求内容包含凭据特征，已阻止发送"),
                                started, model)
        raw, error, dropped = self._post_with_adaptation(
            "/chat/completions", payload, request.timeout_seconds or self.timeout,
            started, model)
        if error:
            return self._finish(error, started, model)
        try:
            envelope = reading_card.parse_model_json(raw.decode("utf-8"))
        except (UnicodeDecodeError, reading_card.ValidationError, TypeError):
            return self._finish(AdapterResult(False, category=CATEGORY_INVALID_JSON,
                                              public_code=secret_guard.E_INVALID_RESPONSE,
                                              message="响应信封不是合法 JSON"), started, model)
        if envelope.get("error"):
            # The provider's own sentence is not shown: it can quote the request.
            return self._finish(AdapterResult(False, category=CATEGORY_HTTP,
                                              public_code=secret_guard.E_UPSTREAM_ERROR,
                                              message="服务商返回 error 字段"), started, model)
        denial = _tool_call_denial(envelope)
        if denial:
            # An executable action is not reading-card data. Nothing in this program has the
            # authority to run it, so the reply is refused rather than parsed or shown.
            return self._finish(AdapterResult(False, category=CATEGORY_INVALID_JSON,
                                              public_code=secret_guard.E_TOOL_CALL_BLOCKED,
                                              message="模型返回了工具调用，已拒绝"), started, model)
        choices = envelope.get("choices") or []
        if not choices:
            return self._finish(AdapterResult(False, category=CATEGORY_BAD_RESPONSE,
                                              public_code=secret_guard.E_INVALID_RESPONSE,
                                              message="响应里没有 choices"), started, model)
        message = choices[0].get("message") or {}
        content = message.get("content", "")
        diagnosis = self._reply_diagnosis(choices[0], message, content)
        if diagnosis is not None:
            return self._finish(diagnosis, started, model, content)
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
        self.unsent_outcomes = {"error"}   # outcomes that never left the machine

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
        # error models a transport failure that never reached the provider (nothing was
        # sent, so the budget may be refunded and one retry is safe). Every other outcome
        # counts as sent, or the ledger would refund a request that was charged.
        self.request_sent = outcome not in self.unsent_outcomes
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
        if outcome in ("invalid_json", "invalid_response"):
            # `invalid_response` is the public code for this category; accept both names so
            # a test can script the outcome using the vocabulary the UI reports.
            return AdapterResult(False, category=CATEGORY_INVALID_JSON,
                                 public_code=secret_guard.E_INVALID_RESPONSE,
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
    """Resolve a credential reference into a Secret.

    Returns ``(Secret|None, code, source_label)``.

    There is **no plaintext fallback**: a legacy ``data/credentials.json`` is reported to
    the caller but never read, and only environment names this application has registered
    can be resolved. The `files` argument is accepted for call-site compatibility and is
    used solely to *report* the legacy file.
    """
    ref = credentials.normalise_ref(api_key_env)
    if not ref:
        return None, credentials.CODE_NOT_BOUND, ""
    secret, code, _message = credentials.read_reference(ref)
    if secret is not None:
        return secret, credentials.CODE_OK, ref
    if code == credentials.CODE_MISSING and files is not None:
        legacy = credentials.legacy_plaintext_status(files)
        if legacy.get("present"):
            return None, credentials.CODE_LEGACY_FILE, ref
    return None, code, ref


def credential_report(ai_settings, files=None):
    """Safe-to-show credential status. Never contains a value or a masked fragment."""
    settings = ai_settings or {}
    provider = settings.get("provider", "none") or "none"
    ref = credentials.normalise_ref(settings.get("api_key_env", ""))
    secret, code, _label = resolve_api_key(settings.get("api_key_env", ""), files)
    if secret is not None:
        state = "bound"
    elif code in (credentials.CODE_NOT_BOUND,):
        state = "unbound"
    elif code in (credentials.CODE_MISSING, credentials.CODE_LEGACY_FILE):
        state = "missing"
    else:
        state = "refused"
    report = {
        "provider": provider,
        "credential_ref": ref,
        "credential_state": state,
        "credential_code": code,
        "fingerprint": secret.fingerprint() if secret is not None else "",
    }
    if files is not None:
        legacy = credentials.legacy_plaintext_status(files)
        if legacy.get("present"):
            report["legacy_plaintext"] = {"present": True, "size_bytes": legacy.get("size_bytes", 0),
                                          "action": legacy.get("action", "")}
    return report


def build_adapter(ai_settings, files=None, timeout=None, ssl_context=None):
    """Create the adapter named by settings.ai, or DisabledAdapter.

    Only the credential *reference* is stored in the library; the value is read at
    construction time from a registered environment variable, wrapped in a Secret and kept
    in memory for the lifetime of the adapter. `files` is used only to notice a legacy
    plaintext file so the failure can explain itself.

    The target is not contacted here: `EndpointPolicy` runs inside the adapter on the first
    request, and the credential is only placed in a header after that policy has approved
    the destination.
    """
    provider = (ai_settings or {}).get("provider", "none")
    model = (ai_settings or {}).get("model", "") or ""
    base_url = (ai_settings or {}).get("base_url", "") or ""
    mode = (ai_settings or {}).get("mode", "") or endpoint_policy.DEFAULT_MODE
    key, code, _ref = resolve_api_key((ai_settings or {}).get("api_key_env", ""), files)
    if provider == "ollama":
        adapter = OllamaAdapter(base_url=base_url, model=model, timeout=timeout or 120,
                                mode=mode, ssl_context=ssl_context)
        adapter.max_output_tokens = int((ai_settings or {}).get("max_output_tokens")
                                        or adapter.max_output_tokens)
        return adapter
    if provider in ("lmstudio", "openai_compatible"):
        adapter = OpenAICompatibleAdapter(base_url=base_url or DEFAULT_OPENAI_BASE, model=model,
                                          api_key=key, timeout=timeout or 120, mode=mode,
                                          ssl_context=ssl_context)
        adapter.max_output_tokens = int((ai_settings or {}).get("max_output_tokens")
                                        or adapter.max_output_tokens)
        if key is None and code not in (credentials.CODE_NOT_BOUND,):
            # Fail closed with the reason attached, instead of making a request without a
            # credential and reading the provider's 401 as the explanation.
            adapter.credential_code = code
            adapter.credential_missing = True
        return adapter
    return DisabledAdapter("尚未选择本地模型 Provider（settings.ai.provider = none）")
