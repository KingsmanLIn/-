"""One output boundary for anything that can leave the process.

Two layers, in this order:

1. **Field whitelists** — the primary defence. DTOs, the failure file, log records and
   exports are built from explicit field lists, so an unknown field cannot travel by
   accident.
2. **Exact-value matching plus narrow pattern matching** — the backstop. Values that the
   process actually read (credentials, session tokens) are registered in memory only; the
   registry is never written to disk, so it cannot become a new plaintext secret list.

The guard deliberately does **not** claim to be able to fix an untrusted provider. It only
makes sure that what this software stores, returns or prints does not carry the secrets it
knows about.

Phase 1 scope: registration, scanning, whitelisting and the fixed error vocabulary.
"""
from __future__ import annotations

import base64
import re
import threading
import urllib.parse

# --- fixed vocabulary ---------------------------------------------------------------
# Public errors are mapped from these; an upstream sentence never becomes user text.
E_AUTH_FAILED = "auth_failed"
E_TLS_FAILED = "tls_failed"
E_ENDPOINT_BLOCKED = "endpoint_blocked"
E_ENDPOINT_INVALID = "endpoint_invalid"
E_REDIRECT_BLOCKED = "redirect_blocked"
E_PROXY_BLOCKED = "proxy_blocked"
E_BUDGET_BLOCKED = "budget_blocked"
E_INVALID_RESPONSE = "invalid_response"
E_EVIDENCE_INVALID = "evidence_invalid"
E_SECRET_ECHO_BLOCKED = "secret_echo_blocked"
E_CREDENTIAL_NOT_BOUND = "credential_not_bound"
E_CREDENTIAL_MISSING = "credential_missing"
E_CREDENTIAL_INVALID = "credential_invalid"
E_UPSTREAM_ERROR = "upstream_error"
E_TIMEOUT = "timeout"
E_UNREACHABLE = "unreachable"
E_REFUSED = "refused"
E_CANCELLED = "cancelled"
E_CONSENT_REQUIRED = "consent_required"
E_OUTCOME_UNKNOWN = "outcome_unknown"
E_PAUSED = "ai_paused"
E_PROFILE_DISABLED = "profile_disabled"
E_TARGET_CHANGED = "target_changed"
E_STALE_GENERATION = "stale_generation"
E_JOB_LIMIT = "job_request_limit"
E_DAY_LIMIT = "daily_request_limit"
E_QUEUE_FULL = "queue_full"
E_TOOL_CALL_BLOCKED = "tool_call_blocked"
E_CAPABILITY_UNSUPPORTED = "capability_unsupported"

MESSAGES = {
    E_AUTH_FAILED: "服务商拒绝了这个凭据（401/403）。请在服务商控制台核对后重新配置。",
    E_TLS_FAILED: "无法验证服务商的 TLS 证书，已停止发送。",
    E_ENDPOINT_BLOCKED: "这个地址类型不被允许（云端模式只允许公开 HTTPS 目标）。",
    E_ENDPOINT_INVALID: "接口地址格式不被接受，请填写规范的 https 地址。",
    E_REDIRECT_BLOCKED: "服务商要求跳转到另一个地址，已按安全策略停止。请直接填写最终地址。",
    E_PROXY_BLOCKED: "检测到代理设置，默认不使用代理，已停止发送。",
    E_BUDGET_BLOCKED: "这次请求超出了本次任务或今日的额度上限。",
    E_INVALID_RESPONSE: "模型返回的内容无法通过结构校验。",
    E_EVIDENCE_INVALID: "引文与原文页码核对不通过，未保存为参考。",
    E_SECRET_ECHO_BLOCKED: "模型返回内容里出现了凭据特征，已整条拒绝，不保存也不展示。",
    E_CREDENTIAL_NOT_BOUND: "这个连接还没有绑定凭据引用。",
    E_CREDENTIAL_MISSING: "没有读到凭据：请确认已登记的环境变量已经设置。",
    E_CREDENTIAL_INVALID: "凭据值不符合要求（长度或控制字符），已拒绝使用。",
    E_UPSTREAM_ERROR: "服务商返回了错误，已按固定分类记录。",
    E_TIMEOUT: "服务商响应超时。",
    E_UNREACHABLE: "无法连接到这个地址。",
    E_REFUSED: "模型拒绝了这个请求；请自行阅读原文并记录。",
    E_CANCELLED: "任务已取消。",
    E_CONSENT_REQUIRED: "这次发送还没有获得你的批准。",
    E_OUTCOME_UNKNOWN: "请求已经发出但结果未知，默认不会自动重发，以免重复计费。",
    E_PAUSED: "AI 出站已暂停；人工阅读不受影响。",
    E_PROFILE_DISABLED: "这个 API 已被停用；重新启用后才能生成。",
    E_TARGET_CHANGED: "接口目标或凭据已变更，需要重新确认后再生成。",
    E_STALE_GENERATION: "这次任务已被更新的任务取代，旧结果不会覆盖新结果。",
    E_JOB_LIMIT: "这篇论文的调用次数已用完（含修复与重试）。",
    E_DAY_LIMIT: "已达到 24 小时内的请求上限，本次未发送。",
    E_QUEUE_FULL: "队列已满，请稍后再试。",
    E_TOOL_CALL_BLOCKED: "模型返回了工具调用，已被拒绝（模型只提供数据，不能执行操作）。",
    E_CAPABILITY_UNSUPPORTED: "服务商不支持必需的请求参数；为避免无上限请求，本次未发送。",
}


def message_for(code: str) -> str:
    return MESSAGES.get(code, "操作没有完成。")


# Every value in MESSAGES is a fixed code; the set lets a caller test membership instead of
# guessing whether a string is safe to store and display.
CODES = frozenset(MESSAGES)


# --- in-memory secret registry ------------------------------------------------------
_REGISTERED: dict[str, int] = {}
_REGISTRY_LOCK = threading.RLock()


def register(value, label: str = ""):
    """Remember a live secret so it can be detected if it ever reaches an output.

    The registry lives in this process only. Phase 1 has no rotation yet, so entries are
    simply kept until the process restarts.
    """
    if not value:
        return
    text = value.reveal() if hasattr(value, "reveal") else str(value)
    if not text:
        return
    with _REGISTRY_LOCK:
        _REGISTERED[text] = _REGISTERED.get(text, 0) + 1



def known_secret_count() -> int:
    with _REGISTRY_LOCK:
        return len(_REGISTERED)

def _registered_values():
    with _REGISTRY_LOCK:
        return tuple(_REGISTERED)


def _encodings(value: str):
    """Common representations an echo could use."""
    forms = {value}
    try:
        forms.add(urllib.parse.quote(value, safe=""))
        forms.add(urllib.parse.quote_plus(value))
    except Exception:                                            # noqa: BLE001
        pass
    try:
        raw = value.encode("utf-8")
        forms.add(base64.b64encode(raw).decode("ascii"))
        forms.add(base64.urlsafe_b64encode(raw).decode("ascii").rstrip("="))
    except Exception:                                            # noqa: BLE001
        pass
    forms.add(value.replace('"', '\\"'))
    return {item for item in forms if item}


def contains_secret(text) -> bool:
    """True when a known live secret appears verbatim or in a common encoding."""
    if not text:
        return False
    haystack = text if isinstance(text, str) else str(text)
    if not haystack:
        return False
    for value in _registered_values():
        for form in _encodings(value):
            if form in haystack:
                return True
    return False


# --- narrow pattern backstop --------------------------------------------------------
# Kept deliberately small: word boundaries and minimum length avoid the classic false
# positive on CSS such as `-webkit-mask-position`.
PATTERNS = (
    re.compile(r"(?<![A-Za-z0-9_])sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9_\-\.\+/]{16,}"),
    re.compile(r"(?i)\b(?:api[_-]?key|apikey|secret|password|passwd|token)"
               r"[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9_\-\.\+/]{16,}"),
    re.compile(r"(?i)authorization[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9_\-\.\+/ ]{16,}"),
)
PLACEHOLDER = "[redacted]"


def redact(text) -> str:
    """Remove pattern-shaped secrets. Complements — never replaces — the whitelists."""
    if not text:
        return ""
    cleaned = text if isinstance(text, str) else str(text)
    for value in _registered_values():
        for form in _encodings(value):
            cleaned = cleaned.replace(form, PLACEHOLDER)
    for pattern in PATTERNS:
        cleaned = pattern.sub(PLACEHOLDER, cleaned)
    return cleaned


def scrub(value):
    """Recursively redact strings inside a JSON-like structure."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {key: scrub(item) for key, item in value.items()}
    if isinstance(value, list):
        return [scrub(item) for item in value]
    return value


def pick(source: dict, allowed) -> dict:
    """Field whitelist: copy only the listed keys, redacting strings on the way out."""
    if not isinstance(source, dict):
        return {}
    out = {}
    for key in allowed:
        if key in source:
            out[key] = scrub(source[key])
    return out


# --- failure record -----------------------------------------------------------------
FAILED_RECORD_FIELDS = ("schema", "paper_id", "prompt_version", "provider", "adapter",
                        "model", "source_sha256", "created_at", "error_code", "errors",
                        "duration_ms", "attempt", "http_status", "request_id", "outcome")


def failure_record(**fields) -> dict:
    """Build a failure record from a fixed field list. Raw model text is never included."""
    record = pick(fields, FAILED_RECORD_FIELDS)
    record.setdefault("errors", [])
    record["outcome"] = record.get("outcome") or "failed"
    return record


LOG_FIELDS = ("event", "paper_id", "provider", "adapter", "model", "duration_ms",
              "category", "codes", "attempt", "status", "stage", "progress", "reason",
              "already_recorded", "ok", "http_status", "request_id", "policy_version",
              "profile_id", "job_id", "count", "outcome")


def log_record(**fields) -> dict:
    """Build a log line from a fixed field list."""
    return pick(fields, LOG_FIELDS)
