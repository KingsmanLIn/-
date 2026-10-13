"""Build the Windows installer (and refresh the portable staging tree it packages).

Steps: make_icon.py (needs Pillow) -> build_package.py (staged portable tree) -> ISCC (installer).
Everything lands in D:\\harness home\\dist; the SHA256 of the installer is printed at the end.

Usage:  runtime\\python.exe tools\\build_installer.py      (the app's bundled runtime)
        <harness python> tools\\make_icon.py              (icon only; needs Pillow)
"""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

try:                                    # the console here is GBK; paths and text contain CJK
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

LAB = Path(__file__).resolve().parent.parent
DIST = Path(r"C:\Users\ASUS\OneDrive\Desktop\论文研读助手\论文研读助手-历史版本\开发工作区（原 D-harness home）\dist")
LIVE = Path(r"C:\Users\ASUS\OneDrive\Desktop\论文研读助手\1.3.8版本\论文研读助手-本地版-1.3.8-完整便携版")
ISCC = Path(r"C:\Users\ASUS\AppData\Local\Programs\Inno Setup 6\ISCC.exe")
APP_VERSION = "1.3.8"
STAGE = DIST / f"论文研读助手-本地版-{APP_VERSION}-完整便携版"
SETUP = DIST / f"论文研读助手-{APP_VERSION}-安装程序.exe"
ICON = LAB / "installer" / "icon.ico"
ISS = LAB / "installer" / "论文研读助手.iss"
PY = LAB / "runtime" / "python.exe"


def run(command, **kwargs):
    print("  $", " ".join(str(part) for part in command))
    done = subprocess.run([str(part) for part in command], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", **kwargs)
    tail = (done.stdout or "").strip().splitlines()[-6:]
    for line in tail:
        print("    |", line)
    if done.returncode != 0:
        print((done.stderr or "").strip()[-800:])
        raise SystemExit(f"failed: {command[0]} (exit {done.returncode})")
    return done


def main():
    if not ICON.is_file():
        raise SystemExit(f"icon missing: {ICON} (run tools/make_icon.py with the harness Python)")
    if not ISCC.is_file():
        raise SystemExit(f"Inno Setup compiler missing: {ISCC}")
    print("1) staged portable tree")
    run([PY, LAB / "tests" / "build_package.py"])
    if not (STAGE / "server.py").is_file():
        raise SystemExit(f"staging looks wrong: {STAGE}")
    print("2) installer")
    run([ISCC, f"/DStageDir={STAGE}", f"/DAppVersion={APP_VERSION}", f"/DOutputDir={DIST}", ISS],
        cwd=str(LAB / "installer"))
    if not SETUP.is_file():
        raise SystemExit(f"installer not produced: {SETUP}")
    digest = hashlib.sha256(SETUP.read_bytes()).hexdigest()
    report = {"installer": str(SETUP), "bytes": SETUP.stat().st_size, "sha256": digest,
              "stage": str(STAGE),
              "stage_files": sum(1 for path in STAGE.rglob("*") if path.is_file())}
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
