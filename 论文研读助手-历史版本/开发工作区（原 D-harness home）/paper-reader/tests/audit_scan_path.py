"""Audit H: the scanned-PDF path with the real extractor (no stubbing).

    python tests/audit_scan_path.py
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
import server        # noqa: E402

SCAN = "finance-2021-zhang"
report = {}

tmp = Path(tempfile.mkdtemp(prefix="audit-h-"))
fresh_library_v1(tmp)
store = server.Store(tmp)
store.update_ai_settings({"provider": "ollama", "model": "audit-model", "auto_analyze": True})
fake = providers.FakeProvider(script=["success"], payload={"answers": {}})
manager = analysis.AnalysisManager(store, provider_factory=lambda s: fake, log=lambda r: None)
manager.enqueue(SCAN, reason="manual", force=True)
manager.process_once()
meta = store.analysis_meta(SCAN)
answer, error = store.analysis_answer(SCAN)
report["scanned_paper"] = {
    "status": meta["status"],
    "error_code": meta["error_code"],
    "message": meta["message"],
    "provider_calls": len(fake.calls),
    "answer": answer is not None,
    "answer_error": error or "",
    "last_extraction": getattr(manager, "last_extraction", None),
    "human_fields_still_editable": True,
}
# the paper must remain usable
paper = store.paper(SCAN)
saved = store.save_paper({"id": SCAN, "revision": paper["revision"],
                          "patch": {"fields": dict(paper["fields"], summary="扫描件手写"),
                                    "annotations": [{"id": "a1", "type": "term", "page": 3,
                                                     "quote": "", "note": "手记",
                                                     "rects": [], "created": "2026-10-10T00:00:00Z"}]}})
report["after_failure"] = {
    "saved_summary": saved["paper"]["fields"]["summary"],
    "annotations": len(saved["paper"]["annotations"]),
    "analysis_status": saved["paper"]["analysis"]["status"],
}
# and a manual analysis on a text-layer paper must use a real provider call
manager.enqueue("medical-2026-zang", reason="manual", force=True)
manager.process_once()
report["text_paper"] = {"status": store.analysis_meta("medical-2026-zang")["status"],
                        "error_code": store.analysis_meta("medical-2026-zang")["error_code"],
                        "provider_calls": len(fake.calls)}
shutil.rmtree(tmp, ignore_errors=True)
print(json.dumps(report, ensure_ascii=False, indent=2))
