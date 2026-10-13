"""Double-click launcher.

Startup has to be forgiving: this folder may already have its service running, the port may
be held by another copy of the app, or the server may fail for a real reason. Each case ends
with the user either seeing the reader or reading one sentence that says what to do - never
with a bare "启动失败".

* this folder's service is up        -> open it
* another copy holds the port        -> say which, and open the one that is up
* the server exits during start-up   -> show the last lines of run/startup.log
"""
from pathlib import Path
import hashlib
import json
import os
import subprocess
import sys
import time
from urllib.request import urlopen
import webbrowser

ROOT = Path(__file__).resolve().parent
fingerprint = hashlib.sha256(str(ROOT).encode()).hexdigest()[:16]
data_fingerprint = hashlib.sha256(str(ROOT / "data").encode()).hexdigest()[:16]
LOG = ROOT / "run" / "startup.log"
START_PORT = int(os.environ.get("PAPER_READER_PORT") or 8765)
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DIALOG_CODE = ("import ctypes,sys;"
               "ctypes.windll.user32.MessageBoxW(0, sys.argv[1], sys.argv[2], "
               "int(sys.argv[3]))")


def probe(port, timeout=1.0):
    """Health payload of a local paper-reader service, or None."""
    try:
        with urlopen(f"http://127.0.0.1:{int(port)}/api/health", timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return payload if payload.get("app") == "paper-reader" else None
    except Exception:
        return None


def recorded_port():
    try:
        return int(json.loads((ROOT / "run" / "server.json").read_text())["port"])
    except Exception:
        return None


def survey(deep=False):
    """Find a responding service.

    Returns (url, belongs_to_this_folder, health). A foreign instance is only reported
    when this folder recorded a service on that port, or when `deep` asks for the full
    range (used after our own server failed to start, to explain what is in the way).
    """
    mine = recorded_port()
    ports = [port for port in [mine] if port]
    if deep:
        ports += [port for port in range(START_PORT, START_PORT + 30) if port not in ports]
    for port in ports:
        health = probe(port)
        if not health:
            continue
        url = f"http://127.0.0.1:{port}"
        if health.get("root") == fingerprint and health.get("data") == data_fingerprint:
            return url, True, health
        if port == mine:
            return url, False, health
    return None, False, None


def port_owner(port):
    """(pid, executable path) of the process listening on a local port, best effort."""
    try:
        output = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True,
                                timeout=10, creationflags=NO_WINDOW).stdout
    except Exception:
        return None, ""
    needle = f"127.0.0.1:{int(port)}"
    pid = None
    for line in output.splitlines():
        if needle in line and "LISTENING" in line.upper():
            parts = line.split()
            if parts and parts[-1].isdigit():
                pid = int(parts[-1])
                break
    if not pid:
        return None, ""
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)      # QUERY_LIMITED_INFORMATION
        if not handle:
            return pid, ""
        try:
            size = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                exe = buffer.value
                # show the software folder, not pythonw.exe: that is what the user can close
                return pid, str(Path(exe).parent) if exe.lower().endswith(pythonw.exe) else exe
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return pid, ""
    return pid, ""


def message(text, icon=0x40):
    """Log the sentence, and show a dialog without blocking this process.

    The dialog runs in a separate short-lived process: MessageBoxW is modal, so calling it
    here would stop the launcher (and any automated run) until somebody clicked OK.
    """
    sys.stderr.write(text.replace("\n", " ") + "\n")
    sys.stderr.flush()
    if os.name != "nt" or os.environ.get("PAPER_READER_NO_DIALOG") == "1":
        return
    try:
        subprocess.Popen([sys.executable, "-c", DIALOG_CODE, text, "论文研读助手", str(icon)],
                         creationflags=NO_WINDOW)
    except Exception:
        pass


def open_reader(url):
    if os.environ.get("PAPER_READER_NO_BROWSER") != "1":
        webbrowser.open(url)


def tail_log(lines=8):
    try:
        content = LOG.read_text(encoding="utf-8", errors="replace").splitlines()
        return [line for line in content if line.strip()][-lines:]
    except Exception:
        return []


def rotate_log():
    try:
        if LOG.exists() and LOG.stat().st_size > 2 * 1024 * 1024:
            rotated = ROOT / "run" / "startup.old.log"
            if rotated.exists():
                rotated.unlink()
            LOG.replace(rotated)
    except OSError:
        pass


def start_server():
    (ROOT / "run").mkdir(exist_ok=True)
    rotate_log()
    with LOG.open("ab") as log:
        return subprocess.Popen([sys.executable, str(ROOT / "server.py")], cwd=ROOT,
                                stdout=log, stderr=log, creationflags=NO_WINDOW)


url, exact, health = survey()
if url and exact:
    open_reader(url)
    raise SystemExit(0)

if url and not exact:
    pid, where = port_owner(url.rsplit(":", 1)[1])
    detail = f"\n\n它来自：{where}" if where else (f"\n\n进程号：{pid}" if pid else "")
    message("已经有一个“论文研读助手”在运行（版本 " + str(health.get("version")) + "）：\n"
            + url + detail + "\n\n"
            "如果那是旧版本，请先关掉它再启动本目录：\n"
            "在它的设置页底部点“保存并退出软件”，然后重新双击本目录的启动文件。\n\n"
            "现在先为你打开正在运行的那个。", 0x30)
    open_reader(url)
    raise SystemExit(0)

process = start_server()
for _ in range(120):
    url, exact, health = survey()
    if url and exact:
        break
    if process.poll() is not None:
        break
    time.sleep(0.15)

if not (url and exact):
    # our own service did not come up; find out whether something else holds the port
    url, exact, health = survey(deep=True)

if url and exact:
    open_reader(url)
    raise SystemExit(0)

if url and not exact:
    open_reader(url)
    message("本目录的服务没能启动：端口被另一个“论文研读助手”占用。\n\n" + url +
            "\n\n请先退出那个实例，再重新双击本目录的启动文件。", 0x30)
    raise SystemExit(0)

lines = tail_log()
hint = "\n".join(lines) if lines else "（run/startup.log 里还没有内容）"
message("论文研读助手启动失败。\n\n"
        "请把下面这段发给我，或先双击“备用启动.cmd”查看完整信息：\n\n" + hint[-700:], 0x10)
raise SystemExit(1)
