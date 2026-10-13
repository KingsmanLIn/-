"""Install the built installer silently, run it, then uninstall — and check the library survives.

This is the end-to-end proof for the installer: a real silent install into a temp directory, a
real launch through the same .vbs the shortcut uses (health check on the port it recorded), a
graceful stop, then a real uninstall — asserting that the reader's `data\\` folder is left alone.

Usage:  runtime\\python.exe tests\\verify_installer.py
"""
import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

LAB = Path(__file__).resolve().parent.parent
DIST = Path(r"C:\Users\ASUS\OneDrive\Desktop\论文研读助手\论文研读助手-历史版本\开发工作区（原 D-harness home）\dist")
APP_VERSION = "1.3.8"
SETUP = DIST / f"论文研读助手-{APP_VERSION}-安装程序.exe"
PY = LAB / "runtime" / "python.exe"


def run(command, timeout=600):
    return subprocess.run([str(part) for part in command], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def main():
    if not SETUP.is_file():
        raise SystemExit(f"installer missing: {SETUP} (run tools/build_installer.py)")
    root = Path(tempfile.mkdtemp(prefix="pr-install-"))
    target = root / "app"
    report = {"installer": SETUP.name, "install_dir": str(target)}
    try:
        installed = run([SETUP, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/NOCANCEL",
                         f"/DIR={target}", f"/LOG={root / 'install.log'}", "/NOICONS"])
        report["install_exit"] = installed.returncode
        report["launcher_present"] = (target / "启动论文研读助手.vbs").is_file()
        report["runtime_present"] = (target / "runtime" / "python.exe").is_file()
        report["library_present"] = (target / "data" / "library.json").is_file()
        report["uninstaller_present"] = (target / "unins000.exe").is_file()
        report["icon_present"] = (target / "icon.ico").is_file()
        report["no_dev_folders"] = not any((target / name).exists()
                                          for name in ("tests", "installer", "tools"))
        if installed.returncode != 0 or not report["launcher_present"]:
            print(json.dumps(report, ensure_ascii=False, indent=2))
            raise SystemExit(f"silent install failed (exit {installed.returncode})")

        # A marker inside data/ proves the uninstall policy: it must survive.
        marker = target / "data" / "papers" / "reader-library-marker.pdf"
        marker.write_bytes(b"%PDF-1.4 marker")

        # Launch exactly as the Start-menu shortcut does, then ask the recorded port for health.
        subprocess.Popen(["wscript.exe", "//nologo", "//B",
                          str(target / "启动论文研读助手.vbs")],
                         cwd=str(target), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        instance = target / "run" / "server.json"
        health, port = None, None
        for _ in range(60):
            time.sleep(1.0)
            if not instance.is_file():
                continue
            try:
                info = json.loads(instance.read_text(encoding="utf-8"))
                with urllib.request.urlopen(f"http://127.0.0.1:{info['port']}/api/health",
                                            timeout=3) as response:
                    health = json.loads(response.read().decode())
                port = info["port"]
                break
            except Exception:                                    # noqa: BLE001
                continue
        report["health"] = health
        report["port"] = port
        report["health_version_matches"] = bool(health and health.get("version") == APP_VERSION)
        # The user's own instance already holds 8765, so the installed copy must have moved on.
        report["port_shifted_away_from_8765"] = bool(port and port != 8765)

        stopped = run([PY, LAB / "tests" / "live_control.py", "stop", target])
        report["stop_log"] = (stopped.stdout or "").strip().splitlines()[-2:]
        time.sleep(1.5)

        removed = run([target / "unins000.exe", "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"])
        report["uninstall_exit"] = removed.returncode
        for _ in range(30):
            time.sleep(0.5)
            if not (target / "runtime" / "python.exe").is_file():
                break
        report["app_files_removed"] = not (target / "runtime" / "python.exe").is_file()
        report["library_kept"] = (target / "data" / "library.json").is_file()
        report["marker_kept"] = marker.is_file()
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
