"""Audit D: browser-side questions the acceptance tests do not answer.

Measures/computes what the acceptance tests never asserted:
* the rendered glyph size of a page in fit-page mode (can a human read it?);
* what happens to a wheel event that arrives inside the 600 ms lock (swallowed?);
* whether the browser page itself scrolls while paging (overscroll);
* whether the visible page is the one the toolbar claims after load and after resize.

    python tests/audit_reader_visual.py
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
OUT = Path(__file__).resolve().parent / "audit-reader-visual.json"

DRIVER = r"""
const url = process.argv[2], port = process.argv[3];
const list = await (await fetch('http://127.0.0.1:' + port + '/json/list')).json();
const page = list.find(t => t.type === 'page');
const ws = new WebSocket(page.webSocketDebuggerUrl);
let id = 0; const pending = new Map();
const send = (m, p = {}) => new Promise((res, rej) => { const i = ++id; pending.set(i, {res, rej}); ws.send(JSON.stringify({id: i, method: m, params: p})); });
ws.addEventListener('message', ev => { const m = JSON.parse(ev.data); if (m.id && pending.has(m.id)) { const {res, rej} = pending.get(m.id); pending.delete(m.id); m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result); } });
await new Promise(r => ws.addEventListener('open', r));
await send('Runtime.enable'); await send('Page.enable');
const ev = async (expression) => {
  const r = await send('Runtime.evaluate', {expression, awaitPromise: true, returnByValue: true});
  if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails.exception?.description || r.exceptionDetails.text));
  return r.result.value;
};
const sleep = ms => new Promise(r => setTimeout(r, ms));
const out = {};
const state = () => ev('(() => { const s = document.querySelector("#pdf-scroll"); const c = document.querySelector("#pdf-canvas"); return {page: Number(document.querySelector("#page-input").value), total: Number((document.querySelector("#page-total").textContent||"/0").replace(/[^0-9]/g,"")), scrollTop: s.scrollTop, overflow: s.scrollHeight - s.clientHeight, canvas: [Math.round(c.getBoundingClientRect().width), Math.round(c.getBoundingClientRect().height)], windowScrollY: window.scrollY, bodyScrollTop: document.body.scrollTop, docScrollTop: document.documentElement.scrollTop, announcer: document.querySelector("#page-announcer").textContent, renderedText: (document.querySelector("#text-layer span") || {}).textContent || ""}; })()');
const wheel = async (dy) => {
  const box = await ev('(() => { const r = document.querySelector("#pdf-scroll").getBoundingClientRect(); return {x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2)}; })()');
  await send('Input.dispatchMouseEvent', {type: 'mouseWheel', x: box.x, y: box.y, deltaX: 0, deltaY: dy, pointerType: 'mouse'});
};

await send('Page.navigate', {url});
await sleep(7500);
await send('Emulation.setDeviceMetricsOverride', {width: 1366, height: 768, deviceScaleFactor: 1, mobile: false});
await sleep(1500);

// --- 1. rendered glyph size in fit-page ------------------------------------------
out.glyphSize = await ev(`(() => {
  const layer = document.querySelector('#text-layer');
  const spans = [...layer.querySelectorAll('span')].filter(s => (s.textContent || '').trim().length > 8);
  const s = spans[0];
  if (!s) return {ok: false};
  const cs = getComputedStyle(s);
  const box = s.getBoundingClientRect();
  const canvas = document.querySelector('#pdf-canvas').getBoundingClientRect();
  const viewportHeightFactor = canvas.height;   // css px of the whole page
  const scale = parseFloat(getComputedStyle(document.querySelector('#pdf-page')).getPropertyValue('--scale-factor')) || 0;
  // the text layer scales glyphs by the same factor as the canvas, so the effective
  // on-screen size is the PDF's own font size multiplied by the scale
  return {ok: true, fontSize: cs.fontSize, transform: cs.transform, spanHeight: Math.round(box.height * 100) / 100,
          scaleFactor: scale, layerWidth: Math.round(document.querySelector('#text-layer').getBoundingClientRect().width),
          canvasWidth: Math.round(canvas.width), canvasHeight: Math.round(canvas.height),
          baseFontPx: cs.fontSize ? Math.round(parseFloat(cs.fontSize) * 100) / 100 : null,
          effectivePx: Math.round(parseFloat(cs.fontSize) * scale * 100) / 100};
})()`);

// --- 2. a wheel event inside the lock: what happens to it? -----------------------
out.wheelInLock = await ev('JSON.stringify({before: Number(document.querySelector("#page-input").value)})');
const before = await state();
await wheel(120);
const events = await ev(`(() => {
  window.__auditWheel = [];
  const box = document.querySelector('#pdf-scroll');
  box.addEventListener('wheel', e => { window.__auditWheel.push({t: performance.now(), dy: e.deltaY, prevented: e.defaultPrevented}); });
  return true;
})()`);
await sleep(120);                       // still inside the 600 ms lock
await wheel(120);
await sleep(120);
const during = await state();
await sleep(900);                       // lock expired
await wheel(120);
await sleep(300);
const after = await state();
out.wheelLock = {pageBefore: before.page, pageDuringLock: during.page, pageAfterLock: after.page,
                 observed: await ev('JSON.stringify(window.__auditWheel)')};

// --- 3. does the browser page scroll while paging? -------------------------------
out.scrollDuringPaging = {before: before.windowScrollY, during: during.windowScrollY, after: after.windowScrollY,
                          docScrollTop: after.docScrollTop, bodyScrollTop: after.bodyScrollTop};

// --- 4. rapid paging while the earlier render is still completing -----------------
const startPage = (await state()).page;
await ev('(() => { window.__canvasLog = []; const c = document.querySelector("#pdf-canvas"); const o = new MutationObserver(() => { window.__canvasLog.push({t: performance.now(), w: c.width, h: c.height}); }); o.observe(c, {attributes: true, attributeFilter: ["width", "height"]}); window.__canvasObserver = o; return true; })()');
for (let i = 0; i < 5; i++) { await wheel(120); await sleep(60); }
await sleep(6000);
const settled = await state();
out.rapidDuringLock = {startPage, settledPage: settled.page, canvas: settled.canvas,
                       mutations: await ev('window.__canvasLog.length'),
                       observerAlive: await ev('!!window.__canvasObserver')};

// --- 5. resize storm while a page is rendering ------------------------------------
for (const [w, h] of [[1600, 900], [1280, 800], [1366, 768], [1024, 768]]) {
  await send('Emulation.setDeviceMetricsOverride', {width: w, height: h, deviceScaleFactor: 1, mobile: false});
  await sleep(180);
}
await sleep(3000);
out.afterResizeStorm = await state();
out.layerMatchesCanvas = await ev('(() => { const c = document.querySelector("#pdf-canvas").getBoundingClientRect(); const l = document.querySelector("#text-layer").getBoundingClientRect(); return Math.abs(c.width - l.width) <= 1 && Math.abs(c.height - l.height) <= 1; })()');
out.pageNumberIsDisplayedPage = await ev('(() => { const n = Number(document.querySelector("#page-input").value); return {page: n, announcer: document.querySelector("#page-announcer").textContent, firstSpan: (document.querySelector("#text-layer span")||{}).textContent || ""}; })()');

const shot = await send('Page.captureScreenshot', {format: 'png'});
out.screenshotData = shot.data;
console.log(JSON.stringify(out, null, 2));
ws.close();
"""


def main():
    tmp = Path(tempfile.mkdtemp(prefix="audit-d-"))
    profile = Path(tempfile.mkdtemp(prefix="cdp-audit-d-"))
    fresh_library_v1(tmp)
    store = server.Store(tmp)
    manager = analysis.AnalysisManager(store, provider_factory=lambda s: providers.DisabledAdapter(),
                                       log=lambda record: None)
    port = free_port()
    httpd = server.ThreadingHTTPServer(("127.0.0.1", port),
                                       server.handler_for(store, "audit-d-token", manager))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    debug_port = free_port()
    driver = Path(tempfile.gettempdir()) / "audit_d_driver.mjs"
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
        run = subprocess.run(["node", str(driver), f"http://127.0.0.1:{port}/?token=audit-d-token",
                              str(debug_port)], capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=400)
        if not (run.stdout or "").strip().startswith("{"):
            raise SystemExit("driver failed:\n" + run.stdout + run.stderr[:2000])
        report = json.loads(run.stdout)
        shot = report.pop("screenshotData", None)
        if shot:
            (Path(__file__).resolve().parent / "audit-fit-page-1366.png").write_bytes(
                base64.b64decode(shot))
        OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
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
