"""Audit B: AI safety - can a ready result be re-generated, and where can fields change?

Checks:
1. every place the front end writes into `paper.fields` / `steps` / `annotations`;
2. whether startup recovery re-queues a `ready` paper (which would re-call the model);
3. whether a missing provider is detected before an HTTP attempt;
4. what the event log and the library actually contain after a failed run.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from baseline_helpers import fresh_library_v1  # noqa: E402
import analysis      # noqa: E402
import providers     # noqa: E402
import server        # noqa: E402
from card_fixtures import build_card, pages_from_prompt  # noqa: E402

PAPER = "medical-2026-zang"
report = {}

# --- 1. write paths in the front end -----------------------------------------------
app = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
writes = []
for match in re.finditer(r"\.fields\[[^\]]+\]\s*=", app):
    line = app[:match.start()].count("\n") + 1
    context = app[max(0, match.start() - 260):match.start() + 80]
    writes.append({"line": line, "excerpt": re.sub(r"\s+", " ", context)[-220:]})
report["field_write_sites"] = writes
report["step_write_sites"] = [
    {"line": app[:m.start()].count("\n") + 1,
     "excerpt": re.sub(r"\s+", " ", app[max(0, m.start() - 120):m.start() + 60])}
    for m in re.finditer(r"\.steps\[[^\]]*\]\s*=", app)]
report["annotation_write_sites"] = [
    {"line": app[:m.start()].count("\n") + 1,
     "excerpt": re.sub(r"\s+", " ", app[max(0, m.start() - 120):m.start() + 60])}
    for m in re.finditer(r"annotations\.(push|splice)", app)]
report["read_paths_that_only_read"] = {
    "card_preview_uses_fields": "p.fields[k]||" in app,
    "ai_panel_is_separate": "aiPanelHTML(p,k)" in app,
}


def card_from_request(request, _payload):
    return build_card(pages_from_prompt(request.user))


def make_env(provider="ollama", auto=True):
    tmp = Path(tempfile.mkdtemp(prefix="audit-b-"))
    fresh_library_v1(tmp)
    store = server.Store(tmp)
    store.update_ai_settings({"provider": provider, "model": "audit-model",
                              "auto_analyze": auto})
    return tmp, store


# --- 2. ready + configured provider: does recovery re-queue? ------------------------
tmp, store = make_env()
fake = providers.FakeProvider(script=["success"], payload={})
fake.payload_for = card_from_request
manager = analysis.AnalysisManager(store, provider_factory=lambda s: fake, log=lambda r: None)
manager.enqueue(PAPER, reason="manual", force=True)
manager.process_once()
first_calls = len(fake.calls)
report["first_run"] = {"status": store.analysis_meta(PAPER)["status"], "calls": first_calls}

store2 = server.Store(tmp)
manager2 = analysis.AnalysisManager(store2, provider_factory=lambda s: fake, log=lambda r: None)
outcome = manager2.recover()
report["recover_after_ready"] = {
    "requeued": outcome["requeued"], "stale": outcome["stale"],
    "status": store2.analysis_meta(PAPER)["status"],
    "calls_after_recover": len(fake.calls),
}
shutil.rmtree(tmp, ignore_errors=True)

# --- 3. no provider: is the HTTP path avoided entirely? ------------------------------
tmp, store = make_env(provider="none")
real = providers.OllamaAdapter(base_url="", model="audit-model")
report["disabled_adapter_is_used"] = isinstance(
    analysis.AnalysisManager(store, provider_factory=None, log=lambda r: None)._effective_provider(
        store.ai_settings()), providers.DisabledAdapter)
manager3 = analysis.AnalysisManager(store, provider_factory=lambda s: real, log=lambda r: None)
manager3.enqueue(PAPER, reason="manual", force=True)
report["queue_without_provider"] = {
    "status": store.analysis_meta(PAPER)["status"],
    "queued": manager3.queue_depth(),
}
manager3.process_once()
report["run_without_provider"] = {
    "status": store.analysis_meta(PAPER)["status"],
    "error_code": store.analysis_meta(PAPER)["error_code"],
    "message": store.analysis_meta(PAPER)["message"],
}
shutil.rmtree(tmp, ignore_errors=True)

# --- 4. failure log and library content ---------------------------------------------
tmp, store = make_env()
events = []
fake = providers.FakeProvider(script=["invalid_json"], payload={},
                              raw_text='{"answers": {"summary": "泄漏测试：这是模型原文"}}')
manager4 = analysis.AnalysisManager(store, provider_factory=lambda s: fake, log=events.append)
manager4.enqueue(PAPER, reason="manual", force=True)
manager4.process_once()
meta = store.analysis_meta(PAPER)
report["failure"] = {
    "status": meta["status"], "error_code": meta["error_code"],
    "error_message": meta["error_message"],
    "message": meta["message"],
}
failed_file = store.files.failed_answer_path(PAPER)
record = json.loads(failed_file.read_text(encoding="utf-8")) if failed_file.is_file() else {}
library_text = (tmp / "library.json").read_text(encoding="utf-8")
report["failure_log"] = {
    "exists": failed_file.is_file(),
    "keys": sorted(record),
    "raw_contains_model_text": "泄漏测试" in record.get("raw_model_output", ""),
    "library_contains_model_text": "泄漏测试" in library_text,
}
report["event_keys"] = sorted({key for event in events for key in event})
report["event_log_contains_paper_text"] = any(
    "分级诊疗" in json.dumps(event, ensure_ascii=False) for event in events)
report["event_log"] = events
shutil.rmtree(tmp, ignore_errors=True)

print(json.dumps(report, ensure_ascii=False, indent=2))
