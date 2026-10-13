"""Double-click launcher. Reuses this folder's running service."""
from pathlib import Path
import hashlib
import json
import subprocess
import sys
import time
from urllib.request import urlopen
import webbrowser

ROOT = Path(__file__).resolve().parent
fingerprint = hashlib.sha256(str(ROOT).encode()).hexdigest()[:16]
data_fingerprint = hashlib.sha256(str(ROOT / "data").encode()).hexdigest()[:16]


def running_url():
    try:
        port = json.loads((ROOT / "run" / "server.json").read_text())["port"]
        url = f"http://127.0.0.1:{int(port)}"
        with urlopen(url + "/api/health", timeout=1) as r:
            state = json.load(r)
        return url if state.get("root") == fingerprint and state.get("data") == data_fingerprint else None
    except Exception:
        return None


url = running_url()
if url is None:
    (ROOT / "run").mkdir(exist_ok=True)
    with (ROOT / "run" / "startup.log").open("ab") as log:
        subprocess.Popen([sys.executable, str(ROOT / "server.py")], cwd=ROOT, stdout=log, stderr=log, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    for _ in range(80):
        url = running_url()
        if url:
            break
        time.sleep(.15)
if url:
    import os
    if os.environ.get("PAPER_READER_NO_BROWSER") != "1":
        webbrowser.open(url)
else:
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, "启动失败。请查看软件文件夹内 run/startup.log，或使用备用启动.cmd。", "论文研读助手", 16)
    except Exception:
        pass
