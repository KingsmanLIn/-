"""Audit F: recovery idempotence, secret handling through a real HTTP error, log shape.

    python tests/audit_safety_http.py
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from baseline_helpers import free_port, fresh_library_v1  # noqa: E402
import analysis      # noqa: E402
import providers     # noqa: E402
import schema        # noqa: E402
import server        # noqa: E402
from card_fixtures import build_card                     # noqa: E402

PAPER = "medical-2026-zang"
SECRET = "sk-live-audit-9f8e7d6c5b4a3210"
report = {}


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


class EchoStub(BaseHTTPRequestHandler):
    """Answers 401 and echoes a body that contains the key, exactly as a proxy might."""

    seen = []

    def log_message(self, *args):
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        EchoStub.seen.append({"path": self.path, "auth": self.headers.get("Authorization", ""),
                              "body": body})
        payload = json.dumps({
            "error": "invalid api key",
            "authorization": self.headers.get("Authorization", ""),
            "hint": f"configured with key {SECRET}",
        }).encode()
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def env(provider="ollama", model="audit-model", auto=True, base_url="", env_name=""):
    tmp = Path(tempfile.mkdtemp(prefix="audit-f-"))
    fresh_library_v1(tmp)
    store = server.Store(tmp)
    store.update_ai_settings({"provider": provider, "model": model, "auto_analyze": auto,
                              "base_url": base_url, "api_key_env": env_name})
    return tmp, store


# --- 1. recover with an unchanged model must not re-queue a ready paper -------------
tmp, store = env(model="audit-model")
fake = providers.FakeProvider(script=["success"], payload={})
fake.payload_for = lambda request, payload: build_card(pages_from_prompt(request.user))
manager = analysis.AnalysisManager(store, provider_factory=lambda s: fake, log=lambda r: None)
manager.enqueue(PAPER, reason="manual", force=True)
manager.process_once()
report["run"] = {"status": store.analysis_meta(PAPER)["status"], "model": store.analysis_meta(PAPER)["model"],
                 "calls": len(fake.calls)}
store2 = server.Store(tmp)
manager2 = analysis.AnalysisManager(store2, provider_factory=lambda s: fake, log=lambda r: None)
outcome = manager2.recover()
report["recover_same_model"] = {"requeued": outcome["requeued"], "stale": outcome["stale"],
                                "status": store2.analysis_meta(PAPER)["status"],
                                "calls": len(fake.calls)}
# and after a clear of the answer file, the ready record must become stale, not silently ready
store2.files.delete_analysis(PAPER)
store3 = server.Store(tmp)
manager3 = analysis.AnalysisManager(store3, provider_factory=lambda s: fake, log=lambda r: None)
manager3.recover()
report["recover_without_answer_file"] = {
    "status": store3.analysis_meta(PAPER)["status"],
    "answer_file": store3.analysis_meta(PAPER)["answer_file"],
    "has_answer_file": store3.files.has_analysis(PAPER),
    "calls": len(fake.calls),
}
shutil.rmtree(tmp, ignore_errors=True)

# --- 2. a 401 whose body echoes the key ---------------------------------------------
stub = ThreadingHTTPServer(("127.0.0.1", 0), EchoStub)
stub.daemon_threads = True
threading.Thread(target=stub.serve_forever, daemon=True).start()
port = stub.server_address[1]
os.environ["AUDIT_LLM_KEY"] = SECRET
try:
    tmp, store = env(provider="openai_compatible", model="audit-model", auto=True,
                     base_url=f"http://127.0.0.1:{port}/v1", env_name="AUDIT_LLM_KEY")
    events = []
    manager = analysis.AnalysisManager(store, provider_factory=None, log=events.append)
    manager.enqueue(PAPER, reason="manual", force=True)
    manager.process_once()
    meta = store.analysis_meta(PAPER)
    library_text = (tmp / "library.json").read_text(encoding="utf-8")
    log_text = json.dumps(events, ensure_ascii=False)
    blob = store.backup()
    backed_up = {}
    with zipfile.ZipFile(BytesIO(blob)) as archive:
        for name in archive.namelist():
            if SECRET in archive.read(name).decode("utf-8", "replace"):
                backed_up[name] = True
    report["auth_failure"] = {
        "status": meta["status"], "error_code": meta["error_code"],
        "error_message": meta["error_message"],
        "auth_header_sent": bool(EchoStub.seen and EchoStub.seen[0]["auth"]),
        "secret_in_library": SECRET in library_text,
        "secret_in_error_message": SECRET in meta["error_message"],
        "redacted_marker_in_error_message": "[redacted]" in meta["error_message"],
        "secret_in_event_log": SECRET in log_text,
        "secret_in_backup": bool(backed_up),
        "backup_members_checked": len(backed_up) == 0,
        "authorization_value_in_event_log": any("Authorization" in json.dumps(e) for e in events),
        "event_keys": sorted({key for event in events for key in event}),
        "events": events,
    }
    shutil.rmtree(tmp, ignore_errors=True)
finally:
    os.environ.pop("AUDIT_LLM_KEY", None)
    stub.shutdown()

# --- 3. what does the served HTML / state expose? -----------------------------------
tmp, store = env(provider="ollama", model="audit-model")
(store.directory / "credentials.json").write_text(json.dumps({"AUDIT_LLM_KEY": SECRET}), encoding="utf-8")
state = json.dumps(store.snapshot(), ensure_ascii=False)
report["state_payload"] = {
    "secret_in_state": SECRET in state,
    "credentials_file_outside_library": "credentials" not in state,
    "top_level_keys": sorted(store.snapshot()),
}
blob = store.backup()
with zipfile.ZipFile(BytesIO(blob)) as archive:
    names = archive.namelist()
    report["backup"] = {
        "members": names,
        "has_credentials": any("credential" in name for name in names),
        "secret_found": any(SECRET in archive.read(name).decode("utf-8", "replace")
                            for name in names),
    }
shutil.rmtree(tmp, ignore_errors=True)

print(json.dumps(report, ensure_ascii=False, indent=2))
