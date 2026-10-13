"""Check the response headers, the asset references and the exclusive port binding.

Two instances are started from the same folder on the same requested port: the second must end
up on the next free port instead of sharing the first one's address.
"""
import json
import re
import shutil
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

LAB = Path(r"C:\Users\ASUS\OneDrive\Desktop\论文研读助手\论文研读助手-历史版本\开发工作区（原 D-harness home）\pr-lab")
PY = LAB / "runtime" / "python.exe"
report = {}


def start(data_dir, port):
    return subprocess.Popen(
        [str(PY), str(LAB / "server.py"), "--port", str(port), "--data-dir", str(data_dir),
         "--no-worker"],
        cwd=str(LAB), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def wait_ready(port, timeout=25):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as response:
                return json.loads(response.read().decode())
        except Exception:                                        # noqa: BLE001
            time.sleep(0.3)
    return None


first_dir = Path(tempfile.mkdtemp(prefix="hdr1-"))
second_dir = Path(tempfile.mkdtemp(prefix="hdr2-"))
first = start(first_dir, 8791)
report["first_health"] = wait_ready(8791)

report["headers"] = {}
for path in ("/", "/index.html", "/static/app.js", "/static/styles.css", "/vendor/pdfjs/pdf.mjs"):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:8791{path}", timeout=5) as response:
            report["headers"][path] = {"status": response.status,
                                       "cache": response.headers.get("Cache-Control")}
    except Exception as exc:                                     # noqa: BLE001
        report["headers"][path] = {"error": type(exc).__name__}

with urllib.request.urlopen("http://127.0.0.1:8791/", timeout=5) as response:
    page = response.read().decode("utf-8", "replace")
report["asset_refs"] = re.findall(r'(?:src|href)="[^"]*(?:app\.js|guides\.js|\.css)[^"]*"', page)
report["unreplaced_version_placeholder"] = "__VERSION__" in page
report["unreplaced_bootstrap_placeholder"] = "__BOOTSTRAP__" in page

second = start(second_dir, 8791)
report["second_health_on_8792"] = wait_ready(8792)
report["second_health_on_8791"] = None
try:
    with urllib.request.urlopen("http://127.0.0.1:8791/api/health", timeout=3) as response:
        report["second_health_on_8791"] = json.loads(response.read().decode())
except Exception:                                                # noqa: BLE001
    pass
report["recorded_port_after_second_start"] = json.loads(
    (LAB / "run" / "server.json").read_text(encoding="utf-8"))["port"]

for process in (first, second):
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
report["stopped"] = [process.poll() is not None for process in (first, second)]
shutil.rmtree(first_dir, ignore_errors=True)
shutil.rmtree(second_dir, ignore_errors=True)
print(json.dumps(report, ensure_ascii=False, indent=2))
