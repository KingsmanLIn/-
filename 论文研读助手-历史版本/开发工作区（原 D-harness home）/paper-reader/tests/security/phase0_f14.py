"""Phase 0 proof for F14, corrected: the paper is in `waiting_for_user` after import.

The first attempt used a library whose paper was still `none`, so the guard did not apply.
This version reproduces the real sequence: import with a provider configured and
auto_analyze off -> paper.analysis.status == waiting_for_user -> user clicks the button.

    python tests/security/phase0_f14.py
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(r"D:\harness home\paper-reader")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import analysis      # noqa: E402
import providers     # noqa: E402
import schema        # noqa: E402
import server        # noqa: E402
from baseline_helpers import fresh_library_v1  # noqa: E402

PAPER = "medical-2026-zang"
report = {}

tmp = Path(tempfile.mkdtemp(prefix="phase0-f14b-"))
fresh_library_v1(tmp)
store = server.Store(tmp)
store.update_ai_settings({"provider": "ollama", "model": "stub-model", "auto_analyze": False})
fake = providers.FakeProvider(script=["success"], payload={"schema_version": 1, "overall": {},
                                                           "answers": {}, "terms": [],
                                                           "reader_questions": [], "warnings": []})
manager = analysis.AnalysisManager(store, provider_factory=lambda settings: fake,
                                   log=lambda record: None)

# --- the state a freshly imported paper really has --------------------------------
state = json.loads((tmp / "library.json").read_text(encoding="utf-8"))
for item in state["papers"]:
    if item["id"] == PAPER:
        item.setdefault("analysis", schema.default_analysis()).update({
            "status": "waiting_for_user", "message": "点击栏目右侧按钮查看 AI 参考"})
(tmp / "library.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
store = server.Store(tmp)
manager = analysis.AnalysisManager(store, provider_factory=lambda settings: fake,
                                   log=lambda record: None)
report["precondition"] = {"status": store.analysis_meta(PAPER)["status"], "provider": "ollama",
                          "auto_analyze": False}

# --- what the UI sends: POST /generate with an empty body -------------------------
body = {}
force = bool(body.get("force", False))
report["ui_click_maps_to"] = {"body": body, "force": force}

# the shipped handler reads force from the body, then calls enqueue
outcome = manager.enqueue(PAPER, reason="force" if force else "retry", force=force)
report["enqueue_retry_force_false"] = outcome
report["status_after_click"] = store.analysis_meta(PAPER)["status"]
report["message_after_click"] = store.analysis_meta(PAPER)["message"]
report["provider_calls_after_click"] = len(fake.calls)

# --- draining the queue proves no request is made ---------------------------------
manager.process_once()
report["status_after_drain"] = store.analysis_meta(PAPER)["status"]
report["provider_calls_after_drain"] = len(fake.calls)

# --- any other status enqueues fine; only waiting_for_user is stuck ---------------
store.update_ai_settings({"provider": "ollama"})
store.mark_analysis_status(PAPER, "none") if hasattr(store, "mark_analysis_status") else None
state = json.loads((tmp / "library.json").read_text(encoding="utf-8"))
for item in state["papers"]:
    if item["id"] == PAPER:
        item["analysis"]["status"] = "none"
(tmp / "library.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
store = server.Store(tmp)
manager = analysis.AnalysisManager(store, provider_factory=lambda settings: fake,
                                   log=lambda record: None)
report["enqueue_from_none"] = manager.enqueue(PAPER, reason="retry", force=False)
report["provider_calls_after_none"] = len(fake.calls)

report["guard_lines"] = [f"{i}: {line.strip()}"
                         for i, line in enumerate(
                             (ROOT / "analysis.py").read_text(encoding="utf-8").splitlines(), 1)
                         if "waiting_for_user" in line]
report["ui_call"] = [f"{i}: {line.strip()[:130]}"
                     for i, line in enumerate(
                         (ROOT / "static" / "app.js").read_text(encoding="utf-8").splitlines(), 1)
                     if "/generate'" in line]

shutil.rmtree(tmp, ignore_errors=True)
print(json.dumps(report, ensure_ascii=False, indent=2))
(Path(__file__).resolve().parent / "phase0-f14.json").write_text(
    json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
