"""Launcher pipe: the only way a bootstrap capability is created.

The launcher (`launch.py`) and the service run as the same user on the same machine. The pipe
is created with an ACL that grants access to that user's SID and to SYSTEM/Administrators
only, so another user on the machine cannot ask for a capability. Messages are length-prefixed
UTF-8 JSON with a small cap; :mod:`pickle` is never used.

The channel carries a *request* ("give me a bootstrap"), never a secret on the wire, and the
service answers with a one-time capability that only the page can spend. If the pipe cannot be
created the service still starts: a session then has to be established through the loopback
API with the header token, and the page shows a hint to relaunch.
"""
from __future__ import annotations

import json
import os
import socket
import struct
import threading

import session

MAX_MESSAGE_BYTES = 4096
PIPE_TIMEOUT_SECONDS = 5.0
MESSAGE_GIVE_BOOTSTRAP = "bootstrap"
MESSAGE_STATUS = "status"


def _accepted_codes():
    return {session.BOOTSTRAP_OK}


class LauncherPipe:
    """A current-user-only named pipe that hands out bootstrap capabilities."""

    def __init__(self, name, sessions, log=None, on_bootstrap=None):
        self.name = name
        self.sessions = sessions
        self.log = log or (lambda record: None)
        self.on_bootstrap = on_bootstrap
        self._thread = None
        self._stop = threading.Event()
        self.created = False
        self.error = ""
        self.requests = 0

    # -- lifecycle ---------------------------------------------------------
    def start(self):
        """Create the pipe and start accepting. Returns True when it is usable."""
        self.created = self._create()
        if not self.created:
            return False
        self._thread = threading.Thread(target=self._serve, daemon=True,
                                        name="launcher-pipe")
        self._thread.start()
        return True

    def stop(self):
        self._stop.set()
        # a connect/accept cannot be cancelled portably; the thread is a daemon and the pipe
        # handle is closed when the process exits

    # -- platform plumbing -------------------------------------------------
    def _create(self):
        if os.name != "nt":
            self.error = "named pipes are Windows-only"
            return False
        try:
            import win32pipe  # type: ignore                # noqa: F401
        except ImportError:
            pass
        try:
            return self._create_windows()
        except Exception as exc:                             # noqa: BLE001
            self.error = f"{type(exc).__name__}"
            return False

    def _create_windows(self):
        """Build the pipe with a DACL limited to the current user."""
        import ctypes
        from ctypes import wintypes

        advapi32 = ctypes.windll.advapi32
        kernel32 = ctypes.windll.kernel32

        class SECURITY_ATTRIBUTES(ctypes.Structure):
            _fields_ = [("nLength", wintypes.DWORD),
                        ("lpSecurityDescriptor", wintypes.LPVOID),
                        ("bInheritHandle", wintypes.BOOL)]

        # SDDL: protected DACL, full access for the current user and for SYSTEM/Admins.
        user_sid = session.effective_uid()
        if not user_sid:
            self.error = "current user SID unavailable"
            return False
        sddl = f"D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GA;;;{user_sid})"
        descriptor = wintypes.LPVOID()
        if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                ctypes.c_wchar_p(sddl), 1, ctypes.byref(descriptor), None):
            self.error = "security descriptor rejected"
            return False

        attributes = SECURITY_ATTRIBUTES()
        attributes.nLength = ctypes.sizeof(attributes)
        attributes.lpSecurityDescriptor = descriptor
        attributes.bInheritHandle = False

        PIPE_ACCESS_DUPLEX = 0x00000003
        PIPE_TYPE_BYTE = 0x00000000
        PIPE_READMODE_BYTE = 0x00000000
        PIPE_WAIT = 0x00000000
        PIPE_UNLIMITED_INSTANCES = 255
        handle = kernel32.CreateNamedPipeW(
            ctypes.c_wchar_p(self.name), PIPE_ACCESS_DUPLEX,
            PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT, PIPE_UNLIMITED_INSTANCES,
            MAX_MESSAGE_BYTES, MAX_MESSAGE_BYTES, 0, ctypes.byref(attributes))
        if handle == wintypes.HANDLE(-1).value or handle == -1:
            self.error = f"CreateNamedPipe failed ({kernel32.GetLastError()})"
            return False
        self._handle = handle
        self._kernel32 = kernel32
        return True

    def _serve(self):
        kernel32 = self._kernel32
        ERROR_PIPE_CONNECTED = 535
        while not self._stop.is_set():
            connected = kernel32.ConnectNamedPipe(self._handle, None)
            if not connected and kernel32.GetLastError() != ERROR_PIPE_CONNECTED:
                break
            try:
                self._handle_client()
            finally:
                kernel32.DisconnectNamedPipe(self._handle)

    def _handle_client(self):
        kernel32 = self._kernel32
        raw = b""
        deadline = 0
        while len(raw) < MAX_MESSAGE_BYTES:
            chunk = self._read_chunk()
            if not chunk:
                break
            raw += chunk
            if len(raw) >= 4:
                (size,) = struct.unpack("<I", raw[:4])
                if size > MAX_MESSAGE_BYTES:
                    break
                if len(raw) >= 4 + size:
                    raw = raw[4:4 + size]
                    break
            deadline += 1
            if deadline > 64:
                break
        try:
            message = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write({"ok": False, "error": "malformed"})
            return
        self.requests += 1
        if not isinstance(message, dict) or message.get("action") != MESSAGE_GIVE_BOOTSTRAP:
            self._write({"ok": False, "error": "unsupported"})
            return
        capability = self.sessions.issue_bootstrap(source="launcher")
        self.log({"event": "bootstrap_issued", "count": self.requests})
        if self.on_bootstrap is not None:
            try:
                self.on_bootstrap(capability)
            except Exception:                                # noqa: BLE001
                pass
        self._write({"ok": True, "capability": capability,
                     "ttl": session.BOOTSTRAP_TTL_SECONDS})

    def _read_chunk(self):
        import ctypes
        from ctypes import wintypes
        kernel32 = self._kernel32
        buffer = ctypes.create_string_buffer(MAX_MESSAGE_BYTES)
        read = wintypes.DWORD(0)
        ok = kernel32.ReadFile(self._handle, buffer, MAX_MESSAGE_BYTES, ctypes.byref(read), None)
        if not ok:
            return b""
        return buffer.raw[:read.value]

    def _write(self, payload):
        import ctypes
        from ctypes import wintypes
        body = json.dumps(payload).encode("utf-8")
        frame = struct.pack("<I", len(body)) + body
        written = wintypes.DWORD(0)
        self._kernel32.WriteFile(self._handle, frame, len(frame), ctypes.byref(written), None)


def request_bootstrap(pipe_name, timeout=PIPE_TIMEOUT_SECONDS):
    """Ask the service for a bootstrap capability (used by launch.py).

    Returns (capability, error). The capability is a single-use, short-lived value; it is
    never logged and never written to disk by either side.
    """
    if os.name != "nt":
        return "", "unsupported_platform"
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.windll.kernel32
    GENERIC_READ_WRITE = 0x80000000 | 0x40000000
    OPEN_EXISTING = 3
    handle = kernel32.CreateFileW(ctypes.c_wchar_p(pipe_name), GENERIC_READ_WRITE, 0, None,
                                  OPEN_EXISTING, 0, None)
    if handle == -1 or handle == wintypes.HANDLE(-1).value:
        return "", f"pipe_unavailable_{kernel32.GetLastError()}"
    try:
        body = json.dumps({"action": MESSAGE_GIVE_BOOTSTRAP, "version": 1}).encode("utf-8")
        frame = struct.pack("<I", len(body)) + body
        written = wintypes.DWORD(0)
        if not kernel32.WriteFile(handle, frame, len(frame), ctypes.byref(written), None):
            return "", "write_failed"
        buffer = ctypes.create_string_buffer(MAX_MESSAGE_BYTES)
        read = wintypes.DWORD(0)
        if not kernel32.ReadFile(handle, buffer, MAX_MESSAGE_BYTES, ctypes.byref(read), None):
            return "", "read_failed"
        raw = buffer.raw[:read.value]
        if len(raw) < 4:
            return "", "short_reply"
        (size,) = struct.unpack("<I", raw[:4])
        if size > MAX_MESSAGE_BYTES or len(raw) < 4 + size:
            return "", "bad_reply"
        payload = json.loads(raw[4:4 + size].decode("utf-8"))
        if not payload.get("ok"):
            return "", str(payload.get("error") or "refused")
        return str(payload.get("capability") or ""), ""
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return "", type(exc).__name__
    finally:
        kernel32.CloseHandle(handle)
