"""SecureTransport: the only place in the application that opens a network connection.

Properties, each of which is checked by a test rather than promised in a comment:

1. **No automatic redirects.** ``http.client`` never follows a 3xx by itself; the transport
   returns the status, so a redirect cannot carry the Authorization header to a new host.
2. **No implicit proxy.** The connection is opened directly to an address the policy
   approved. Environment proxy variables are reported but never used, and no global opener
   is installed (so a library that calls :mod:`urllib` is unaffected and cannot be affected).
3. **DNS pinning.** Resolution happens once, in the policy. The socket connects to an
   approved address, and the certificate is validated against the approved *hostname* — so a
   name that changes answer between the check and the connect cannot redirect the request.
4. **TLS on, verification on.** ``CERT_REQUIRED`` with ``check_hostname=True`` and SNI set to
   the approved hostname. There is no unverified-connection path and no HTTP fallback: a TLS
   failure is a failure.
5. **Bounded I/O.** Both the connect and the read have deadlines, and the body is capped.

The transport is deliberately small (stdlib only) and has no configuration surface of its
own: everything it may do is decided by an :class:`endpoint_policy.EndpointPolicy`.
"""
from __future__ import annotations

import http.client
import json
import socket
import re
import ssl
import time
import threading
from contextlib import nullcontext
from dataclasses import dataclass, field

import endpoint_policy as policy_mod
import secret_guard

MAX_BODY_BYTES = 8 * 1024 * 1024
DEFAULT_CONNECT_TIMEOUT = 10.0
DEFAULT_READ_TIMEOUT = 120.0
TLS_PORT = 443


#: Parameter names this program may adapt to. Matching happens here, on the raw bytes of an
#: error reply, so no free text from the server is ever carried further.
ADAPTABLE_PARAMETERS = ("response_format", "format", "temperature", "max_tokens",
                        "num_predict", "max_completion_tokens")


def _named_parameters(body):
    """Entries of ADAPTABLE_PARAMETERS named in an error body. Nothing else is read.

    Matching uses word boundaries so `response_format` does not also report `format`: a
    wrong match would drop a parameter the server never complained about.
    """
    if not body:
        return ()
    try:
        text = body[:8192].decode("utf-8", "replace").lower()
    except (AttributeError, ValueError):
        return ()
    found = []
    for name in ADAPTABLE_PARAMETERS:
        if re.search(r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])", text):
            found.append(name)
    return tuple(found)


@dataclass
class TransportResponse:
    ok: bool
    status: int = 0
    body: bytes = b""
    headers: dict = field(default_factory=dict)
    code: str = secret_guard.E_UPSTREAM_ERROR
    message: str = ""
    request_id: str = ""
    connected_ip: str = ""
    duration_ms: int = 0
    #: Optional request parameters a 4xx reply named. Only entries of a fixed vocabulary ever
    #: appear here: the reply text itself is never retained.
    rejected_parameters: tuple = ()

    def as_dict(self):
        """Safe summary: status, fixed code, request id, address — never a header value."""
        return {"ok": self.ok, "status": self.status, "code": self.code,
                "request_id": self.request_id, "connected_ip": self.connected_ip,
                "duration_ms": self.duration_ms, "bytes": len(self.body),
                "rejected_parameters": list(self.rejected_parameters)}

    def public_message(self):
        return secret_guard.message_for(self.code)


class SecureTransport:
    """A direct, pinned, non-redirecting HTTP client for one approved target."""

    def __init__(self, policy, ssl_context=None, connect_timeout=DEFAULT_CONNECT_TIMEOUT,
                 read_timeout=DEFAULT_READ_TIMEOUT, max_body=MAX_BODY_BYTES,
                 fail_closed=True):
        self.policy = policy
        self._ssl_context = ssl_context
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.max_body = max_body
        self.fail_closed = fail_closed
        self.connections = []          # evidence for tests: (host, port, address, tls)
        self.authorized_addresses = []
        self.send_guard = None
        self.cancel_event = None
        self.request_written = False
        self._deadline = 0
        self._socket = None
        self._socket_lock = threading.Lock()

    def _remaining(self):
        remaining = self._deadline - time.monotonic() if self._deadline else self.read_timeout
        if remaining <= 0:
            raise socket.timeout()
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise OutboundDenied(secret_guard.E_CANCELLED)
        return remaining

    def abort(self):
        # shutdown interrupts reads even when HTTPResponse retains a socket file.
        with self._socket_lock:
            sock = self._socket
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            _close_quietly(sock)

    def _track(self, sock):
        with self._socket_lock:
            self._socket = sock
        return sock

    # -- TLS ---------------------------------------------------------------
    def _context(self):
        if self._ssl_context is not None:
            return self._ssl_context
        context = ssl.create_default_context()          # CERT_REQUIRED, check_hostname=True
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        return context

    # -- connection --------------------------------------------------------
    def _connect(self, client):
        """http.client helper: connect to a *verified* address, then do the handshake."""
        target = self.policy.target
        addresses = self.policy.connection_addresses()
        last_error = None
        for family, address in addresses:
            raw = None
            try:
                raw = self._track(socket.socket(family, socket.SOCK_STREAM))
                raw.settimeout(min(self.connect_timeout, self._remaining()))
                raw.connect((address, target.port))
                self.policy.check_connected(address)     # must be an approved address
                self.authorized_addresses.append(address)
                if target.scheme == "https":
                    wrapped = self._track(self._context().wrap_socket(
                        raw, server_hostname=target.host, do_handshake_on_connect=False))    # SNI + hostname verification
                    wrapped.settimeout(min(self.connect_timeout, self._remaining()))
                    wrapped.do_handshake()
                    wrapped.settimeout(self._remaining())
                    self.connections.append({"host": target.host, "port": target.port,
                                             "address": address, "tls": True})
                    return wrapped
                raw.settimeout(self._remaining())
                self.connections.append({"host": target.host, "port": target.port,
                                         "address": address, "tls": False})
                return raw
            except ssl.SSLCertVerificationError as exc:
                last_error = ("tls", f"证书校验失败：{exc.verify_message or 'verify failed'}")
                _close_quietly(raw)
                break
            except ssl.SSLError:
                last_error = ("tls", "TLS 握手失败")
                _close_quietly(raw)
                break
            except policy_mod.PolicyError:
                # Never degraded into "unreachable": an address that is not the approved one
                # is a policy refusal and must surface as one.
                _close_quietly(raw)
                raise
            except (OSError, socket.timeout) as exc:
                last_error = ("connect", type(exc).__name__)
                _close_quietly(raw)
                continue
        if last_error is None:
            last_error = ("connect", "没有可用地址")
        raise _ConnectFailure(*last_error)

    def _request(self, method, path, payload=None, headers=None, timeout=None):
        target = self.policy.target
        if not self.policy.connection_addresses():
            # authorization happens before any credential is read by the caller; if the
            # caller forgot, refuse instead of connecting on an unverified target
            raise policy_mod.PolicyError(policy_mod.CODE_BLOCKED, "目标还没有通过校验")
        url_path = policy_mod.join_path(target.base_path, path)
        body = None
        send_headers = {"Accept": "application/json", "Host": host_header(target)}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            send_headers["Content-Type"] = "application/json"
            send_headers["Content-Length"] = str(len(body))
        for name, value in (headers or {}).items():
            if isinstance(value, bytes):                 # a header that must not be copied
                send_headers[name] = value
            else:
                send_headers[name] = str(value)

        started = time.time()
        connection = None
        done = threading.Event()
        total_timeout = max(0.001, float(timeout or self.read_timeout))
        self._deadline = time.monotonic() + total_timeout
        self.request_written = False

        def watch():
            while not done.wait(min(0.02, max(0.001, self._deadline - time.monotonic()))):
                if time.monotonic() >= self._deadline or (
                        self.cancel_event is not None and self.cancel_event.is_set()):
                    self.abort()
                    return
        watcher = threading.Thread(target=watch, daemon=True, name='bounded-http')
        watcher.start()
        try:
            # A plain HTTPConnection is used for both schemes: `_connect` performs the
            # pinned TCP connect and, for https, the TLS handshake with SNI and full
            # verification. Letting HTTPSConnection wrap the socket as well would negotiate
            # twice, and would leave the address unpinned.
            connection = http.client.HTTPConnection(target.host, target.port,
                                                    timeout=self.read_timeout)
            connection._create_connection = (
                lambda address, timeout=None, source_address=None: self._connect(connection))
            connection.connect()
            # Connect carries no credential. The application lock covers the final
            # authorization check and HTTP write; no response read holds that lock.
            with self.send_guard() if self.send_guard else nullcontext():
                self._remaining()
                connection.sock.settimeout(self._remaining())
                # A partial write may already be billable, so conservatively charge it.
                self.request_written = True
                connection.request(method, url_path, body=body, headers=send_headers)
            self._remaining()
            # Own the response directly. HTTPConnection.getresponse() closes its socket
            # reference for HTTP/1.0/Connection: close replies while a buffered response
            # still reads from it; retaining the reference keeps the deadline watchdog
            # and cancellation able to interrupt both headers and body.
            response = http.client.HTTPResponse(connection.sock, method=method)
            response.begin()
            parts, size = [], 0
            while size <= self.max_body:
                self._remaining()
                with self._socket_lock:
                    active = self._socket
                if active is not None:
                    active.settimeout(self._remaining())
                block = response.read1(min(65536, self.max_body + 1 - size))
                if not block:
                    break
                parts.append(block)
                size += len(block)
            raw = b''.join(parts)
            self._remaining()
            duration = int((time.time() - started) * 1000)
            connected = self.authorized_addresses[-1] if self.authorized_addresses else ""
            status = response.status
            if len(raw) > self.max_body:
                return TransportResponse(False, status=status, code=secret_guard.E_INVALID_RESPONSE,
                                         message="响应体超过大小上限", duration_ms=duration,
                                         connected_ip=connected)
            if status in (301, 302, 303, 307, 308):
                # Returned, not followed: the caller sees a fixed code and the user decides.
                return TransportResponse(
                    False, status=status, code=secret_guard.E_REDIRECT_BLOCKED,
                    message="服务商要求跳转，已按安全策略停止",
                    request_id=request_id_of(dict(response.getheaders())),
                    duration_ms=duration, connected_ip=connected)
            if status in (401, 403):
                return TransportResponse(False, status=status, code=secret_guard.E_AUTH_FAILED,
                                         message="凭据被拒绝", duration_ms=duration,
                                         request_id=request_id_of(dict(response.getheaders())),
                                         connected_ip=connected)
            if status >= 400:
                # headers are kept for error replies too: a 429 carries Retry-After, which
                # the caller may use for a bounded, explicit backoff
                return TransportResponse(False, status=status,
                                         code=secret_guard.E_UPSTREAM_ERROR,
                                         headers=dict(response.getheaders()),
                                         rejected_parameters=_named_parameters(raw),
                                         message="服务商返回错误", duration_ms=duration,
                                         request_id=request_id_of(dict(response.getheaders())),
                                         connected_ip=connected)
            return TransportResponse(True, status=status, body=raw,
                                     headers=dict(response.getheaders()), code="ok",
                                     request_id=request_id_of(dict(response.getheaders())),
                                     duration_ms=duration, connected_ip=connected)
        except OutboundDenied as exc:
            return TransportResponse(False, code=exc.code)
        except policy_mod.PolicyError as exc:
            return TransportResponse(False, code=exc.code)
        except (OSError, http.client.HTTPException, _ConnectFailure) as exc:
            if self.cancel_event is not None and self.cancel_event.is_set():
                code = secret_guard.E_CANCELLED
            elif time.monotonic() >= self._deadline or isinstance(exc, socket.timeout):
                code = secret_guard.E_TIMEOUT
            elif isinstance(exc, (ssl.SSLError, ssl.SSLCertVerificationError)):
                code = secret_guard.E_TLS_FAILED
            else:
                code = getattr(exc, 'code', secret_guard.E_UNREACHABLE)
            return TransportResponse(False, code=code,
                                     duration_ms=int((time.time() - started) * 1000))
        finally:
            done.set()
            if 'response' in locals():
                response.close()
            if connection is not None:
                connection.close()
            with self._socket_lock:
                self._socket = None
            watcher.join(timeout=0.1)
            self._deadline = 0

    def post_json(self, path, payload, headers=None):
        return self._request("POST", path, payload=payload, headers=headers)

    def get_json(self, path, headers=None):
        return self._request("GET", path, headers=headers)


def _close_quietly(sock):
    """Release a socket on a failure path; a leaked fd is a resource leak, not a warning."""
    if sock is None:
        return
    try:
        sock.close()
    except OSError:
        pass


class _ConnectFailure(Exception):
    def __init__(self, kind, detail):
        super().__init__(detail)
        self.kind = kind
        self.code = secret_guard.E_TLS_FAILED if kind == "tls" else secret_guard.E_UNREACHABLE
        self.message = "TLS 校验失败" if kind == "tls" else detail


def host_header(target):
    default = endpoint_policy_default_port(target.scheme)
    return target.host if target.port == default else f"{target.host}:{target.port}"


def endpoint_policy_default_port(scheme):
    return policy_mod.DEFAULT_PORTS.get(scheme, 0)


def request_id_of(headers):
    """Provider request id when present; sanitised to a safe character set."""
    if not headers:
        return ""
    import re
    for name in ("x-request-id", "request-id", "x-amzn-requestid", "cf-ray",
                 "x-ms-request-id", "x-goog-request-id"):
        for key, value in headers.items():
            if key.lower() == name and value:
                cleaned = re.sub(r"[^A-Za-z0-9._:\-]", "", str(value))[:64]
                if cleaned:
                    return cleaned
    return ""


class OutboundDenied(Exception):
    def __init__(self, code):
        super().__init__('outbound request refused')
        self.code = code
