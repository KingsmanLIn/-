"""Screenshot the live install's page: their library, their settings.

Read-only: asks the running instance for a one-time bootstrap capability (as launch.py does),
opens it in headless Chrome, and captures the reading view plus the AI settings section scrolled
into view. Nothing is clicked that changes state.

Run:  D:\\harness home\\pr-lab\\runtime\\python.exe D:\\harness home\\pr-lab\\tests\\live_screenshot.py
"""
import base64
import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

LAB = Path(r"C:\Users\ASUS\OneDrive\Desktop\论文研读助手\论文研读助手-历史版本\开发工作区（原 D-harness home）\pr-lab")
LIVE = Path(r"C:\Users\ASUS\OneDrive\Desktop\论文研读助手\1.3.8版本\论文研读助手-本地版-1.3.8-完整便携版")
CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
SHOTS = LAB / "shots"
sys.path.insert(0, str(LAB))
sys.path.insert(0, str(LAB / "tests"))
import launcher_pipe                      # noqa: E402
from ui_layout_check import Client        # noqa: E402


def main():
    SHOTS.mkdir(exist_ok=True)
    instance = json.loads((LIVE / "run" / "server.json").read_text(encoding="utf-8"))
    base = f"http://127.0.0.1:{instance['port']}"
    capability, error = launcher_pipe.request_bootstrap(instance.get("pipe") or "")
    report = {"port": instance["port"], "bootstrap_ok": bool(capability) and not error}
    if not capability:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    profile = Path(tempfile.mkdtemp(prefix="live-shots-"))
    browser = None
    debug_port = 9335

    def shoot(client, name, width, height):
        client.command("Emulation.setDeviceMetricsOverride",
                       {"width": width, "height": height, "deviceScaleFactor": 1,
                        "mobile": False})
        time.sleep(1.6)
        shot = client.command("Page.captureScreenshot", {"format": "png"})
        blob = shot.get("result", {}).get("data")
        if not blob:
            report[name] = "capture failed"
            return
        path = SHOTS / f"{name}.png"
        path.write_bytes(base64.b64decode(blob))
        report[name] = {"file": str(path), "kb": round(path.stat().st_size / 1024, 1)}

    try:
        browser = subprocess.Popen([str(CHROME), "--headless=new", "--disable-gpu",
                                    "--no-first-run", "--no-default-browser-check",
                                    "--disable-extensions", "--remote-allow-origins=*",
                                    f"--remote-debugging-port={debug_port}",
                                    f"--user-data-dir={profile}", "about:blank"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        page_url = f"{base}/#bootstrap={capability}"
        target = None
        for _ in range(60):
            time.sleep(0.25)
            try:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{debug_port}/json/new?{urllib.parse.quote(page_url, safe='')}",
                    method="PUT")
                with urllib.request.urlopen(request, timeout=5) as response:
                    target = json.loads(response.read().decode())
                break
            except Exception:                                    # noqa: BLE001
                continue
        if not target:
            report["error"] = "chrome target never appeared"
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return
        client = Client(target["webSocketDebuggerUrl"])
        # Set the viewport *before* the app lays out, so the capture matches a real wide window
        # (the page keeps whatever ratio its first layout produced otherwise).
        client.command("Emulation.setDeviceMetricsOverride",
                       {"width": 1500, "height": 950, "deviceScaleFactor": 1, "mobile": False})
        client.command("Page.reload")
        time.sleep(5.0)
        report["title"] = client.command("Runtime.evaluate", {
            "expression": "document.querySelector('#paper-title').textContent",
            "returnByValue": True}).get("result", {}).get("result", {}).get("value")
        shoot(client, "live-reader", 1500, 950)
        client.command("Runtime.evaluate",
                       {"expression": "document.querySelector('#toggle-sidebar').click()"})
        time.sleep(1.2)
        shoot(client, "live-sidebar-collapsed", 1500, 950)
        client.command("Runtime.evaluate",
                       {"expression": "document.querySelector('#toggle-sidebar').click()"})
        time.sleep(0.8)
        client.command("Runtime.evaluate",
                       {"expression": "document.querySelector('#settings-button').click()"})
        time.sleep(1.2)
        client.command("Runtime.evaluate", {
            "expression": "document.querySelector('#ai-vision')"
                          ".scrollIntoView({block:'center'})"})
        time.sleep(1.0)
        shoot(client, "live-settings-ai", 1500, 950)
        report["vision_checked"] = client.command("Runtime.evaluate", {
            "expression": "document.querySelector('#ai-vision').checked",
            "returnByValue": True}).get("result", {}).get("result", {}).get("value")
        report["max_figures_value"] = client.command("Runtime.evaluate", {
            "expression": "document.querySelector('#ai-max-figures').value",
            "returnByValue": True}).get("result", {}).get("result", {}).get("value")
    finally:
        if browser is not None:
            browser.terminate()
        shutil.rmtree(profile, ignore_errors=True)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
