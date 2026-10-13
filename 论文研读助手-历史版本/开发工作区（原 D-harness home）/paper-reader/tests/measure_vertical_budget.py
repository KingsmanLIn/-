"""Measure the vertical budget of the reader: how much height is left for the PDF page.

    python tests/measure_vertical_budget.py [--label before]
"""
from __future__ import annotations

import argparse
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
SIZES = [(1366, 768), (1920, 1080), (1280, 800), (1600, 900)]
OUT = Path(__file__).resolve().parent / "vertical-budget.json"

PROBE = r"""
const url = process.argv[2], port = process.argv[3], sizes = JSON.parse(process.argv[4]);
const list = await (await fetch('http://127.0.0.1:' + port + '/json/list')).json();
const page = list.find(t => t.type === 'page');
const ws = new WebSocket(page.webSocketDebuggerUrl);
let id = 0; const pending = new Map();
const send = (m, p = {}) => new Promise((res, rej) => { const i = ++id; pending.set(i, {res, rej}); ws.send(JSON.stringify({id: i, method: m, params: p})); });
ws.addEventListener('message', ev => { const m = JSON.parse(ev.data); if (m.id && pending.has(m.id)) { const {res, rej} = pending.get(m.id); pending.delete(m.id); m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result); } });
await new Promise(r => ws.addEventListener('open', r));
await send('Runtime.enable'); await send('Page.enable');
const ev = async (e) => { const r = await send('Runtime.evaluate', {expression: e, awaitPromise: true, returnByValue: true}); if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails.exception?.description)); return r.result.value; };
const sleep = ms => new Promise(r => setTimeout(r, ms));
await send('Page.navigate', {url});
await sleep(7500);
const out = [];
for (const [w, h] of sizes) {
  await send('Emulation.setDeviceMetricsOverride', {width: w, height: h, deviceScaleFactor: 1, mobile: false});
  await sleep(1500);
  out.push(await ev(`((vw, vh) => {
    const rect = (sel) => { const el = document.querySelector(sel); if (!el) return null; const r = el.getBoundingClientRect(); return {top: Math.round(r.top), h: Math.round(r.height), w: Math.round(r.width)}; };
    const scroll = document.querySelector('#pdf-scroll');
    const canvas = document.querySelector('#pdf-canvas').getBoundingClientRect();
    const spans = [...document.querySelectorAll('#text-layer span')].filter(s => (s.textContent || '').trim().length > 6);
    const sizesPx = spans.map(s => parseFloat(getComputedStyle(s).fontSize)).filter(v => v > 0).sort((a, b) => a - b);
    const vp = vh;
    const parts = {
      topbar: rect('.topbar'), paperHeader: rect('.paper-strip'), ribbon: rect('.reading-ribbon'),
      workspace: rect('.workspace'), toolbar: rect('.pdf-toolbar'), subbar: rect('.pdf-subbar'),
      scroll: rect('#pdf-scroll'), footer: rect('.pdf-footer'),
    };
    const chrome = (parts.topbar?.h || 0) + (parts.paperHeader?.h || 0) + (parts.ribbon?.h || 0)
      + (parts.toolbar?.h || 0) + (parts.subbar?.h || 0) + (parts.footer?.h || 0)
      + 2 * 22;   // reader-view padding
    return {
      viewport: [vw, vh], parts, chromePx: Math.round(chrome),
      pdfHeightPx: parts.scroll ? parts.scroll.h : 0,
      pdfShareOfViewport: parts.scroll ? Math.round(parts.scroll.h / vp * 1000) / 10 : 0,
      canvas: [Math.round(canvas.width), Math.round(canvas.height)],
      medianFontPx: sizesPx.length ? Math.round(sizesPx[Math.floor(sizesPx.length / 2)] * 100) / 100 : null,
      wholePage: scroll ? (scroll.scrollHeight - scroll.clientHeight) <= 2 : null,
      zoom: document.querySelector('#zoom-select').value,
      notesWidth: rect('.notes-panel') ? rect('.notes-panel').w : null,
      pdfWidth: rect('.pdf-panel') ? rect('.pdf-panel').w : null,
      parser: document.querySelector('.paper-strip h1') ? document.querySelector('.paper-strip h1').textContent : '',
    };
  })(${w}, ${h})`));
}
console.log(JSON.stringify(out, null, 2));
ws.close();
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", default="")
    parser.add_argument("--out", default=str(OUT))
    args = parser.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="vert-budget-"))
    profile = Path(tempfile.mkdtemp(prefix="cdp-vert-"))
    fresh_library_v1(tmp)
    store = server.Store(tmp)
    manager = analysis.AnalysisManager(store, provider_factory=lambda s: providers.DisabledAdapter(),
                                       log=lambda record: None)
    port = free_port()
    httpd = server.ThreadingHTTPServer(("127.0.0.1", port),
                                       server.handler_for(store, "vert-token", manager))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    debug_port = free_port()
    driver = Path(tempfile.gettempdir()) / "vert_budget.mjs"
    driver.write_text(PROBE, encoding="utf-8")
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
        run = subprocess.run(["node", str(driver), f"http://127.0.0.1:{port}/?token=vert-token",
                              str(debug_port), json.dumps(SIZES)],
                             capture_output=True, text=True, encoding="utf-8", errors="replace",
                             timeout=400)
        if not (run.stdout or "").strip().startswith("["):
            raise SystemExit("probe failed:\n" + run.stdout + run.stderr[:1500])
        rows = json.loads(run.stdout)
        payload = {"label": args.label, "rows": rows} if args.label else rows
        Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        for row in rows:
            w, h = row["viewport"]
            parts = row["parts"]
            print(f"{w}x{h}: chrome {row['chromePx']}px | PDF pane {row['pdfHeightPx']}px "
                  f"({row['pdfShareOfViewport']}% of viewport) | canvas {row['canvas']} | "
                  f"text {row['medianFontPx']}px | whole page {row['wholePage']} | zoom {row['zoom']}")
            if row.get("collapsed"):
                col = row["collapsed"]
                print(f"    [记录栏收起] PDF 宽 {col['pdfWidthPx']}px 高 {col['pdfHeightPx']}px | "
                      f"canvas {col['canvas']} | text {col['medianFontPx']}px | whole page {col['wholePage']}")
            header_h = parts['paperHeader']['h'] if parts['paperHeader'] else 0
            print(f"    topbar {parts['topbar']['h']} header {header_h} "
                  f"ribbon {parts['ribbon']['h']} toolbar {parts['toolbar']['h']} "
                  f"subbar {parts['subbar']['h']} footer {parts['footer']['h']} "
                  f"| pdf width {row['pdfWidth']} notes width {row['notesWidth']}")
        print("written:", args.out)
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
