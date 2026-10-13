"""Capture the three acceptance screenshots (1440x900, 1366x768, 1024x768) of the product.

    python tests/capture_acceptance_shots.py
"""
from __future__ import annotations

import base64
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.request import urlopen

from baseline_helpers import PROJECT_ROOT, free_port, fresh_library_v1

sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import analysis      # noqa: E402
import providers     # noqa: E402
import server        # noqa: E402

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
SIZES = [(1440, 900), (1366, 768), (1024, 768)]
PAPER = "medical-2026-zang"

DRIVER = r"""
const url = process.argv[2], port = process.argv[3], sizes = JSON.parse(process.argv[4]);
const list = await (await fetch('http://127.0.0.1:' + port + '/json/list')).json();
const page = list.find(t => t.type === 'page');
const ws = new WebSocket(page.webSocketDebuggerUrl);
let id = 0; const pending = new Map();
const send = (m, p = {}) => new Promise((res, rej) => { const i = ++id; pending.set(i, {res, rej}); ws.send(JSON.stringify({id: i, method: m, params: p})); });
ws.addEventListener('message', ev => { const m = JSON.parse(ev.data); if (m.id && pending.has(m.id)) { const {res, rej} = pending.get(m.id); pending.delete(m.id); m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result); } });
await new Promise(r => ws.addEventListener('open', r));
await send('Runtime.enable'); await send('Page.enable');
const ev = async (e) => { const r = await send('Runtime.evaluate', {expression: e, awaitPromise: true, returnByValue: true}); if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails.exception?.description || r.exceptionDetails.text)); return r.result.value; };
const sleep = ms => new Promise(r => setTimeout(r, ms));
await send('Page.navigate', {url});
await sleep(7500);
const shots = [];
for (const [w, h] of sizes) {
  await send('Emulation.setDeviceMetricsOverride', {width: w, height: h, deviceScaleFactor: 1, mobile: false});
  await sleep(1800);
  const info = await ev('(() => { const ws = document.querySelector(".workspace"); const panel = document.querySelector(".pdf-panel"); const notes = document.querySelector(".notes-panel"); const c = document.querySelector("#pdf-canvas").getBoundingClientRect(); const s = document.querySelector("#pdf-scroll"); return {viewport: [innerWidth, innerHeight], panelShare: Math.round(panel.getBoundingClientRect().width / ws.getBoundingClientRect().width * 1000) / 10, notesWidth: Math.round(notes.getBoundingClientRect().width), canvas: [Math.round(c.width), Math.round(c.height)], overflow: s.scrollHeight - s.clientHeight, stacked: notes.getBoundingClientRect().top >= panel.getBoundingClientRect().bottom - 2, page: document.querySelector("#page-input").value}; })()');
  const shot = await send('Page.captureScreenshot', {format: 'png'});
  shots.push({size: w + 'x' + h, info, data: shot.data});
}
console.log(JSON.stringify(shots));
ws.close();
"""


def main():
    tmp = Path(tempfile.mkdtemp(prefix="phase8-shots-"))
    profile = Path(tempfile.mkdtemp(prefix="cdp-phase8-shots-"))
    fresh_library_v1(tmp)
    store = server.Store(tmp)
    manager = analysis.AnalysisManager(store, provider_factory=lambda s: providers.DisabledAdapter(),
                                       log=lambda record: None)
    port = free_port()
    httpd = server.ThreadingHTTPServer(("127.0.0.1", port),
                                       server.handler_for(store, "shots-token", manager))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    debug_port = free_port()
    driver = Path(tempfile.gettempdir()) / "phase8_shots.mjs"
    driver.write_text(DRIVER, encoding="utf-8")
    chrome = subprocess.Popen([CHROME, "--headless=new", f"--remote-debugging-port={debug_port}",
                               f"--user-data-dir={profile}", "--no-first-run", "--hide-scrollbars",
                               "--no-default-browser-check", "about:blank"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(80):
            try:
                with urlopen(f"http://127.0.0.1:{debug_port}/json/version", timeout=1) as response:
                    json.load(response)
                break
            except Exception:                                   # noqa: BLE001
                time.sleep(0.25)
        run = subprocess.run(["node", str(driver), f"http://127.0.0.1:{port}/?token=shots-token",
                              str(debug_port), json.dumps(SIZES)],
                             capture_output=True, text=True, encoding="utf-8", errors="replace",
                             timeout=300)
        if not (run.stdout or "").strip().startswith("["):
            raise SystemExit("driver failed:\n" + run.stdout + run.stderr[:1500])
        shots = json.loads(run.stdout)
        out_dir = Path(__file__).resolve().parent
        summary = []
        for shot in shots:
            target = out_dir / f"phase8-window-{shot['size']}.png"
            target.write_bytes(base64.b64decode(shot.pop("data")))
            summary.append({"size": shot["size"], **shot["info"], "file": target.name})
            print(f"{shot['size']}: PDF panel {shot['info']['panelShare']}% | canvas "
                  f"{shot['info']['canvas']} | overflow {shot['info']['overflow']} | "
                  f"stacked {shot['info']['stacked']} -> {target.name}")
        (out_dir / "phase8-window-shots.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print("written:", out_dir / "phase8-window-shots.json")
    finally:
        chrome.terminate()
        try:
            chrome.wait(timeout=10)
        except subprocess.TimeoutExpired:
            chrome.kill()
        httpd.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    main()
