"""Measure the live install's page geometry (read-only) to see the actual split ratio."""
import json
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

LAB = Path(r"C:\Users\ASUS\OneDrive\Desktop\论文研读助手\论文研读助手-历史版本\开发工作区（原 D-harness home）\pr-lab")
LIVE = Path(r"C:\Users\ASUS\OneDrive\Desktop\论文研读助手\1.3.8版本\论文研读助手-本地版-1.3.8-完整便携版")
CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
sys.path.insert(0, str(LAB))
sys.path.insert(0, str(LAB / "tests"))
import launcher_pipe                      # noqa: E402
from ui_layout_check import Client        # noqa: E402

instance = json.loads((LIVE / "run" / "server.json").read_text(encoding="utf-8"))
base = f"http://127.0.0.1:{instance['port']}"
capability, _error = launcher_pipe.request_bootstrap(instance.get("pipe") or "")
profile = Path(r"C:\Users\ASUS\AppData\Local\Temp\live-measure-profile")
shutil.rmtree(profile, ignore_errors=True)
browser = subprocess.Popen([str(CHROME), "--headless=new", "--disable-gpu", "--no-first-run",
                            "--no-default-browser-check", "--disable-extensions",
                            "--remote-allow-origins=*", "--remote-debugging-port=9336",
                            f"--user-data-dir={profile}", "about:blank"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    page_url = f"{base}/#bootstrap={capability}"
    target = None
    for _ in range(60):
        time.sleep(0.25)
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:9336/json/new?{urllib.parse.quote(page_url, safe='')}",
                method="PUT")
            with urllib.request.urlopen(request, timeout=5) as response:
                target = json.loads(response.read().decode())
            break
        except Exception:                                        # noqa: BLE001
            continue
    client = Client(target["webSocketDebuggerUrl"])
    time.sleep(5.0)
    report = {}
    for width, height in ((1500, 950), (1280, 800)):
        client.command("Emulation.setDeviceMetricsOverride",
                       {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False})
        time.sleep(1.5)
        value = client.command("Runtime.evaluate", {"returnByValue": True, "expression": r"""
        (() => {
          const ws = document.querySelector('.workspace');
          const r = (sel) => { const el = document.querySelector(sel);
            if (!el) return null; const b = el.getBoundingClientRect();
            return {x: Math.round(b.x), w: Math.round(b.width)}; };
          return JSON.stringify({
            viewport: [innerWidth, innerHeight],
            cssVar: getComputedStyle(ws).getPropertyValue('--pdf-split').trim(),
            inlineVar: ws.style.getPropertyValue('--pdf-split'),
            tracks: getComputedStyle(ws).gridTemplateColumns,
            stored: localStorage.getItem('paper-reader:pdf-split'),
            pdf: r('.pdf-panel'), notes: r('.notes-panel'),
            splitter: r('#splitter'),
          });
        })()
        """}).get("result", {}).get("result", {}).get("value")
        report[f"{width}x{height}"] = json.loads(value)
    print(json.dumps(report, ensure_ascii=False, indent=2))
finally:
    browser.terminate()
    shutil.rmtree(profile, ignore_errors=True)
