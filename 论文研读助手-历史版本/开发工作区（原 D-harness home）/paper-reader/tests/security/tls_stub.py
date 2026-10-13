"""A local HTTPS stub that serves the test certificate and records what it received.

Records only: method, path, whether an Authorization header was present and its scheme, the
byte counts, and the field names of a JSON body. No header value and no body text is stored,
so a failure report can never print the material under test.
"""
from __future__ import annotations

import hashlib
import json
import socket
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import secure_transport

CERT_DIR = Path(__file__).resolve().parent / "certs"


def ca_context(trust="server"):
    """A client context that trusts exactly one test certificate.

    A fresh context is built on every call: an SSLContext is mutated by ``wrap_socket``, so
    sharing one instance between the stub's server socket and a client would make the
    verification result depend on call order.
    """
    context = ssl.create_default_context(cafile=str(CERT_DIR / f"{trust}-cert.pem"))
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def server_context(name="server"):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(CERT_DIR / f"{name}-cert.pem"),
                            str(CERT_DIR / f"{name}-key.pem"))
    return context


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        return

    def _record(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(length) if length else b""
        auth = self.headers.get("Authorization", "")
        entry = {
            "method": self.command,
            "path": self.path,
            "auth_present": bool(auth),
            "auth_scheme": auth.split(" ", 1)[0] if auth else "",
            "body_bytes": len(body),
            "body_field_names": [],
            "body_sha256_12": hashlib.sha256(body).hexdigest()[:12] if body else "",
            "tls_version": (self.connection.version() if hasattr(self.connection, "version")
                            else ""),
            "server_name": getattr(self.connection, "server_name", "") or "",
        }
        if body:
            try:
                parsed = json.loads(body.decode("utf-8", "replace"))
                if isinstance(parsed, dict):
                    entry["body_field_names"] = sorted(parsed)[:24]
            except (UnicodeDecodeError, json.JSONDecodeError):
                entry["body_json"] = False
        self.server.requests.append(entry)
        return entry

    def _respond(self):
        mode = self.server.mode
        if mode == "redirect":
            self.send_response(self.server.redirect_status)
            self.send_header("Location", self.server.redirect_location)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if mode == "ok":
            body = json.dumps(self.server.payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
        else:
            body = b"{}"
            self.send_response(self.server.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Request-Id", "tls-stub-1")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    do_GET = do_POST = lambda self: (self._record(), self._respond())      # noqa: E731


class TlsStub:
    """HTTPS (or plain HTTP) endpoint on 127.0.0.1 with a fixed port."""

    def __init__(self, cert="server", mode="ok", payload=None, status=200, tls=True,
                 redirect_status=302, redirect_location="/elsewhere"):
        self.cert = cert
        self.mode = mode
        self.payload = payload if payload is not None else {"data": [{"id": "stub-model"}]}
        self.status = status
        self.use_tls = tls
        self.redirect_status = redirect_status
        self.redirect_location = redirect_location
        self.requests = []
        self.server_names = []
        self.port = 0
        self._server = None

    def start(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        server.daemon_threads = True
        server.mode = self.mode
        server.payload = self.payload
        server.status = self.status
        server.redirect_status = self.redirect_status
        server.redirect_location = self.redirect_location
        server.requests = self.requests
        if self.use_tls:
            context = server_context(self.cert)
            names = self.server_names

            def record_sni(socket_, name, ssl_context):
                names.append(name or "")
                return None                      # accept the handshake

            context.sni_callback = record_sni
            server.socket = context.wrap_socket(server.socket, server_side=True)
        self.port = server.server_address[1]
        self._server = server
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return self

    def stop(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    @property
    def count(self):
        return len(self.requests)

    def paths(self):
        return [item["path"] for item in self.requests]

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
        return False


# --- policy construction helpers ------------------------------------------------------
def policy_for(url, mode, address="127.0.0.1", host=None):
    """A policy for the stub, with the approved address set directly.

    The policy is not asked to resolve anything here: the tests need to exercise the
    *transport* against a real socket on 127.0.0.1, and cloud mode would (correctly) refuse a
    loopback answer. Pinning the verified address is exactly what `authorize()` does after a
    successful resolution, so the transport sees the same state either way.
    """
    import endpoint_policy

    target = endpoint_policy.parse_base_url(url, mode)
    if host is not None:
        target = endpoint_policy.Target(scheme=target.scheme, host=host, port=target.port,
                                       base_path=target.base_path, mode=target.mode)
    policy = endpoint_policy.EndpointPolicy(target)
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    policy._verified = [(family, address)]
    return policy


def transport_for(stub, host="127.0.0.1", mode="local_loopback", context=None, **kwargs):
    """A real SecureTransport pointed at the stub."""
    scheme = "https" if stub.use_tls else "http"
    url = f"{scheme}://127.0.0.1:{stub.port}/v1"
    policy = policy_for(url, mode, host=host)
    return secure_transport.SecureTransport(
        policy,
        ssl_context=context if context is not None else (ca_context() if stub.use_tls else None),
        connect_timeout=5, read_timeout=10, **kwargs)
