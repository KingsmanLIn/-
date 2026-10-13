"""Phase 0 step 6: record the behavioural regression baseline with a real browser.

Facts recorded (all machine-derived, no manual claims):
  * PDF panel / scroll / canvas geometry and the effective scale, per viewport;
  * default zoom mode and the zoom option list;
  * current wheel behaviour inside #pdf-scroll (scrolls, no page turn);
  * reading-card field provenance: marker values injected into the copied library
    must appear verbatim in the reading card, proving the card renders
    paper.fields and nothing else.

The injected markers live only in a temporary copy of library.json + papers/.

    python tests/capture_baseline.py
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
from baseline_helpers import DATA_DIR, PROJECT_ROOT, free_port, get_json, get_text, local_service  # noqa: E402

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
OUT = PROJECT_ROOT / "tests" / "baseline-ui.json"
MARK = "ZZMARK"

DRIVER = r"""
const url = process.argv[2], port = process.argv[3], mark = process.argv[4];
const list = await (await fetch('http://127.0.0.1:' + port + '/json/list')).json();
const page = list.find(t => t.type === 'page') || list.find(t => t.type === 'tab');
const ws = new WebSocket(page.webSocketDebuggerUrl);
let id = 0; const pending = new Map();
const send = (m, p = {}) => new Promise((res, rej) => { const i = ++id; pending.set(i, {res, rej}); ws.send(JSON.stringify({id: i, method: m, params: p})); });
ws.addEventListener('message', ev => { const m = JSON.parse(ev.data); if (m.id && pending.has(m.id)) { const {res, rej} = pending.get(m.id); pending.delete(m.id); m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result); } });
await new Promise(r => ws.addEventListener('open', r));
await send('Runtime.enable'); await send('Page.enable');
const ev = async (e) => { const r = await send('Runtime.evaluate', {expression: e, awaitPromise: true, returnByValue: true}); if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails.exception?.description || r.exceptionDetails.text)); return r.result.value; };
const sleep = ms => new Promise(r => setTimeout(r, ms));
await send('Page.navigate', {url});
await sleep(7000);

const geometry = `(() => {
  const q = s => document.querySelector(s);
  const box = el => { if (!el) return null; const r = el.getBoundingClientRect(); return {w: Math.round(r.width), h: Math.round(r.height), top: Math.round(r.top), left: Math.round(r.left)}; };
  const scroll = q('#pdf-scroll'), canvas = q('#pdf-canvas'), wrap = q('#pdf-page');
  const cw = canvas ? parseFloat(canvas.style.width) : 0, ch = canvas ? parseFloat(canvas.style.height) : 0;
  return {
    viewport: {w: innerWidth, h: innerHeight, dpr: devicePixelRatio},
    sidebar: box(q('.sidebar')), topbar: box(q('.topbar')), paperHeader: box(q('.paper-header')),
    ribbon: box(q('.reading-ribbon')), workspace: box(q('.workspace')), pdfPanel: box(q('.pdf-panel')),
    pdfToolbar: box(q('.pdf-toolbar')), pdfSubbar: box(q('.pdf-subbar')), pdfScroll: box(scroll),
    pdfFooter: box(q('.pdf-footer')), notesPanel: box(q('.notes-panel')),
    canvasCss: {w: cw, h: ch}, pageWrap: box(wrap),
    scale: cw && canvas ? +(cw / 595.28).toFixed(4) : null,
    effectiveScale: wrap ? +(parseFloat(getComputedStyle(wrap).getPropertyValue('--total-scale-factor')) || 0).toFixed(4) : null,
    scrollMetrics: {clientH: scroll.clientHeight, scrollH: scroll.scrollHeight, clientW: scroll.clientWidth, scrollW: scroll.scrollWidth},
    verticalSlack: scroll.clientHeight - ch,
    horizontalSlack: scroll.clientWidth - cw,
    fitsPageCompletely: (scroll.clientHeight - ch) >= 0 && (scroll.clientWidth - cw) >= 0,
    pageInput: q('#page-input')?.value, pageTotal: q('#page-total')?.textContent,
    zoomSelectValue: q('#zoom-select')?.value,
    zoomOptions: [...q('#zoom-select').options].map(o => o.value + ':' + o.textContent),
    printedLabel: q('#printed-label')?.textContent,
    toolbarControlIds: [...document.querySelectorAll('.pdf-toolbar button, .pdf-toolbar input, .pdf-toolbar select')].map(el => el.id),
  };
})()`;

const out = {viewports: {}};
for (const [w, h] of [[1280,800],[1600,1000],[1920,1080]]) {
  await send('Emulation.setDeviceMetricsOverride', {width: w, height: h, deviceScaleFactor: 1, mobile: false});
  await sleep(1800);
  out.viewports[w + 'x' + h] = await ev(geometry);
}

// ---- wheel behaviour inside #pdf-scroll, at the default zoom ----
await send('Emulation.setDeviceMetricsOverride', {width: 1600, height: 1000, deviceScaleFactor: 1, mobile: false});
await sleep(1500);
const rect = await ev(`(() => { const r = document.querySelector('#pdf-scroll').getBoundingClientRect(); return {x: Math.round(r.left + r.width/2), y: Math.round(r.top + r.height/2)}; })()`);
const pageBefore = await ev(`document.querySelector('#page-input').value`);
const scrollBefore = await ev(`document.querySelector('#pdf-scroll').scrollTop`);
for (let i = 0; i < 3; i++) {
  await send('Input.dispatchMouseEvent', {type: 'mouseWheel', x: rect.x, y: rect.y, deltaX: 0, deltaY: 120, modifiers: 0});
  await sleep(220);
}
await sleep(700);
const scrollAfter = await ev(`document.querySelector('#pdf-scroll').scrollTop`);
const pageAfter = await ev(`document.querySelector('#page-input').value`);
const wheelListener = await ev(`(() => { const s = document.querySelector('#pdf-scroll');
  let fired = 0; const h = () => fired++; s.addEventListener('wheel', h, {passive: true});
  window.__wheelProbe = () => fired; return 'installed'; })()`);
await send('Input.dispatchMouseEvent', {type: 'mouseWheel', x: rect.x, y: rect.y, deltaX: 0, deltaY: 120, modifiers: 0});
await sleep(300);
const wheelFired = await ev(`window.__wheelProbe()`);
out.wheel = {pageBefore, pageAfter, scrollBefore, scrollAfter, wheelEventsObserved: wheelFired,
             pageTurnedByWheel: String(pageBefore) !== String(pageAfter), scrollsWithWheel: scrollAfter > scrollBefore};

// ---- wheel at a forced overflow (zoom 150%) to prove scrolling is available ----
await ev(`(() => { const s = document.querySelector('#zoom-select'); s.value = '1.5'; s.dispatchEvent(new Event('change')); })()`);
await sleep(1600);
const p2before = await ev(`document.querySelector('#page-input').value`);
const s2before = await ev(`document.querySelector('#pdf-scroll').scrollTop`);
for (let i = 0; i < 3; i++) {
  await send('Input.dispatchMouseEvent', {type: 'mouseWheel', x: rect.x, y: rect.y, deltaX: 0, deltaY: 120, modifiers: 0});
  await sleep(200);
}
await sleep(600);
out.zoom150 = {pageBefore: p2before, pageAfter: await ev(`document.querySelector('#page-input').value`),
               scrollBefore: s2before, scrollAfter: await ev(`document.querySelector('#pdf-scroll').scrollTop`),
               canvasH: await ev(`parseFloat(document.querySelector('#pdf-canvas').style.height)`),
               scrollClientH: await ev(`document.querySelector('#pdf-scroll').clientHeight`)};

// ---- reading card provenance ----
await ev(`document.querySelector('[data-tab="card"]').click()`);
await sleep(900);
out.card = await ev(`(() => {
  const card = document.querySelector('.card-preview');
  const text = card ? card.innerText : '';
  const headings = [...card.querySelectorAll('h3')].map(h => h.textContent);
  return {
    markerStagesPresent: {
      question: text.includes('${mark}-question'),
      assessment: text.includes('${mark}-assessment'),
      next: text.includes('${mark}-next'),
      summary: text.includes('${mark}-summary'),
    },
    markerCountInCard: (text.match(new RegExp('${mark}', 'g')) || []).length,
    headings,
    cardMetaText: card ? card.querySelector('.card-meta')?.textContent : null,
    unfilledPlaceholders: [...card.querySelectorAll('p.unfilled')].map(p => p.textContent),
    sourceExpression: 'app.js renderCard(): Object.entries(FIELDS).map(([k,v]) => definition(p,k)[0] + p.fields[k])',
  };
})()`);
await send('Emulation.clearDeviceMetricsOverride');
console.log(JSON.stringify(out, null, 2));
ws.close();
"""


def main():
    tmp = Path(tempfile.mkdtemp(prefix="paper-reader-baseline-"))
    shutil.copytree(DATA_DIR, tmp / "data")
    state = json.loads((tmp / "data" / "library.json").read_text(encoding="utf-8"))
    target = state["papers"][0]["id"]
    markers = {"question": f"{MARK}-question", "assessment": f"{MARK}-assessment",
               "next": f"{MARK}-next", "summary": f"{MARK}-summary"}
    with local_service(data_dir=tmp / "data") as svc:
        errors = []
        # NOTE the patch shape: /api/save whitelists only top-level record keys, so manual
        # values must travel as {"patch": {"fields": {...}}}. A bare {"patch": {"question": ...}}
        # is silently ignored and still answers 200 - see baseline-ui.json _meta.patchShapeNote.
        fields = dict(state["papers"][0]["fields"])
        for key, value in markers.items():
            from urllib.error import HTTPError
            from urllib.request import Request, urlopen
            fields[key] = value
            body = json.dumps({"token": svc["token"], "id": target,
                               "revision": state["papers"][0]["revision"],
                               "patch": {"fields": fields}}).encode()
            req = Request(svc["base"] + "/api/save", data=body, method="POST",
                          headers={"Content-Type": "application/json"})
            try:
                saved = json.loads(urlopen(req, timeout=10).read().decode("utf-8"))
                state["papers"][0]["revision"] = saved["paper"]["revision"]
            except HTTPError as exc:
                errors.append(f"{key}: {exc.code} {exc.read().decode('utf-8', 'replace')}")
        if errors:
            print("marker injection failed:", errors, file=sys.stderr)
            return 2
        verify = get_json(svc["base"] + "/api/state", svc["token"])
        stored = next(p for p in verify["papers"] if p["id"] == target)
        print("marker read-back from /api/state:",
              {k: stored["fields"].get(k) for k in markers}, "revision:", stored["revision"])
        if any(stored["fields"].get(k) != v for k, v in markers.items()):
            print("markers did not persist; aborting", file=sys.stderr)
            return 3

        port = free_port()
        profile = Path(tempfile.mkdtemp(prefix="cdp-profile-"))
        driver = Path(tempfile.gettempdir()) / "phase0_baseline_capture.mjs"
        driver.write_text(DRIVER, encoding="utf-8")
        proc = subprocess.Popen([CHROME, "--headless=new", f"--remote-debugging-port={port}",
                                 f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
                                 "--disable-extensions", "--hide-scrollbars", "about:blank"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(80):
                try:
                    with urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1) as r:
                        json.load(r)
                    break
                except Exception:
                    time.sleep(0.25)
            run = subprocess.run(["node", str(driver), svc["base"] + "/", str(port), MARK],
                                 capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
            data = json.loads((run.stdout or "{}").strip() or "{}")
            data["_meta"] = {
                "capturedBy": "tests/capture_baseline.py",
                "appVersion": svc["health"]["version"],
                "paperProbed": target,
                "markersInjected": markers,
                "note": "markers exist only in the temporary copy of the library",
                "sourceTree": "working copy, functionally identical to the released build",
                "patchShapeNote": ("/api/save applies only whitelisted top-level record keys from "
                                   "body.patch; manual values must be sent as patch.fields. "
                                   "Unknown patch keys are ignored silently with HTTP 200."),
            }
            OUT.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            print("written:", OUT)
            for size, geo in data["viewports"].items():
                print(f"  {size}: scroll={geo['pdfScroll']['w']}x{geo['pdfScroll']['h']} "
                      f"canvas={geo['canvasCss']['w']}x{geo['canvasCss']['h']} "
                      f"scale={geo['effectiveScale']} vSlack={geo['verticalSlack']} fits={geo['fitsPageCompletely']}")
            print("  default zoom:", data["viewports"]["1600x1000"]["zoomSelectValue"],
                  data["viewports"]["1600x1000"]["zoomOptions"])
            print("  wheel@default:", data["wheel"])
            print("  card:", json.dumps(data["card"], ensure_ascii=False))
            return 0
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            shutil.rmtree(profile, ignore_errors=True)
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
