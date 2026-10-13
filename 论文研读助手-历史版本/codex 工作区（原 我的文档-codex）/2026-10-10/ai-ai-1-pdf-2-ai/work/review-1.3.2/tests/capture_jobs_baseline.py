"""Phase 3 end-to-end evidence: import returns fast, the worker does the slow work.

Runs against an isolated copy of the library with a FakeProvider (no real model).
Writes tests/phase3-jobs-baseline.json.

    python tests/capture_jobs_baseline.py
"""
from __future__ import annotations

import json
import shutil
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from baseline_helpers import fresh_library_v1  # noqa: E402
import analysis      # noqa: E402
import providers     # noqa: E402
import server        # noqa: E402

report = {"provider": "fake", "steps": []}
tmp = Path(tempfile.mkdtemp(prefix="phase3-"))
fresh_library_v1(tmp)
store = server.Store(tmp)
store.update_ai_settings({"provider": "ollama", "model": "fake-model", "auto_analyze": True})

events = []
fake = providers.FakeProvider(
    script=["success", "success", "success"],
    payload={"fields": {key: {"page": 1, "quote": f"引文 {key}", "text": f"参考 {key}"}
                        for key in analysis.FIELD_KEYS}},
    delay=1.2)                     # a slow model: the import must not wait for it
manager = analysis.AnalysisManager(store, provider_factory=lambda settings: fake,
                                   log=lambda record: events.append(record))

with socket.socket() as probe:
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
httpd = server.ThreadingHTTPServer(("127.0.0.1", port), server.handler_for(store, "tok", manager))
httpd.daemon_threads = True
threading.Thread(target=httpd.serve_forever, daemon=True).start()
manager.start()

blob = (ROOT / "data" / "papers" / "finance-2021-zhang.pdf").read_bytes()
started = time.time()
request = Request(f"http://127.0.0.1:{port}/api/import", data=blob, method="POST",
                  headers={"X-Reader-Token": "tok", "X-File-Name": "phase3-demo.pdf"})
with urlopen(request, timeout=60) as response:
    paper = json.loads(response.read().decode("utf-8"))["paper"]
import_seconds = time.time() - started
report["import_seconds"] = round(import_seconds, 3)
report["import_returned_status"] = paper["analysis"]["status"]
report["provider_calls_at_import"] = len(fake.calls)
report["steps"].append({"at": "import", "status": paper["analysis"]["status"],
                        "provider_calls": len(fake.calls)})

paper_id = paper["id"]
deadline = time.time() + 120
while time.time() < deadline:
    meta = store.analysis_meta(paper_id)
    if meta["status"] in ("ready", "failed", "waiting_for_provider"):
        break
    time.sleep(0.05)
meta = store.analysis_meta(paper_id)
report["final_status"] = meta["status"]
report["final_stage"] = meta["stage"]
report["final_progress"] = meta["progress"]
report["duration_ms"] = meta["duration_ms"]
report["provider_calls_total"] = len(fake.calls)
report["answer_file"] = meta["answer_file"]
report["error_code"] = meta["error_code"]
answer, answer_error = store.analysis_answer(paper_id)
report["answer_fields"] = answer["field_count"] if answer else None
report["answer_error"] = answer_error
report["transitions"] = [{"status": e.get("status"), "stage": e.get("stage"),
                          "progress": e.get("progress")}
                         for e in events if e.get("event") == "transition"]
report["provider_call_events"] = [{"provider": e.get("provider"), "model": e.get("model"),
                                   "duration_ms": e.get("duration_ms"),
                                   "category": e.get("category")}
                                  for e in events if e.get("event") == "provider_call"]

# every state change must be on disk
on_disk = json.loads((tmp / "library.json").read_text(encoding="utf-8"))
report["persisted_status"] = next(p for p in on_disk["papers"] if p["id"] == paper_id)["analysis"]["status"]
report["analyses_dir"] = sorted(p.name for p in (tmp / "analyses").iterdir())
report["_meta"] = {"capturedBy": "tests/capture_jobs_baseline.py",
                   "note": "FakeProvider only; no model was contacted. The library is a "
                           "temporary copy and is deleted after capture."}

manager.stop()
httpd.shutdown()
shutil.rmtree(tmp, ignore_errors=True)
out = ROOT / "tests" / "phase3-jobs-baseline.json"
out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(report, ensure_ascii=False, indent=2))
print("written:", out)
