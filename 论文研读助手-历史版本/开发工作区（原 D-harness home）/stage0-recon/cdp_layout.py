"""Stage 0: measure the reader height chain to size the fit-page work."""
from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import urlopen

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
APP = "http://127.0.0.1:8765/"

DRIVER = r"""
const url = process.argv[2], port = process.argv[3];
const list = await (await fetch('http://127.0.0.1:' + port + '/json/list')).json();
const page = list.find(t => t.type === 'page') || list.find(t => t.type === 'tab');
const ws = new WebSocket(page.webSocketDebuggerUrl);
let id = 0; const pending = new Map();
const send = (m, p = {}) => new Promise((res, rej) => { const i = ++id; pending.set(i, {res, rej}); ws.send(JSON.stringify({id: i, method: m, params: p})); });
ws.addEventListener('message', ev => { const m = JSON.parse(ev.data); if (m.id && pending.has(m.id)) { const {res, rej} = pending.get(m.id); pending.delete(m.id); m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result); } });
await new Promise(r => ws.addEventListener('open', r));
await send('Runtime.enable');
const ev = async (e) => { const r = await send('Runtime.evaluate', {expression: e, awaitPromise: true, returnByValue: true}); return r.exceptionDetails ? {exception: JSON.stringify(r.exceptionDetails)} : r.result.value; };
await send('Page.enable');
await send('Page.navigate', {url});
await new Promise(r => setTimeout(r, 7000));
const probe = `(() => {
  const q = s => document.querySelector(s);
  const box = el => { if (!el) return null; const r = el.getBoundingClientRect(); const cs = getComputedStyle(el);
    return {w: Math.round(r.width), h: Math.round(r.height), top: Math.round(r.top), bottom: Math.round(r.bottom),
            minH: cs.minHeight, overflow: cs.overflow, flex: cs.flex, display: cs.display, pad: cs.padding}; };
  const scroll = q('#pdf-scroll');
  const canvas = q('#pdf-canvas');
  const report = {
    viewport: {w: innerWidth, h: innerHeight},
    body: box(document.body), sidebar: box(q('.sidebar')), main: box(q('.main')),
    topbar: box(q('.topbar')), readerView: box(q('#reader-view')),
    paperHeader: box(q('.paper-header')), ribbon: box(q('.reading-ribbon')),
    workspace: box(q('.workspace')), pdfPanel: box(q('.pdf-panel')),
    pdfToolbar: box(q('.pdf-toolbar')), pdfSubbar: box(q('.pdf-subbar')),
    pdfScroll: box(scroll), pdfFooter: box(q('.pdf-footer')), notesPanel: box(q('.notes-panel')),
    canvasCss: canvas ? {w: canvas.style.width, h: canvas.style.height} : null,
    printableSlack: scroll ? {verticalSlack: scroll.clientHeight - (canvas ? parseFloat(canvas.style.height) : 0),
                              horizontalSlack: scroll.clientWidth - (canvas ? parseFloat(canvas.style.width) : 0)} : null,
    fitsPageNow: false,
  };
  report.fitsPageNow = report.printableSlack ? (report.printableSlack.verticalSlack >= 0 && report.printableSlack.horizontalSlack >= 0) : false;
  return report;
})()`;
const sizes = [[1600,1000],[1440,900],[1280,800],[1920,1080]];
const out = {};
for (const [w,h] of sizes) {
  await send('Emulation.setDeviceMetricsOverride', {width: w, height: h, deviceScaleFactor: 1, mobile: false});
  await new Promise(r => setTimeout(r, 1600));
  out[w + 'x' + h] = await ev(probe);
}
await send('Emulation.clearDeviceMetricsOverride');
console.log(JSON.stringify(out, null, 2));
ws.close();
"""


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def main():
    port, profile = free_port(), Path(tempfile.mkdtemp(prefix="cdp-profile-"))
    driver = Path(tempfile.gettempdir()) / "stage0_cdp_driver2.mjs"
    driver.write_text(DRIVER, encoding="utf-8")
    proc = subprocess.Popen([CHROME, "--headless=new", f"--remote-debugging-port={port}",
                             f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
                             "--disable-extensions", "--hide-scrollbars", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(60):
            try:
                with urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1) as r:
                    json.load(r)
                break
            except Exception:
                time.sleep(0.25)
        out = subprocess.run(["node", str(driver), APP, str(port)], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=180)
        data = json.loads((out.stdout or "").strip() or "{}")
        Path(r"D:\harness home\stage0-recon\baseline-layout.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        for size, rep in data.items():
            s = rep.get("printableSlack") or {}
            print(f"{size}: viewport h={rep['viewport']['h']} readerView h={rep['readerView']['h']} "
                  f"workspace h={rep['workspace']['h']} pdfPanel h={rep['pdfPanel']['h']} "
                  f"pdfScroll h={rep['pdfScroll']['h']} canvas h={rep['canvasCss']['h'] if rep.get('canvasCss') else '?'} "
                  f"vSlack={s.get('verticalSlack')} hSlack={s.get('horizontalSlack')} fits={rep['fitsPageNow']}")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
