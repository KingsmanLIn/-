"""Local session handling: a one-time bootstrap exchanged for a cookie session.

Threat boundary (stated, not overclaimed)
-----------------------------------------
Loopback HTTP plus exact Host/Origin checks stop *browser* cross-site attacks. They do not
prove the caller is the logged-in user: any program running as this user can speak HTTP to
127.0.0.1. What this module adds is that a page cannot obtain management authority simply by
being loaded - the capability has to come from the launcher over a current-user-only pipe,
be exchanged once, and then be presented as an HttpOnly cookie with a CSRF token.

Flow
----
1. `launch.py` reads a one-time bootstrap capability from the launcher pipe and writes it into
   the page's URL fragment (never a query string, never storage, never a log).
2. The page POSTs it to `/api/session/bootstrap`. On success the server creates a session,
   returns a short-lived CSRF token **in the response body only**, and sets an HttpOnly,
   SameSite=Strict session cookie.
3. Later requests authenticate with the cookie; state-changing requests must also carry the
   CSRF token in a header. The token is kept in page memory only.

The cookie is deliberately *not* marked `Secure` and does not use the `__Host-` prefix:
those attributes are ignored or rejected on plain `http://127.0.0.1`, which is the only
transport this service offers. For the same reason the cookie has no `Max-Age`, so it is a
browser-session cookie.

Bootstrap capabilities are single-use, expire quickly, and are revoked when the service
stops. Sessions are revoked on shutdown, on idle expiry, and when their absolute lifetime
ends. Nothing here is written to disk except the public pipe name, which is not a secret.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time
from dataclasses import dataclass, field

BOOTSTRAP_TTL_SECONDS = 120
SESSION_IDLE_SECONDS = 12 * 3600
SESSION_ABSOLUTE_SECONDS = 7 * 24 * 3600
CSRF_TTL_SECONDS = 12 * 3600
COOKIE_NAME = "pr_session"
MAX_SESSIONS = 32
MAX_BOOTSTRAPS = 8

# Fixed outcomes; the UI maps them to sentences. Never include the supplied value.
BOOTSTRAP_OK = "ok"
BOOTSTRAP_UNKNOWN = "bootstrap_unknown"
BOOTSTRAP_EXPIRED = "bootstrap_expired"
BOOTSTRAP_USED = "bootstrap_used"
BOOTSTRAP_MALFORMED = "bootstrap_malformed"
SESSION_MISSING = "session_missing"
SESSION_EXPIRED = "session_expired"
CSRF_MISSING = "csrf_missing"
CSRF_MISMATCH = "csrf_mismatch"


def _now():
    return time.time()


def _token(bytes_=32):
    return secrets.token_urlsafe(bytes_)


@dataclass
class Session:
    session_id: str
    csrf: str
    created_at: float
    last_seen: float
    user_agent_hash: str = ""

    def expired(self, now=None, idle=SESSION_IDLE_SECONDS, absolute=SESSION_ABSOLUTE_SECONDS):
        now = now or _now()
        return (now - self.created_at > absolute) or (now - self.last_seen > idle)

    def as_dict(self):
        """Safe identity: no cookie value, no CSRF value."""
        return {"created_at": int(self.created_at), "last_seen": int(self.last_seen)}


@dataclass
class Bootstrap:
    capability: str
    created_at: float
    used: bool = False
    source: str = ""

    def expired(self, now=None):
        return (now or _now()) - self.created_at > BOOTSTRAP_TTL_SECONDS


@dataclass
class SessionStore:
    """In-memory sessions and bootstrap capabilities. Nothing is persisted."""

    bootstraps: dict = field(default_factory=dict)
    sessions: dict = field(default_factory=dict)
    issued_bootstraps: int = 0
    accepted_bootstraps: int = 0
    rejected_bootstraps: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # -- bootstrap ---------------------------------------------------------
    def issue_bootstrap(self, source="launcher"):
        """Create a one-time capability. Called only from the trusted local pipe."""
        with self._lock:
            self._prune()
            while len(self.bootstraps) >= MAX_BOOTSTRAPS:
                self.bootstraps.pop(next(iter(self.bootstraps)), None)
            capability = _token(32)
            self.bootstraps[capability] = Bootstrap(capability=capability,
                                                    created_at=_now(), source=source)
            self.issued_bootstraps += 1
            return capability

    def exchange_bootstrap(self, capability, user_agent=""):
        """Spend a capability and create a session.

        Returns (session, code). A capability is spent even when the exchange fails after
        matching, so a replay cannot be retried into a session.
        """
        supplied = str(capability or "")
        if not supplied or len(supplied) > 128:
            with self._lock:
                self.rejected_bootstraps += 1
            return None, BOOTSTRAP_MALFORMED
        with self._lock:
            self._prune()
            record = None
            for known, candidate in self.bootstraps.items():
                if secrets.compare_digest(known, supplied):
                    record = candidate
                    break
            if record is None:
                self.rejected_bootstraps += 1
                return None, BOOTSTRAP_UNKNOWN
            if record.used:
                self.rejected_bootstraps += 1
                return None, BOOTSTRAP_USED
            record.used = True
            self.bootstraps.pop(record.capability, None)
            if record.expired():
                # report expiry rather than "unknown": the user needs to know to relaunch
                self.rejected_bootstraps += 1
                return None, BOOTSTRAP_EXPIRED
            session = self._create_session_locked(user_agent)
            self.accepted_bootstraps += 1
            return session, BOOTSTRAP_OK

    # -- sessions ----------------------------------------------------------
    def _create_session_locked(self, user_agent=""):
        while len(self.sessions) >= MAX_SESSIONS:
            self.sessions.pop(next(iter(self.sessions)), None)
        session = Session(session_id=_token(32), csrf=_token(24), created_at=_now(),
                          last_seen=_now(),
                          user_agent_hash=hashlib.sha256(str(user_agent).encode()).hexdigest()[:16])
        self.sessions[session.session_id] = session
        return session

    def create_session(self, user_agent=""):
        with self._lock:
            self._prune()
            return self._create_session_locked(user_agent)

    def lookup(self, session_id):
        """Validate a cookie value. Returns (session, code), refreshing last_seen."""
        supplied = str(session_id or "")
        if not supplied or len(supplied) > 128:
            return None, SESSION_MISSING
        with self._lock:
            self._prune()
            found = None
            for known, candidate in self.sessions.items():
                if secrets.compare_digest(known, supplied):
                    found = candidate
                    break
            if found is None:
                return None, SESSION_MISSING
            if found.expired():
                self.sessions.pop(found.session_id, None)
                return None, SESSION_EXPIRED
            found.last_seen = _now()
            return found, BOOTSTRAP_OK

    def check_csrf(self, session, supplied):
        if session is None:
            return CSRF_MISSING
        if not supplied:
            return CSRF_MISSING
        return BOOTSTRAP_OK if secrets.compare_digest(str(supplied), session.csrf) else CSRF_MISMATCH

    def revoke(self, session_id):
        with self._lock:
            return self.sessions.pop(session_id, None) is not None

    def revoke_all(self):
        with self._lock:
            count = len(self.sessions)
            self.sessions.clear()
            self.bootstraps.clear()
            return count

    def counts(self):
        with self._lock:
            self._prune()
            return {"sessions": len(self.sessions), "bootstraps": len(self.bootstraps),
                    "issued": self.issued_bootstraps, "accepted": self.accepted_bootstraps,
                    "rejected": self.rejected_bootstraps}

    def _prune(self):
        """Drop expired records, but keep an expired *unused* capability long enough that the
        exchange reports `bootstrap_expired` instead of an indistinguishable `unknown`."""
        now = _now()
        for key, record in list(self.bootstraps.items()):
            if record.used:
                self.bootstraps.pop(key, None)
                continue
            if record.expired(now):
                # keep it for one extra TTL window so the reason is reported, then drop
                if now - record.created_at > 2 * BOOTSTRAP_TTL_SECONDS:
                    self.bootstraps.pop(key, None)
        for key, session in list(self.sessions.items()):
            if session.expired(now):
                self.sessions.pop(key, None)


def parse_cookies(header_value):
    """Minimal, strict Cookie parser: name=value pairs, no attributes, bounded length."""
    jar = {}
    text = str(header_value or "")
    if not text or len(text) > 4096:
        return jar
    for part in text.split(";"):
        item = part.strip()
        if not item or "=" not in item:
            continue
        name, _, value = item.partition("=")
        name = name.strip()
        if not name or len(name) > 64 or len(value) > 512:
            continue
        if any(ch in name for ch in " \t\r\n"):
            continue
        jar[name] = value
    return jar


def cookie_header(session_id, max_age=None):
    """The Set-Cookie value.

    No `Secure` and no `__Host-` prefix: this service is plain HTTP on loopback, where those
    attributes make the cookie unusable. SameSite=Strict is what matters for cross-site
    protection here, and HttpOnly keeps it away from page scripts.
    """
    parts = [f"{COOKIE_NAME}={session_id}", "Path=/", "HttpOnly", "SameSite=Strict"]
    if max_age is not None:
        parts.append(f"Max-Age={int(max_age)}")
    return "; ".join(parts)


def launcher_pipe_name(root_fingerprint):
    """Public pipe name for this installation. Not a secret: it carries no capability."""
    digest = hashlib.sha256(f"paper-reader-launcher/{root_fingerprint}".encode()).hexdigest()
    return f"\\\\.\\pipe\\paper-reader-{digest[:24]}"


def effective_uid():
    """The current user's SID, used for the launcher pipe ACL. '' when unavailable."""
    if os.name != "nt":
        return str(os.getuid()) if hasattr(os, "getuid") else ""
    try:
        import ctypes
        from ctypes import wintypes

        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                              ctypes.POINTER(wintypes.HANDLE)]
        advapi32.OpenProcessToken.restype = wintypes.BOOL
        advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                 wintypes.LPVOID, wintypes.DWORD,
                                                 ctypes.POINTER(wintypes.DWORD)]
        advapi32.GetTokenInformation.restype = wintypes.BOOL
        advapi32.ConvertSidToStringSidW.argtypes = [wintypes.LPVOID,
                                                    ctypes.POINTER(ctypes.c_wchar_p)]
        advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        TOKEN_QUERY = 0x0008
        TOKEN_USER = 1
        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), TOKEN_QUERY,
                                         ctypes.byref(token)):
            return ""
        try:
            needed = wintypes.DWORD(0)
            advapi32.GetTokenInformation(token, TOKEN_USER, None, 0, ctypes.byref(needed))
            if not needed.value:
                return ""
            buffer = ctypes.create_string_buffer(needed.value)
            if not advapi32.GetTokenInformation(token, TOKEN_USER, buffer, needed.value,
                                                ctypes.byref(needed)):
                return ""
            # TOKEN_USER begins with SID_AND_ATTRIBUTES { PSID Sid; DWORD Attributes; }
            sid_pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
            string_sid = ctypes.c_wchar_p()
            if not advapi32.ConvertSidToStringSidW(sid_pointer, ctypes.byref(string_sid)):
                return ""
            return string_sid.value or ""
        finally:
            kernel32.CloseHandle(token)
    except Exception:                                            # noqa: BLE001
        return ""
