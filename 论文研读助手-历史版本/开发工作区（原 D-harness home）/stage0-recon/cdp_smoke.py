"""Stage 0 feasibility check: drive the live app in headless Chrome via CDP.

Node >= 22 has a global WebSocket, so no npm install is needed.
Runs read-only UI measurements: does the app boot, how large is the PDF area,
does the canvas render, and does the page-fit maths leave vertical slack?
"""
from __future__ import annotations

import json
import os
import re
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
const url = process.argv[2];
const list = await (await fetch('http://127.0.0.1:' + process.argv[3] + '/json/list')).json();
const page = list.find(t => t.type === 'page') || list.find(t => t.type === 'tab');
if (!page) { console.log(JSON.stringify({error: 'no page target', targets: list.map(t => t.url)})); process.exit(1); }
const ws = new WebSocket(page.webSocketDebuggerUrl);
let id = 0; const pending = new Map();
const send = (method, params = {}) => new Promise((res, rej) => { const i = ++id; pending.set(i, {res, rej}); ws.send(JSON.stringify({id: i, method, params})); });
ws.addEventListener('message', ev => { const m = JSON.parse(ev.data); if (m.id && pending.has(m.id)) { const {res, rej} = pending.get(m.id); pending.delete(m.id); m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result); } });
await new Promise(r => ws.addEventListener('open', r));
await send('Runtime.enable');
const evalJs = async (expr) => {
  const r = await send('Runtime.evaluate', {expression: expr, awaitPromise: true, returnByValue: true});
  if (r.exceptionDetails) return {exception: r.exceptionDetails.text + ' ' + JSON.stringify(r.exceptionDetails.exception?.description || '')};
  return r.result.value;
};
await send('Page.navigate', {url});
await new Promise(r => setTimeout(r, 6000));
const out = await evalJs(`(async () => {
  const q = s => document.querySelector(s);
  const rect = el => { const r = el.getBoundingClientRect(); return {w: Math.round(r.width), h: Math.round(r.height), top: Math.round(r.top), left: Math.round(r.left)}; };
  const scroll = q('#pdf-scroll');
  const canvas = q('#pdf-canvas');
  return {
    title: document.title,
    boot: !!document.querySelector('#boot')?.textContent?.includes('token'),
    paperTitle: q('#paper-title')?.textContent?.slice(0, 40),
    pageTotal: q('#page-total')?.textContent,
    viewport: {w: innerWidth, h: innerHeight, dpr: devicePixelRatio},
    sidebar: rect(document.querySelector('.sidebar')),
    pdfPanel: rect(document.querySelector('.pdf-panel')),
    pdfScroll: rect(scroll),
    notesPanel: rect(document.querySelector('.notes-panel')),
    canvas: canvas ? {cssW: canvas.style.width, cssH: canvas.style.height, attrW: canvas.width, attrH: canvas.height} : null,
    pdfPageBox: q('#pdf-page') ? rect(q('#pdf-page')) : null,
    scrollMetrics: scroll ? {clientW: scroll.clientWidth, clientH: scroll.clientHeight, scrollW: scroll.scrollWidth, scrollH: scroll.scrollHeight} : null,
    textLayerSpans: document.querySelectorAll('#text-layer span').length,
    printedLabel: q('#printed-label')?.textContent,
    zoomValue: q('#zoom-select')?.value,
  };
})()`);
console.log(JSON.stringify(out, null, 2));
ws.close();
"""


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def main():
    debug_port = free_port()
    profile = Path(tempfile.mkdtemp(prefix="cdp-profile-"))
    driver = Path(tempfile.gettempdir()) / "stage0_cdp_driver.mjs"
    driver.write_text(DRIVER, encoding="utf-8")
    proc = subprocess.Popen([
        CHROME, "--headless=new", f"--remote-debugging-port={debug_port}",
        f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
        "--disable-extensions", "--window-size=1600,1000", "--hide-scrollbars", "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(60):
            try:
                with urlopen(f"http://127.0.0.1:{debug_port}/json/version", timeout=1) as r:
                    ver = json.load(r)
                break
            except Exception:
                time.sleep(0.25)
        else:
            print(json.dumps({"error": "chrome debug port never opened"}))
            return 1
        print("chrome:", ver.get("Browser"))
        out = subprocess.run(["node", str(driver), APP, str(debug_port)], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=120)
        print((out.stdout or "").strip() or (out.stderr or "").strip()[:2000])
        return 0 if out.returncode == 0 else out.returncode
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
