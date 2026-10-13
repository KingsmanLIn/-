"""Capture the served UI as PNGs so a human (or I) can look at it.

Uses the same headless-Chrome/CDP approach as ui_layout_check.py: real server, real page, real
fonts. Saves: reading view (wide), settings dialog, narrow view (stacked).

Run:  D:\\harness home\\pr-lab\\runtime\\python.exe D:\\harness home\\pr-lab\\tests\\ui_screenshots.py
"""
import base64
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

LAB = Path(r"C:\Users\ASUS\OneDrive\Desktop\论文研读助手\论文研读助手-历史版本\开发工作区（原 D-harness home）\pr-lab")
sys.path.insert(0, str(LAB))
sys.path.insert(0, str(LAB / "tests"))
import launcher_pipe                      # noqa: E402
from ui_layout_check import Client        # noqa: E402  (same minimal CDP client)

SHOTS = LAB / "shots"
CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
PORT = 8796


def main():
    import shutil
    import subprocess
    import tempfile

    SHOTS.mkdir(exist_ok=True)
    data_dir = Path(tempfile.mkdtemp(prefix="ui-shots-"))
    profile = Path(tempfile.mkdtemp(prefix="ui-shots-profile-"))
    server = subprocess.Popen([str(LAB / "runtime" / "python.exe"), str(LAB / "server.py"),
                               "--port", str(PORT), "--data-dir", str(data_dir), "--no-worker"],
                              cwd=str(LAB), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    browser = None
    report = {}
    try:
        base = f"http://127.0.0.1:{PORT}"
        for _ in range(60):
            time.sleep(0.4)
            try:
                with urllib.request.urlopen(base + "/api/health", timeout=2) as response:
                    json.loads(response.read().decode())
                break
            except Exception:                                    # noqa: BLE001
                continue
        instance = json.loads((data_dir / "run-instance.json").read_text(encoding="utf-8"))
        capability, _error = launcher_pipe.request_bootstrap(instance.get("pipe") or "")
        debug_port = 9334
        browser = subprocess.Popen([str(CHROME), "--headless=new", "--disable-gpu",
                                    "--no-first-run", "--no-default-browser-check",
                                    "--disable-extensions", "--remote-allow-origins=*",
                                    "--force-device-scale-factor=1",
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
            print("chrome target never appeared")
            return

        def shoot(client, name, width, height):
            client.command("Emulation.setDeviceMetricsOverride",
                           {"width": width, "height": height, "deviceScaleFactor": 1,
                            "mobile": False})
            time.sleep(1.5)
            shot = client.command("Page.captureScreenshot", {"format": "png"})
            blob = shot.get("result", {}).get("data")
            if not blob:
                report[name] = "capture failed"
                return
            path = SHOTS / f"{name}.png"
            path.write_bytes(base64.b64decode(blob))
            report[name] = {"file": str(path), "kb": round(path.stat().st_size / 1024, 1),
                            "size": [width, height]}

        client = Client(target["webSocketDebuggerUrl"])
        time.sleep(4.0)
        shoot(client, "reader-wide", 1500, 950)
        # The settings screen is where the two new AI controls live; the agent asked for an eyeball.
        client.command("Runtime.evaluate",
                       {"expression": "document.querySelector('#settings-button').click()"})
        time.sleep(1.2)
        shoot(client, "settings-dialog", 1500, 950)
        client.command("Runtime.evaluate",
                       {"expression": "document.querySelector('#settings-dialog').close()"})
        client.command("Runtime.evaluate",
                       {"expression": "document.querySelector('#toggle-notes').click()"})
        time.sleep(0.8)
        shoot(client, "notes-collapsed", 1500, 950)
        client.command("Runtime.evaluate",
                       {"expression": "document.querySelector('#toggle-notes').click()"})
        time.sleep(0.6)
        shoot(client, "reader-narrow", 900, 900)
        client.command("Emulation.clearDeviceMetricsOverride")
    finally:
        if browser is not None:
            browser.terminate()
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
        import shutil
        shutil.rmtree(profile, ignore_errors=True)
        shutil.rmtree(data_dir, ignore_errors=True)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
