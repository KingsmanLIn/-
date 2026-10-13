"""Real end-to-end check of figure reading: one paid call, bounded.

Copies the lab library, starts a server that can see PAPER_READER_KEY, switches vision on with a
two-page image budget, forces one analysis, and reports what came back: which pages were attached
as images and whether the answers cite figure/table captions.

Run:  D:\\harness home\\pr-lab\\runtime\\python.exe D:\\harness home\\pr-lab\\tests\\live_vision_check.py
"""
import http.cookiejar
import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import winreg
from pathlib import Path

LAB = Path(r"C:\Users\ASUS\OneDrive\Desktop\论文研读助手\论文研读助手-历史版本\开发工作区（原 D-harness home）\pr-lab")
PY = LAB / "runtime" / "python.exe"
PORT = 8798
PAPER = "9edf1d4b-561b-47a2-860e-9119d865f54b"
sys.path.insert(0, str(LAB))
import launcher_pipe  # noqa: E402

report = {}
data_dir = Path(tempfile.mkdtemp(prefix="pr-live-vision-"))
for folder in ("papers", "extracted", "analyses"):
    source = LAB / "data" / folder
    if source.is_dir():
        shutil.copytree(source, data_dir / folder)
shutil.copy2(LAB / "data" / "library.json", data_dir / "library.json")

environment = dict(**{key: value for key, value in __import__("os").environ.items()})
with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as handle:
    environment["PAPER_READER_KEY"] = winreg.QueryValueEx(handle, "PAPER_READER_KEY")[0]
environment["PYTHONDONTWRITEBYTECODE"] = "1"

process = subprocess.Popen(
    [str(PY), str(LAB / "server.py"), "--port", str(PORT), "--data-dir", str(data_dir)],
    cwd=str(LAB), env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
base = f"http://127.0.0.1:{PORT}"
for _ in range(80):
    time.sleep(0.4)
    try:
        with urllib.request.urlopen(f"{base}/api/health", timeout=2) as response:
            json.loads(response.read().decode())
        break
    except Exception:                                            # noqa: BLE001
        continue

instance = json.loads((data_dir / "run-instance.json").read_text(encoding="utf-8"))
capability, _error = launcher_pipe.request_bootstrap(instance.get("pipe") or "")
jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def call(path, payload=None, headers=None, timeout=40):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(base + path, data=data,
                                     method="POST" if data is not None else "GET")
    request.add_header("Content-Type", "application/json")
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
            return response.status, (json.loads(body) if body.strip().startswith(("{", "[")) else body)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")[:300]


_status, session = call("/api/session/bootstrap", {"capability": capability})
csrf = str(session.get("csrf") or "")

settings = {"provider": "openai_compatible", "mode": "cloud_https",
            "base_url": "https://api.deepseek.com", "model": "deepseek-flash",
            "api_key_env": "env:PAPER_READER_KEY", "auto_analyze": False,
            "vision": True, "max_figure_pages": 2, "max_output_tokens": 32768}
_status, saved = call("/api/ai-settings", {"ai": settings}, {"X-CSRF-Token": csrf})
report["settings"] = {key: saved.get("ai", {}).get(key)
                      for key in ("provider", "model", "vision", "max_figure_pages")} \
    if isinstance(saved, dict) else saved
report["health"] = saved.get("health", {}).get("ok") if isinstance(saved, dict) else None

_status, started = call(f"/api/analysis/{PAPER}/generate", {"force": True},
                        {"X-CSRF-Token": csrf})
report["generate"] = started if isinstance(started, dict) else str(started)[:200]

deadline = time.time() + 900
seen = []
while time.time() < deadline:
    time.sleep(4)
    _status, state = call("/api/state", timeout=30)
    paper = next((item for item in state.get("papers", []) if item["id"] == PAPER), None)
    if paper is None:
        break
    meta = paper.get("analysis", {})
    step = (meta.get("status"), meta.get("stage"), meta.get("progress"), meta.get("message"))
    if not seen or seen[-1] != step:
        seen.append(step)
        print(f"  {meta.get('status'):<16} {meta.get('progress') or 0:>3}%  "
              f"{meta.get('message', '')[:96]}", flush=True)
    if meta.get("status") in ("ready", "failed", "cancelled", "waiting_for_user"):
        break
report["transitions"] = [list(step) for step in seen]
report["final_status"] = seen[-1][0] if seen else ""

card, error = None, None
answer_file = sorted((data_dir / "analyses").glob(f"{PAPER}*.json"))
if answer_file:
    card = json.loads(answer_file[0].read_text(encoding="utf-8"))
    report["answer_file"] = answer_file[0].name
if card:
    block = card.get("input", {})
    report["figure_pages_sent"] = block.get("figure_pages")
    report["input_note"] = block.get("note")
    report["passes"] = block.get("passes")
    quotes = [item["quote"] for answer in (card.get("answers") or {}).values()
              for item in (answer.get("evidence") or []) if item.get("quote")]
    caption_quotes = [quote for quote in quotes
                      if any(marker in quote for marker in
                             ("Figure", "Table", "Fig.", "Panel", "Chart", "图 ", "表 "))]
    report["evidence_quotes"] = len(quotes)
    report["caption_quotes"] = len(caption_quotes)
    report["caption_samples"] = caption_quotes[:5]
    report["fields_found"] = sum(1 for answer in (card.get("answers") or {}).values()
                                 if answer.get("status") == "found")

call("/api/shutdown", {}, {"X-CSRF-Token": csrf})
time.sleep(1.5)
if process.poll() is None:
    process.terminate()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
report["stopped"] = process.poll() is not None
# Keep the evidence when the run fails: the failed-answer file carries the coded problems.
failed = sorted((data_dir / "analyses").glob(f"{PAPER}*.failed.json"))
if failed:
    record = json.loads(failed[0].read_text(encoding="utf-8"))
    report["failure_file"] = failed[0].name
    report["failure_errors"] = record.get("errors")
log_path = LAB / "run" / "startup.log"
if log_path.is_file():
    report["log_tail"] = [line for line in log_path.read_text(encoding="utf-8",
                                                           errors="replace").splitlines()[-40:]
                          if any(marker in line for marker in
                                 ("figures_attached", "figures_skipped", "figures_dropped",
                                  "validation_failed", "repair_call", "provider_call"))][-8:]
print(json.dumps(report, ensure_ascii=False, indent=2))
if not report.get("final_status") == "ready":
    print(f"artifacts kept in {data_dir}")
else:
    shutil.rmtree(data_dir, ignore_errors=True)
