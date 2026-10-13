"""Audit C: does the real HTTP provider path work, independent of FakeProvider?

Starts a minimal Ollama-compatible and an OpenAI-compatible stub on loopback, points the
shipped adapters at them, and runs the real pipeline. This proves the queue → HTTP call →
parse → validate → save chain works with the actual adapter code (no local model needed).

    python tests/audit_provider_http.py
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from baseline_helpers import free_port, fresh_library_v1  # noqa: E402
import analysis      # noqa: E402
import extraction    # noqa: E402
import providers     # noqa: E402
import reading_card  # noqa: E402
import server        # noqa: E402
from card_fixtures import build_card  # noqa: E402
from storage import DataStore         # noqa: E402

PAPER = "medical-2026-zang"
report = {}

# The card the stub model returns: built from the same page text the real prompt carries,
# so every quote is genuine (this is what a competent local model would have to do).
CARD = None


def pages_from_prompt(prompt):
    class Page:
        __slots__ = ("pdf_page", "text", "char_count", "backend")

        def __init__(self, number, text):
            self.pdf_page = number
            self.text = text
            self.char_count = len(text)
            self.backend = "prompt"

    pages = []
    blocks = re.split(r"PDF 第 (\d+)-(\d+) 页", prompt or "")
    for index in range(1, len(blocks) - 2, 3):
        text = blocks[index + 2]
        if len(text.strip()) >= 20:
            pages.append(Page(int(blocks[index]), text))
    return pages


class Stub(BaseHTTPRequestHandler):
    """Records the request and answers with whatever `style` asks for."""

    style = "ollama"
    outcome = "ok"
    seen = []

    def log_message(self, *args):        # keep the audit output clean
        return

    def _read(self):
        length = int(self.headers.get("Content-Length", "0"))
        return self.rfile.read(length).decode("utf-8", "replace") if length else ""

    def _send(self, payload, status=200):
        blob = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def do_GET(self):
        Stub.seen.append({"method": "GET", "path": self.path})
        if self.path.endswith("/api/tags"):
            self._send({"models": [{"name": "audit-model"}]})
        elif self.path.endswith("/models"):
            self._send({"data": [{"id": "audit-model"}]})
        else:
            self._send({"error": "unknown"}, 404)

    def do_POST(self):
        body = self._read()
        Stub.seen.append({"method": "POST", "path": self.path, "body": body[:400000],
                          "auth": self.headers.get("Authorization", "")})
        if Stub.outcome == "http_error":
            self._send({"error": "model crashed"}, 500)
            return
        if Stub.outcome == "bad_json":
            self._send({"message": {"content": "I cannot answer in JSON, sorry."}}) \
                if Stub.style == "ollama" else \
                self._send({"choices": [{"message": {"content": "not json at all"}}]})
            return
        # build a card from the prompt this request actually carried
        prompt = body
        try:
            parsed = json.loads(body)
            if Stub.style == "ollama":
                prompt = "".join(m.get("content", "") for m in parsed.get("messages", []))
            else:
                prompt = "".join(m.get("content", "") for m in parsed.get("messages", []))
        except json.JSONDecodeError:
            pass
        card = build_card(pages_from_prompt(prompt))
        content = json.dumps(card, ensure_ascii=False)
        if Stub.style == "ollama":
            self._send({"model": "audit-model", "done": True,
                        "message": {"role": "assistant", "content": content}})
        else:
            self._send({"model": "audit-model",
                        "choices": [{"index": 0, "finish_reason": "stop",
                                     "message": {"role": "assistant", "content": content}}],
                        "usage": {"prompt_tokens": 100, "completion_tokens": 200}})


def start_stub(style, outcome="ok"):
    Stub.style, Stub.outcome, Stub.seen = style, outcome, []
    port = free_port()
    server_ = ThreadingHTTPServer(("127.0.0.1", port), Stub)
    server_.daemon_threads = True
    threading.Thread(target=server_.serve_forever, daemon=True).start()
    return server_, port


def run_pipeline(provider, base_url, outcome="ok", style="ollama"):
    stub, port = start_stub(style, outcome)
    try:
        tmp = Path(tempfile.mkdtemp(prefix="audit-c-"))
        fresh_library_v1(tmp)
        store = server.Store(tmp)
        store.update_ai_settings({"provider": provider, "model": "audit-model",
                                  "base_url": f"http://127.0.0.1:{port}"
                                  + ("/v1" if style == "openai" else ""),
                                  "auto_analyze": True})
        events = []
        manager = analysis.AnalysisManager(store, provider_factory=None, log=events.append)
        manager.recover()
        manager.enqueue(PAPER, reason="manual", force=True)
        manager.process_once()
        meta = store.analysis_meta(PAPER)
        answer, error = store.analysis_answer(PAPER)
        result = {"provider": provider, "outcome": outcome, "status": meta["status"],
                  "error_code": meta["error_code"], "error_message": meta["error_message"],
                  "requests": [{"method": r["method"], "path": r["path"],
                                "auth": bool(r.get("auth"))} for r in Stub.seen],
                  "answer": {"fields": len(answer["answers"]) if answer else 0,
                             "verified": answer["verification"]["verified_quotes"] if answer else 0},
                  "answer_error": error or "",
                  "events": [e.get("event") for e in events]}
        shutil.rmtree(tmp, ignore_errors=True)
        return result
    finally:
        stub.shutdown()


report["ollama_ok"] = run_pipeline("ollama", "", style="ollama")
report["openai_compatible_ok"] = run_pipeline("openai_compatible", "", style="openai")
report["ollama_http_500"] = run_pipeline("ollama", "", outcome="http_error", style="ollama")
report["ollama_bad_json"] = run_pipeline("ollama", "", outcome="bad_json", style="ollama")

# what did the outgoing request actually look like? (first Ollama call)
stub, port = start_stub("ollama")
try:
    tmp = Path(tempfile.mkdtemp(prefix="audit-c2-"))
    fresh_library_v1(tmp)
    store = server.Store(tmp)
    store.update_ai_settings({"provider": "ollama", "model": "audit-model",
                              "base_url": f"http://127.0.0.1:{port}", "auto_analyze": False})
    manager = analysis.AnalysisManager(store, provider_factory=None, log=lambda r: None)
    manager.enqueue(PAPER, reason="manual", force=True)
    manager.process_once()
    request = json.loads(Stub.seen[0]["body"])
    report["outgoing_request"] = {
        "path": Stub.seen[0]["path"],
        "keys": sorted(request),
        "model": request.get("model"),
        "format": request.get("format"),
        "stream": request.get("stream"),
        "temperature": request.get("options", {}).get("temperature"),
        "system_prompt_chars": len(request.get("messages", [{}])[0].get("content", "")),
        "user_prompt_chars": len(request.get("messages", [{}, {}])[1].get("content", "")),
        "user_prompt_has_page_markers": "PDF 第" in request.get("messages", [{}, {}])[1].get("content", ""),
    }
    shutil.rmtree(tmp, ignore_errors=True)
finally:
    stub.shutdown()

print(json.dumps(report, ensure_ascii=False, indent=2))
