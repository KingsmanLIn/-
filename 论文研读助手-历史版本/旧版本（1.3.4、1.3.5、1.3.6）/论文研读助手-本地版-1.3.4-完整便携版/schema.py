"""Library schema, its versions and the migration rules between them.

Pure data: no I/O, no HTTP, no provider access. The server imports this module
for constants, validation and `migrate_state()`.

Schema history
--------------
version 1  settings{vault_path, sync_enabled, last_sync, sync_error}, papers[] without analysis
version 2  + settings.ai{...}, + paper.analysis{...}; every version 1 record is kept verbatim
"""
from __future__ import annotations

import copy
import re
import time

import credentials

SCHEMA_VERSION = 2
# --- settings.ai defaults (version 2) -------------------------------------
PROVIDERS = ("none", "ollama", "lmstudio", "openai_compatible")
ANALYSIS_STATUSES = ("none", "queued", "running", "ready", "failed", "stale",
                     "waiting_for_provider", "waiting_for_user", "cancelled")
ANALYSIS_STAGES = ("queued", "extracting", "chunking", "requesting", "validating", "saving", "done")
ANALYSIS_CLOSED_STATUSES = ("ready", "failed", "stale", "waiting_for_provider",
                            "waiting_for_user", "cancelled")
MAX_CONCURRENT_JOBS_RANGE = (1, 4)
AI_SETTINGS_DEFAULTS = {
    "provider": "none",           # nothing is ever called until the user picks a provider
    "base_url": "",               # empty means: use the provider's default local endpoint
    "model": "",
    "api_key_env": "",            # NAME of an environment variable, never the key itself
    "mode": "local_loopback",     # local_loopback | cloud_https (see endpoint_policy.py)
    # Sent to the provider; bounds what one call can cost. The default is deliberately well
    # above the size of one reading-card answer, because a reasoning ("thinking") model spends
    # output tokens on its reasoning channel first: at 4096 a real model used the whole ceiling
    # on reasoning and returned an empty answer, which reads as a JSON failure. Raise, never
    # lower, unless the provider rejects the value.
    "max_output_tokens": 16384,
    "auto_analyze": False,        # only new imports may enqueue a job, and only when true
    "max_concurrent_jobs": 1,
    "config_revision": 1,
    "credential_revision": 1,
    # Budget and queue ceilings (see job_policy.LIMIT_RANGES). Validated below so a bad
    # number is refused at save time rather than during a job.
    "limits": {"max_queue_depth": 20, "max_requests_per_job": 2,
                "daily_request_limit": 100},
}

# --- paper.analysis defaults (version 2) ----------------------------------
ANALYSIS_DEFAULTS = {
    "status": "none",
    "stage": "",
    "progress": 0,
    "message": "",
    "source_sha256": "",
    "prompt_version": "",
    "provider": "",
    "adapter": "",
    "model": "",
    "language": "",
    "requested_at": "",
    "started_at": "",
    "completed_at": "",
    "updated_at": "",
    "duration_ms": 0,
    "attempt": 0,
    "error_code": "",
    "error_message": "",
    "answer_file": "",
}
ANALYSIS_FILE_SCHEMA = 1          # schema of data/analyses/<id>.json
EXTRACTED_FILE_SCHEMA = 1         # schema of data/extracted/<id>.json

# --- validation limits ----------------------------------------------------
MAX_ERROR_MESSAGE = 500
MAX_ANALYSIS_MESSAGE = 300
MAX_MODEL_NAME = 200
MAX_BASE_URL = 300
MAX_OUTPUT_TOKENS_RANGE = (256, 32768)
MAX_ENV_NAME = 100
MAX_ANSWER_FILE = 120
MAX_SHA256_HEX = 64
MAX_LANGUAGE = 8
PROGRESS_RANGE = (0, 100)
PROMPT_VERSION_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,99}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ANALYSIS_FILE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,110}\.json$")
URL_RE = re.compile(r"^https?://[^\s\"'<>]{1,290}$")


class SchemaError(ValueError):
    """Raised when incoming data does not satisfy the version 2 contract."""


def _text(value, limit, field):
    if not isinstance(value, str):
        raise SchemaError(f"{field} 必须是文本")
    if len(value) > limit:
        raise SchemaError(f"{field} 超过长度上限 {limit}")
    return value


def _timestamp(value, field):
    text = _text(value, 40, field)
    if text:
        try:
            time.strptime(text[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError as exc:
            raise SchemaError(f"{field} 不是有效的时间戳") from exc
    return text


def validate_limits(raw):
    """Merge a limits patch onto the defaults, refusing out-of-range numbers."""
    base = dict(AI_SETTINGS_DEFAULTS["limits"])
    if raw is None:
        return base
    if not isinstance(raw, dict):
        raise SchemaError("limits 必须是对象")
    unknown = set(raw) - set(base)
    if unknown:
        raise SchemaError("limits 含有未知字段：" + "、".join(sorted(unknown)))
    for key in base:
        if key not in raw:
            continue
        value = raw[key]
        if not isinstance(value, int) or isinstance(value, bool):
            raise SchemaError(f"limits.{key} 必须是整数")
        low, high = LIMIT_RANGES[key]
        if not (low <= value <= high):
            raise SchemaError(f"limits.{key} 必须在 {low}–{high} 之间")
        base[key] = value
    return base


# The ranges live in job_policy; duplicated here as literals so schema stays dependency-free.
LIMIT_RANGES = {"max_queue_depth": (1, 500), "max_requests_per_job": (1, 4),
                "daily_request_limit": (1, 10000)}


def default_ai_settings():
    return copy.deepcopy(AI_SETTINGS_DEFAULTS)


def default_analysis():
    return copy.deepcopy(ANALYSIS_DEFAULTS)


def _normalise_credential_ref(raw):
    """`NAME` -> `env:NAME`, `env:NAME` unchanged, anything else kept for validation."""
    text = _text(raw, MAX_ENV_NAME + 8, "api_key_env")
    if not text:
        return ""
    return text if ":" in text else "env:" + text


def validate_ai_settings(raw):
    """Return a normalised settings.ai dict or raise SchemaError."""
    if not isinstance(raw, dict):
        raise SchemaError("settings.ai 必须是对象")
    unknown = set(raw) - set(AI_SETTINGS_DEFAULTS)
    if unknown:
        raise SchemaError("settings.ai 含有未知字段：" + "、".join(sorted(unknown)))
    merged = default_ai_settings()
    merged.update(raw)
    if merged["provider"] not in PROVIDERS:
        raise SchemaError("AI Provider 无效")
    if not isinstance(merged["auto_analyze"], bool):
        raise SchemaError("auto_analyze 必须是布尔值")
    jobs = merged["max_concurrent_jobs"]
    if not isinstance(jobs, int) or isinstance(jobs, bool) or not (
            MAX_CONCURRENT_JOBS_RANGE[0] <= jobs <= MAX_CONCURRENT_JOBS_RANGE[1]):
        raise SchemaError(f"max_concurrent_jobs 必须在 {MAX_CONCURRENT_JOBS_RANGE[0]}–{MAX_CONCURRENT_JOBS_RANGE[1]} 之间")
    merged["base_url"] = _text(merged["base_url"], MAX_BASE_URL, "base_url")
    if merged["base_url"] and not URL_RE.match(merged["base_url"]):
        raise SchemaError("base_url 必须是 http(s) 地址")
    merged["mode"] = _text(merged["mode"], 32, "mode") or "local_loopback"
    if merged["mode"] not in ("local_loopback", "cloud_https"):
        raise SchemaError("mode 只能是 local_loopback 或 cloud_https")
    merged["limits"] = validate_limits(merged.get("limits"))
    tokens = merged["max_output_tokens"]
    if not isinstance(tokens, int) or isinstance(tokens, bool) or not (
            MAX_OUTPUT_TOKENS_RANGE[0] <= tokens <= MAX_OUTPUT_TOKENS_RANGE[1]):
        raise SchemaError(f"max_output_tokens 必须在 {MAX_OUTPUT_TOKENS_RANGE[0]}–{MAX_OUTPUT_TOKENS_RANGE[1]} 之间")
    for revision in ("config_revision", "credential_revision"):
        value = merged[revision]
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 2**53:
            raise SchemaError("invalid_revision")
    merged["model"] = _text(merged["model"], MAX_MODEL_NAME, "model")
    # api_key_env holds a *reference* to a credential, never a value. A bare environment
    # name is accepted for backward compatibility and normalised to `env:NAME` so the stored
    # form is unambiguous about which credential source it points at.
    merged["api_key_env"] = _normalise_credential_ref(merged["api_key_env"])
    name = merged["api_key_env"][4:] if merged["api_key_env"].startswith("env:") else ""
    if name and not ENV_NAME_RE.match(name):
        raise SchemaError("api_key_env 必须是环境变量的名字，不能填写密钥本身")
    if merged["api_key_env"] and not merged["api_key_env"].startswith("env:"):
        raise SchemaError("api_key_env 只支持已登记的环境变量引用")
    return merged


def validate_analysis(raw):
    """Return a normalised paper.analysis dict or raise SchemaError."""
    if not isinstance(raw, dict):
        raise SchemaError("analysis 必须是对象")
    unknown = set(raw) - set(ANALYSIS_DEFAULTS)
    if unknown:
        raise SchemaError("analysis 含有未知字段：" + "、".join(sorted(unknown)))
    merged = default_analysis()
    merged.update(raw)
    if merged["status"] not in ANALYSIS_STATUSES:
        raise SchemaError("analysis.status 无效")
    merged["stage"] = _text(merged["stage"], 24, "stage")
    if merged["stage"] and merged["stage"] not in ANALYSIS_STAGES:
        raise SchemaError("analysis.stage 无效")
    progress = merged["progress"]
    if not isinstance(progress, int) or isinstance(progress, bool) or not (
            PROGRESS_RANGE[0] <= progress <= PROGRESS_RANGE[1]):
        raise SchemaError(f"analysis.progress 必须是 {PROGRESS_RANGE[0]}–{PROGRESS_RANGE[1]} 的整数")
    merged["message"] = _text(merged["message"], MAX_ANALYSIS_MESSAGE, "message")
    attempts = merged["attempt"]
    if not isinstance(attempts, int) or isinstance(attempts, bool) or not 0 <= attempts <= 1000:
        raise SchemaError("analysis.attempt 必须是 0–1000 的整数")
    duration = merged["duration_ms"]
    if not isinstance(duration, int) or isinstance(duration, bool) or not 0 <= duration <= 86400000:
        raise SchemaError("analysis.duration_ms 无效")
    merged["language"] = _text(merged["language"], MAX_LANGUAGE, "language")
    if merged["language"] and merged["language"] not in ("zh", "en"):
        raise SchemaError("analysis.language 只能是 zh 或 en")
    merged["source_sha256"] = _text(merged["source_sha256"], MAX_SHA256_HEX, "source_sha256").lower()
    if merged["source_sha256"] and not SHA256_RE.match(merged["source_sha256"]):
        raise SchemaError("source_sha256 必须是 64 位十六进制")
    merged["prompt_version"] = _text(merged["prompt_version"], 32, "prompt_version")
    if merged["prompt_version"] and not PROMPT_VERSION_RE.match(merged["prompt_version"]):
        raise SchemaError("prompt_version 只能包含字母、数字、点、下划线和短横线")
    merged["provider"] = _text(merged["provider"], 40, "provider")
    if merged["provider"] and merged["provider"] not in PROVIDERS:
        raise SchemaError("analysis.provider 无效")
    # dapter is the concrete adapter that ran (may be a test fake), kept apart from
    # the configured provider so a fake can never masquerade as a real provider.
    merged["adapter"] = _text(merged["adapter"], 40, "adapter")
    merged["model"] = _text(merged["model"], MAX_MODEL_NAME, "model")
    for key in ("requested_at", "started_at", "completed_at", "updated_at"):
        merged[key] = _timestamp(merged[key], key)
    merged["error_code"] = _text(merged["error_code"], 40, "error_code")
    merged["error_message"] = _text(merged["error_message"], MAX_ERROR_MESSAGE, "error_message")
    merged["answer_file"] = _text(merged["answer_file"], MAX_ANSWER_FILE, "answer_file")
    if merged["answer_file"] and not ANALYSIS_FILE_RE.match(merged["answer_file"]):
        raise SchemaError("answer_file 必须是 analyses 目录内的文件名")
    return merged


def analysis_metadata(source_sha256="", status="queued", language="", provider="", model="",
                      prompt_version=""):
    """Fresh metadata for a paper whose analysis state is being initialised.

    `status` is honoured as given (queued / waiting_for_provider / waiting_for_user / ...);
    only a runnable status gets a stage and a request timestamp.
    """
    stamp = _utc_now()
    meta = default_analysis()
    runnable = status in ("queued", "running")
    meta.update(status=status, stage="queued" if runnable else "",
                progress=0, source_sha256=source_sha256, language=language,
                provider=provider, model=model, prompt_version=prompt_version,
                requested_at=stamp if runnable else "", updated_at=stamp)
    return meta


def _utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"


def analysis_is_stale(meta, source_sha256="", prompt_version="", model="", language=""):
    """True when a stored answer no longer matches what would be produced today.

    Used for the idempotency check before queueing work: an answer produced from the
    same PDF, prompt version, model and language does not need to run again.
    """
    if not isinstance(meta, dict):
        return True
    if meta.get("status") != "ready":
        return True
    for key, current in (("source_sha256", source_sha256), ("prompt_version", prompt_version),
                         ("model", model), ("language", language)):
        if current and (meta.get(key) or "") != current:
            return True
    return False


# --- migrations -----------------------------------------------------------

def _migrate_1_to_2(state):
    """version 1 -> 2. Idempotent: every step only fills what is missing."""
    settings = state.setdefault("settings", {})
    ai = settings.get("ai")
    if isinstance(ai, dict):
        # An old library may keep a plaintext key in settings.ai. It is dropped here, by
        # name and without reading the value, and the location is recorded so the user can
        # delete it. Leaving it would both fail validation and preserve a stored secret.
        removed = credentials.strip_legacy_plaintext(ai)
        if removed:
            state.setdefault("legacy_plaintext", {})
            state["legacy_plaintext"]["settings.ai"] = sorted(removed)
    settings["ai"] = validate_ai_settings(ai) if isinstance(ai, dict) else default_ai_settings()
    # An old library predates the approval model, so it cannot carry a standing authorisation.
    # Automatic analysis is switched off during migration and the user turns it back on:
    # importing a paper must never become the first request to a target that nobody has
    # approved in this installation.
    settings["ai"]["auto_analyze"] = False
    for paper in state.get("papers", []):
        paper["analysis"] = validate_analysis(paper["analysis"]) if isinstance(paper.get("analysis"), dict) \
            else default_analysis()
    state["version"] = 2
    return state


def _migrate_2_to_3(state):
    """Placeholder for the next version; keeps the migration chain explicit."""
    raise SchemaError("当前版本无法读取这个数据库，请先保留备份")


MIGRATIONS = {1: _migrate_1_to_2, 2: _migrate_2_to_3}


def state_fingerprint(state):
    """Stable text of everything the migration may touch, used by tests."""
    settings = state.get("settings", {})
    return {
        "version": state.get("version"),
        "ai": copy.deepcopy(settings.get("ai")),
        "sync": {k: settings.get(k) for k in ("vault_path", "sync_enabled", "last_sync", "sync_error")},
        "papers": [{
            "id": p.get("id"),
            "analysis": copy.deepcopy(p.get("analysis")),
            "revision": p.get("revision"),
        } for p in state.get("papers", [])],
    }


def migrate_state(state):
    """Migrate a decoded library in place.

    Returns (state, changed). `changed` is False for an already current library,
    which is what makes running the migration twice a no-op.
    """
    if not isinstance(state, dict):
        raise SchemaError("数据库内容不是对象")
    version = state.get("version")
    if not isinstance(version, int) or version < 1:
        raise SchemaError("数据库缺少有效的 version")
    if version > SCHEMA_VERSION:
        raise SchemaError("这个数据库来自更新的软件版本，请先升级软件或保留备份")
    changed = False
    while version < SCHEMA_VERSION:
        step = MIGRATIONS.get(version)
        if step is None:
            raise SchemaError(f"缺少 version {version} 的迁移规则")
        step(state)
        version = state["version"]
        changed = True
    # A version 2 file may still be missing its blocks (older beta or hand-edited copy).
    settings = state.setdefault("settings", {})
    if not isinstance(settings.get("ai"), dict):
        settings["ai"] = default_ai_settings()
        changed = True
    else:
        normalised = validate_ai_settings(settings["ai"])
        if normalised != settings["ai"]:
            settings["ai"] = normalised
            changed = True
    for paper in state.get("papers", []):
        if not isinstance(paper.get("analysis"), dict):
            paper["analysis"] = default_analysis()
            changed = True
            continue
        stored = paper["analysis"]
        # Fields introduced by a later release must appear on older records too, otherwise a
        # library written by an earlier build fails validation on load.
        for key, value in ANALYSIS_DEFAULTS.items():
            if key not in stored:
                stored[key] = copy.deepcopy(value)
                changed = True
        normalised = validate_analysis(stored)
        if normalised != stored:
            paper["analysis"] = normalised
            changed = True
    if state.get("version") != SCHEMA_VERSION:
        state["version"] = SCHEMA_VERSION
        changed = True
    # Never let a migration arm the AI on its own.
    ai = settings["ai"]
    if ai.get("provider") not in PROVIDERS or ai["provider"] == "none":
        ai["provider"] = "none"
    return state, changed
