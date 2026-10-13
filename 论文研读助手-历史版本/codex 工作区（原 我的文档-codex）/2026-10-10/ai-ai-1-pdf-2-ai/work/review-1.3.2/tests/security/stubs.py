"""Shared fixtures for the AI security tests.

Rules (from the security test specification):
* every credential used here is a **fictional sentinel** created in the test process;
* the sentinel is installed with ``mock.patch.dict`` so no real environment variable is
  read, changed or printed;
* the counting sink records only *whether* a header/field was present and how many bytes
  arrived - never a value, never a prompt;
* assertions report the channel (file, field, endpoint) where a sentinel was found, not
  the sentinel itself.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import secret_guard


class Sentinel:
    """A fictional credential plus the encodings an echo could use."""

    def __init__(self, seed: str, style: str = "prefixed"):
        digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
        if style == "prefixed":                     # sk-… (the vendor shape)
            self.value = "sk-test-" + digest[:24]
        elif style == "opaque":                     # no vendor prefix at all
            self.value = "t3st" + digest[:28]
        elif style == "symbols":                    # legal punctuation, no sk- prefix
            self.value = "Zx9._-" + digest[:20] + "~+/"
        elif style == "long":
            self.value = "sentinel" + digest + digest[:32]
        elif style == "short":                      # short but above the minimum length
            self.value = "ab" + digest[:6]
        else:
            raise ValueError(style)
        self.name = "PAPER_READER_TEST_" + style.upper()

    def encodings(self):
        """All forms that must be treated as the same secret."""
        import base64
        import urllib.parse
        raw = self.value.encode("utf-8")
        return {
            "plain": self.value,
            "url": urllib.parse.quote(self.value, safe=""),
            "query": urllib.parse.quote_plus(self.value),
            "base64": base64.b64encode(raw).decode("ascii"),
            "base64_nopad": base64.urlsafe_b64encode(raw).decode("ascii").rstrip("="),
            "json": json.dumps(self.value)[1:-1],
        }

    def channels(self, blob):
        """Which encoding of this sentinel appears in `blob` (never returns the value)."""
        text = blob.decode("utf-8", "replace") if isinstance(blob, bytes) else str(blob)
        return sorted(name for name, form in self.encodings().items() if form in text)

    def __repr__(self):
        return f"<Sentinel {self.name} {len(self.value)} chars>"


class CountingSink:
    """A local HTTP endpoint that answers like a provider and counts what it received.

    It stores booleans, field names and byte counts only. No header value, no body text,
    so a failure report can never leak the material under test.
    """

    def __init__(self, style="openai", reply="ok", card=None, status=200, redirect_to=None,
                 delay=0.0, slow_headers=False):
        self.style = style
        self.reply = reply
        self.card = card or {}
        self.status = status
        self.redirect_to = redirect_to
        self.delay = delay
        self.slow_headers = slow_headers
        self.requests = []
        self._lock = threading.Lock()
        self._server = None
        self._thread = None
        self.port = 0

    # -- lifecycle ---------------------------------------------------------
    def start(self):
        sink = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                return

            def _record(self, method):
                length = int(self.headers.get("Content-Length", "0") or 0)
                body = self.rfile.read(length) if length else b""
                entry = {
                    "method": method,
                    "path": self.path,
                    "host_header": bool(self.headers.get("Host")),
                    "auth_header_present": bool(self.headers.get("Authorization")),
                    "auth_scheme": (self.headers.get("Authorization", "").split(" ")[0]
                                    if self.headers.get("Authorization") else ""),
                    "body_bytes": len(body),
                    "body_field_names": [],
                    "body_sha256": hashlib.sha256(body).hexdigest()[:12] if body else "",
                    "user_agent_present": bool(self.headers.get("User-Agent")),
                }
                if body:
                    try:
                        parsed = json.loads(body.decode("utf-8", "replace"))
                        if isinstance(parsed, dict):
                            entry["body_field_names"] = sorted(parsed)[:24]
                            messages = parsed.get("messages")
                            if isinstance(messages, list):
                                entry["message_count"] = len(messages)
                                entry["message_chars"] = sum(
                                    len(str(item.get("content", ""))) for item in messages
                                    if isinstance(item, dict))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        entry["body_json"] = False
                with sink._lock:
                    sink.requests.append(entry)
                return body

            def _reply(self):
                if sink.delay:
                    import time
                    time.sleep(sink.delay)
                if sink.redirect_to:
                    payload = b""
                    self.send_response(sink.status)
                    self.send_header("Location", sink.redirect_to)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if sink.reply == "ok":
                    content = json.dumps(sink.card, ensure_ascii=False)
                    if sink.style == "ollama":
                        envelope = {"model": "stub", "done": True,
                                    "message": {"role": "assistant", "content": content}}
                    else:
                        envelope = {"model": "stub",
                                    "choices": [{"index": 0, "finish_reason": "stop",
                                                 "message": {"role": "assistant",
                                                             "content": content}}]}
                    body = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
                    self.send_response(200)
                elif sink.reply == "prose":
                    content = "I am sorry, I cannot answer that in JSON."
                    if sink.style == "ollama":
                        envelope = {"message": {"content": content}}
                    else:
                        envelope = {"choices": [{"message": {"content": content}}]}
                    body = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
                    self.send_response(200)
                elif sink.reply == "echo_error":
                    # A gateway that echoes the request, the prompt and the credential.
                    body = json.dumps({
                        "error": "invalid_request",
                        "authorization": self.headers.get("Authorization", ""),
                        "prompt_echo": "分级诊疗制度下的医疗控费机制",
                    }, ensure_ascii=False).encode("utf-8")
                    self.send_response(sink.status if sink.status != 200 else 401)
                elif sink.reply == "huge":
                    body = b'{"blob": "' + b"A" * (9 * 1024 * 1024) + b'"}'
                    self.send_response(200)
                elif sink.reply == "bad_json":
                    body = b"{not json at all"
                    self.send_response(200)
                else:
                    body = b"{}"
                    self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("X-Request-Id", "stub-request-1")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_POST(self):
                self._record("POST")
                self._reply()

            def do_GET(self):
                self._record("GET")
                self._reply()

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    # -- inspection --------------------------------------------------------
    @property
    def count(self):
        with self._lock:
            return len(self.requests)

    def paths(self):
        with self._lock:
            return [item["path"] for item in self.requests]

    def any_auth(self):
        with self._lock:
            return any(item["auth_header_present"] for item in self.requests)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
        return False


def text_of(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def find_in_tree(root: Path, sentinel: Sentinel, skip_suffix=()):
    """Which files under `root` contain the sentinel, per encoding. Values are not returned."""
    hits = {}
    for item in Path(root).rglob("*"):
        if not item.is_file() or item.suffix in skip_suffix:
            continue
        channels = sentinel.channels(item.read_bytes())
        if channels:
            hits[str(item.relative_to(root))] = channels
    return hits


def register(sentinel: Sentinel):
    """Make the sentinel known to the in-memory guard (as a live credential would be)."""
    secret_guard.register(sentinel.value, "test")
    return sentinel


def guard_is_clean():
    """Snapshot of the guard size, so a test can assert the registry is not persisted."""
    return secret_guard.known_secret_count()
