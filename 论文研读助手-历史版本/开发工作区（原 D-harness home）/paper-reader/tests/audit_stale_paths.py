"""Audit G: pinpoint who flags a missing answer file, and the same-model restart case.

    python tests/audit_stale_paths.py
"""
from __future__ import annotations

import json
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
import schema        # noqa: E402
import server        # noqa: E402
from card_fixtures import build_card             # noqa: E402

PAPER = "medical-2026-zang"
report = {}


def pages_from_prompt(prompt):
    import re

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


def setup(model="audit-model"):
    tmp = Path(tempfile.mkdtemp(prefix="audit-g-"))
    fresh_library_v1(tmp)
    store = server.Store(tmp)
    store.update_ai_settings({"provider": "ollama", "model": model, "auto_analyze": False})
    fake = providers.FakeProvider(script=["success"], model=model, payload={})
    fake.payload_for = lambda request, payload: build_card(pages_from_prompt(request.user))
    manager = analysis.AnalysisManager(store, provider_factory=lambda s: fake,
                                       log=lambda record: None)
    manager.enqueue(PAPER, reason="manual", force=True)
    manager.process_once()
    return tmp, store, fake


# --- same model name: restart must keep ready and not re-call the model -------------
tmp, store, fake = setup("audit-model")
report["first_run"] = {"status": store.analysis_meta(PAPER)["status"],
                       "model": store.analysis_meta(PAPER)["model"], "calls": len(fake.calls)}
store2 = server.Store(tmp)
manager2 = analysis.AnalysisManager(store2, provider_factory=lambda s: fake, log=lambda r: None)
outcome = manager2.recover()
report["restart_same_model"] = {"requeued": outcome["requeued"], "stale": outcome["stale"],
                                "status": store2.analysis_meta(PAPER)["status"],
                                "calls": len(fake.calls)}
shutil.rmtree(tmp, ignore_errors=True)

# --- answer file deleted: who notices? --------------------------------------------
tmp, store, fake = setup("audit-model")
store.files.delete_analysis(PAPER)
report["answer_file_deleted"] = {
    "metadata_status": store.analysis_meta(PAPER)["status"],
    "metadata_answer_file": store.analysis_meta(PAPER)["answer_file"],
    "answer_file_exists": store.files.has_analysis(PAPER),
    "is_stale_rule": schema.analysis_is_stale(store.analysis_meta(PAPER),
                                              source_sha256=store.analysis_meta(PAPER)["source_sha256"],
                                              prompt_version=analysis.PROMPT_VERSION,
                                              model="audit-model", language="zh"),
}
store3 = server.Store(tmp)
manager3 = analysis.AnalysisManager(store3, provider_factory=lambda s: fake, log=lambda r: None)
manager3.recover()
meta = store3.analysis_meta(PAPER)
report["answer_file_deleted_after_recover"] = {
    "status": meta["status"], "calls": len(fake.calls),
    "has_answer_file": store3.files.has_analysis(PAPER),
}
shutil.rmtree(tmp, ignore_errors=True)

# --- metadata says ready with a stale answer_file name ----------------------------
tmp, store, fake = setup("audit-model")
state = json.loads((tmp / "library.json").read_text(encoding="utf-8"))
for item in state["papers"]:
    if item["id"] == PAPER:
        item["analysis"]["answer_file"] = "ghost.json"
        (tmp / "analyses" / f"{PAPER}.json").unlink()
(tmp / "library.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
store4 = server.Store(tmp)
manager4 = analysis.AnalysisManager(store4, provider_factory=lambda s: fake, log=lambda r: None)
manager4.recover()
meta = store4.analysis_meta(PAPER)
report["library_points_at_missing_file"] = {
    "status": meta["status"], "answer_file": meta["answer_file"],
    "has_answer_file": store4.files.has_analysis(PAPER), "calls": len(fake.calls),
}
shutil.rmtree(tmp, ignore_errors=True)

print(json.dumps(report, ensure_ascii=False, indent=2))
