"""End-to-end check of the reported case: a paper whose PDF was deleted by hand.

Creates a small library with one orphan record, starts a real server, then over real HTTP:
  1. reads /api/state and expects source_missing=true for the orphan (and false for the other),
  2. serves the page and checks the UI hooks (row delete control, AI progress bar, versioned assets),
  3. deletes the orphan through /api/delete and confirms the library is clean,
  4. confirms the other paper and its file are untouched.
"""
import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import http.cookiejar
from pathlib import Path

LAB = Path(r"C:\Users\ASUS\OneDrive\Desktop\论文研读助手\论文研读助手-历史版本\开发工作区（原 D-harness home）\pr-lab")
PY = LAB / "runtime" / "python.exe"
PORT = 8795
ORPHAN = "aaaaaaaa-1111-2222-3333-444444444444"
KEPT = "bbbbbbbb-1111-2222-3333-444444444444"
report = {}

sys.path.insert(0, str(LAB))
import reading_card  # noqa: E402
import schema        # noqa: E402
import server        # noqa: E402

data_dir = Path(tempfile.mkdtemp(prefix="pr-orphan-"))
store = server.Store(data_dir)


def paper(paper_id, title):
    return {"id": paper_id, "title": title, "authors": "", "year": "", "venue": "", "doi": "",
            "citation": "", "tags": "", "language": "en", "type": "empirical",
            "fields": {key: "" for key in reading_card.FIELD_KEYS},
            "steps": [False] * 4, "annotations": [], "pageOffset": 0, "pageLabels": [],
            "lastPage": 1, "revision": 0, "created": "2026-10-13T00:00:00Z",
            "updated": "2026-10-13T00:00:00Z", "archived": False, "note_name": "x.md",
            "analysis": schema.default_analysis()}


store.state["papers"].append(paper(ORPHAN, "orphan-record"))
store.state["papers"].append(paper(KEPT, "kept-paper"))
store.persist()
(store.files.papers / f"{KEPT}.pdf").write_bytes(b"%PDF-1.4 fixture")

process = subprocess.Popen(
    [str(PY), str(LAB / "server.py"), "--port", str(PORT), "--data-dir", str(data_dir),
     "--no-worker"],
    cwd=str(LAB), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
base = f"http://127.0.0.1:{PORT}"
for _ in range(60):
    time.sleep(0.4)
    try:
        with urllib.request.urlopen(f"{base}/api/health", timeout=2) as response:
            json.loads(response.read().decode())
        break
    except Exception:                                            # noqa: BLE001
        continue

info = json.loads((LAB / "run" / "server.json").read_text(encoding="utf-8"))
import launcher_pipe  # noqa: E402

capability, error = launcher_pipe.request_bootstrap(str(info.get("pipe") or ""))
jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def call(path, payload=None, headers=None):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(base + path, data=data,
                                     method="POST" if data is not None else "GET")
    request.add_header("Content-Type", "application/json")
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    try:
        with opener.open(request, timeout=20) as response:
            body = response.read().decode("utf-8", "replace")
            return response.status, (json.loads(body) if body.strip().startswith(("{", "[")) else body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, body[:200]


status, body = call("/api/session/bootstrap", {"capability": capability})
csrf = str(body.get("csrf") or "") if isinstance(body, dict) else ""
report["session"] = status

status, state = call("/api/state")
flags = {p["id"]: p.get("source_missing") for p in state["papers"]}
report["source_missing_flags"] = {("orphan" if k == ORPHAN else "kept"): v for k, v in flags.items()}

status, page = call("/")
report["index_status"] = status
report["assets_versioned"] = "?v=" in page and "__VERSION__" not in page
report["ui_has_delete_dialog"] = "delete-dialog" in page
status, script = call("/static/app.js")
report["ui_has_row_delete"] = "data-delete-paper" in script
report["ui_has_ai_progress"] = "ai-progress-fill" in script
report["ui_reads_source_missing"] = "source_missing" in script

status, body = call("/api/delete", {"paper_id": ORPHAN}, {"X-CSRF-Token": csrf})
report["delete_status"] = status
report["delete_result"] = body.get("deleted") if isinstance(body, dict) else body
status, state = call("/api/state")
report["remaining_papers"] = [p["title"] for p in state["papers"]]
report["kept_pdf_still_there"] = (data_dir / "papers" / f"{KEPT}.pdf").is_file()

process.terminate()
try:
    process.wait(timeout=10)
except subprocess.TimeoutExpired:
    process.kill()
report["stopped"] = process.poll() is not None
shutil.rmtree(data_dir, ignore_errors=True)
print(json.dumps(report, ensure_ascii=False, indent=2))
