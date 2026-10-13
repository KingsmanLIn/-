"""Credential references: only registered environment variables, no plaintext fallback.

Design (phase 1, option A):

* The library stores a **reference**, never a value: ``env:PAPER_READER_VENDOR_KEY``.
  A bare name such as ``OPENAI_API_KEY`` is accepted on input and normalised to that form
  so existing settings keep working.
* Only references this application owns can be read. The environment holds every secret of
  the machine, so an unregistered name (``AWS_SECRET_ACCESS_KEY``, ``GITHUB_TOKEN``, …) is
  refused with a fixed code instead of being forwarded to an API endpoint.
* If a legacy ``data/credentials.json`` exists it is **reported, never read**. There is no
  fallback path any more; the user is told to move the value into a registered environment
  variable (or, in a later phase, the OS credential store).
* A reference is bound to the profile and the approved endpoint identity. Moving a profile
  to another origin invalidates the binding instead of trying the same key elsewhere.
* Values are validated before use: length cap, no CR/LF/NUL or other header control
  characters, no leading/trailing whitespace. Errors never contain the value.

The value lives in a small object that keeps it out of ``repr`` and ``str`` so an accidental
log line cannot print it.
"""
from __future__ import annotations

import os
import secrets
_IDENTITY_SALT = secrets.token_bytes(32)
import re

# Fixed codes; the UI maps them to sentences. Never include the offending value.
CODE_OK = "ok"
CODE_NOT_BOUND = "credential_not_bound"
CODE_MISSING = "credential_missing"
CODE_UNREGISTERED = "credential_source_unregistered"
CODE_INVALID = "credential_invalid"
CODE_LEGACY_FILE = "credential_legacy_plaintext_present"

REF_PREFIX = "env:"
# Environment names this application is willing to read. Anything else is refused, so a
# mis-typed or hostile name cannot turn into "send that system secret to the API".
OWNED_PREFIXES = ("PAPER_READER_",)
KNOWN_VENDOR_NAMES = frozenset({
    "OPENAI_API_KEY", "AZURE_OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY",
    "MOONSHOT_API_KEY", "DASHSCOPE_API_KEY", "ZHIPUAI_API_KEY", "SILICONFLOW_API_KEY",
    "GROQ_API_KEY", "MISTRAL_API_KEY", "TOGETHER_API_KEY", "OPENROUTER_API_KEY",
    "OLLAMA_API_KEY", "LMSTUDIO_API_KEY", "LM_STUDIO_API_KEY", "LOCALAI_API_KEY",
    "VLLM_API_KEY",
})
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
MAX_KEY_BYTES = 512
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class Secret:
    """A credential value that refuses to reveal itself in logs or traces."""

    __slots__ = ("_value",)

    def __init__(self, value: str):
        self._value = value

    def reveal(self) -> str:
        """Only the transport may call this, and only for an approved target."""
        return self._value

    def __bool__(self):
        return bool(self._value)

    def __len__(self):
        return len(self._value)

    def __repr__(self):
        return "<Secret len=%d>" % len(self._value)

    __str__ = __repr__

    def fingerprint(self) -> str:
        """Non-reversible label for diagnostics; shows nothing usable."""
        import hashlib
        return hashlib.sha256(self._value.encode("utf-8")).hexdigest()[:8]


def normalise_ref(raw: str) -> str:
    """Return the canonical reference form, or '' when the input is empty."""
    text = str(raw or "").strip()
    if not text:
        return ""
    return text if text.startswith(REF_PREFIX) else REF_PREFIX + text


def name_of(ref: str) -> str:
    ref = normalise_ref(ref)
    return ref[len(REF_PREFIX):] if ref.startswith(REF_PREFIX) else ""


def is_registered(name: str) -> bool:
    if not name or not NAME_RE.match(name):
        return False
    return name.upper().startswith(OWNED_PREFIXES) or name.upper() in KNOWN_VENDOR_NAMES


def live_identity(ref):
    """Process-local keyed digest: binds consent to a value without a reusable hash.

    The salt is not persisted. Restarting conservatively requires credential consent
    again, and a value rotation under the same environment name invalidates queued work.
    """
    import hashlib, hmac
    secret, code, _ = read_reference(ref)
    if secret is None:
        return code if ref else ''
    return hmac.new(_IDENTITY_SALT, secret.reveal().encode('utf-8'), hashlib.sha256).hexdigest()


def registry_hint() -> dict:
    """Machine-readable help for the UI: which names may be referenced."""
    return {"prefix": REF_PREFIX, "owned_prefixes": list(OWNED_PREFIXES),
            "known_names": sorted(KNOWN_VENDOR_NAMES),
            "examples": ["PAPER_READER_VENDOR_KEY", "PAPER_READER_BACKUP_KEY",
                         "OPENAI_API_KEY"]}


def validate_value(value: str):
    """(ok, code, message). Message never contains the value."""
    if value is None:
        return False, CODE_MISSING, "没有读到凭据值"
    text = str(value)
    if text != text.strip():
        return False, CODE_INVALID, "凭据值首尾不能有空白字符"
    if not text:
        return False, CODE_MISSING, "凭据值为空"
    if len(text.encode('utf-8')) < 5:
        return False, CODE_INVALID, 'Credential must contain at least five UTF-8 bytes'
    if len(text.encode("utf-8")) > MAX_KEY_BYTES:
        return False, CODE_INVALID, f"凭据值超过 {MAX_KEY_BYTES} 字节"
    if CONTROL_RE.search(text):
        return False, CODE_INVALID, "凭据值含控制字符，已拒绝使用"
    return True, CODE_OK, ""


def read_reference(ref: str):
    """Resolve a reference from the environment.

    Returns ``(Secret|None, code, message)``. Unknown sources and unregistered names fail
    closed with a fixed code; no other storage is consulted.
    """
    ref = normalise_ref(ref)
    if not ref:
        return None, CODE_NOT_BOUND, "这个连接还没有绑定凭据引用"
    if not ref.startswith(REF_PREFIX):
        return None, CODE_UNREGISTERED, "只支持已登记的环境变量引用"
    name = name_of(ref)
    if not is_registered(name):
        return None, CODE_UNREGISTERED, "这个环境变量没有登记为应用凭据，已拒绝读取"
    value = os.environ.get(name)
    if value is None or value == "":
        return None, CODE_MISSING, f"环境变量 {name} 没有设置"
    ok, code, message = validate_value(value)
    if not ok:
        return None, code, message
    return Secret(value), CODE_OK, ""


def legacy_plaintext_status(files):
    """Report a leftover credentials.json without reading a single byte of it."""
    try:
        path = files.credentials
    except AttributeError:
        return {"present": False, "action": ""}
    try:
        present = path.is_file()
        size = path.stat().st_size if present else 0
    except OSError:
        return {"present": False, "action": ""}
    if not present:
        return {"present": False, "action": ""}
    return {
        "present": True,
        "size_bytes": size,
        "code": CODE_LEGACY_FILE,
        "action": ("检测到旧版明文凭据文件。本版本不再读取它：请把密钥写入已登记的环境变量"
                   "（例如 PAPER_READER_VENDOR_KEY），确认可用后再自行删除该文件。"),
    }


#: Field names that older versions used to store a key in plain text. The migration drops
#: them; nothing in this program ever reads their values.
LEGACY_PLAINTEXT_KEYS = ("api_key", "apikey", "key", "secret", "token", "password",
                         "api_key_value", "openai_api_key")


def strip_legacy_plaintext(mapping):
    """Remove legacy plaintext key fields in place.

    Returns the *names* that were removed, never their values. Callers use this to tell the
    user where something was found so they can delete it themselves.
    """
    removed = []
    if not isinstance(mapping, dict):
        return removed
    for name in list(mapping):
        if name in LEGACY_PLAINTEXT_KEYS:
            removed.append(name)
            mapping.pop(name, None)
    return removed


class Binding:
    """Ties a reference to one profile and one approved endpoint identity."""

    __slots__ = ("profile_id", "ref", "endpoint_identity", "revision")

    def __init__(self, profile_id: str, ref: str, endpoint_identity: str, revision: int = 1):
        self.profile_id = str(profile_id or "")
        self.ref = normalise_ref(ref)
        self.endpoint_identity = str(endpoint_identity or "")
        self.revision = int(revision)

    def matches(self, profile_id: str, endpoint_identity: str) -> bool:
        return (self.profile_id == str(profile_id or "")
                and self.endpoint_identity == str(endpoint_identity or ""))

    def as_dict(self) -> dict:
        return {"profile_id": self.profile_id, "credential_ref": self.ref,
                "endpoint_identity": self.endpoint_identity, "credential_revision": self.revision,
                "configured": bool(self.ref)}
