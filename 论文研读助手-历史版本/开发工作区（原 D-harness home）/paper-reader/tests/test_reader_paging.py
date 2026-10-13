"""Phase 7 browser test: fit-page default, wheel paging, keyboard, resize, selection.

Drives a real Chrome over CDP against a temporary server; the shipped library is never
touched. Wheel events go through CDP Input.dispatchMouseEvent so the browser's own event
pipeline (including deltaMode and preventDefault) is exercised.

    python -m unittest discover -s tests -p "test_reader_paging.py"
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
import unittest
from pathlib import Path
from urllib.request import urlopen

from baseline_helpers import PROJECT_ROOT, free_port, fresh_library_v1

sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import analysis      # noqa: E402
import providers     # noqa: E402
import server
import session        # noqa: E402

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PAPER = "medical-2026-zang"
OUT = Path(__file__).resolve().parent / "phase7-paging-report.json"

DRIVER = r"""
const url = process.argv[2], port = process.argv[3], paper = process.argv[4];
const list = await (await fetch('http://127.0.0.1:' + port + '/json/list')).json();
const page = list.find(t => t.type === 'page');
const ws = new WebSocket(page.webSocketDebuggerUrl);
let id = 0; const pending = new Map();
const send = (m, p = {}) => new Promise((res, rej) => { const i = ++id; pending.set(i, {res, rej}); ws.send(JSON.stringify({id: i, method: m, params: p})); });

async function waitForSession(){
  // The page exchanges the capability in its fragment for a cookie session; its CSRF token
  // appears on window once that is done. Drivers wait for it before touching the API.
  for (let attempt = 0; attempt < 80; attempt += 1) {
    const r = await send('Runtime.evaluate', {expression: 'window.__csrf || ""', returnByValue: true});
    if (r && r.result && r.result.value) return r.result.value;
    await new Promise(done => setTimeout(done, 250));
  }
  throw new Error('session was never established');
}

ws.addEventListener('message', ev => { const m = JSON.parse(ev.data); if (m.id && pending.has(m.id)) { const {res, rej} = pending.get(m.id); pending.delete(m.id); m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result); } });
await new Promise(r => ws.addEventListener('open', r));
await send('Runtime.enable'); await send('Page.enable'); await send('Input.enable').catch(() => {});
const ev = async (expression) => {
  const r = await send('Runtime.evaluate', {expression, awaitPromise: true, returnByValue: true});
  if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails.exception?.description || r.exceptionDetails.text));
  return r.result.value;
};
const sleep = ms => new Promise(r => setTimeout(r, ms));
const out = {steps: []};
const mark = (name, extra = {}) => out.steps.push({name, ...extra});
const state = () => ev(`(() => {
  const scroll = document.querySelector('#pdf-scroll');
  return {
    page: Number(document.querySelector('#page-input').value),
    total: Number((document.querySelector('#page-total').textContent || '/0').replace(/[^0-9]/g, '')) || null,
    scrollTop: scroll.scrollTop,
    scrollOverflow: scroll.scrollHeight - scroll.clientHeight,
    canvasW: Math.round(document.querySelector('#pdf-canvas').getBoundingClientRect().width),
    canvasH: Math.round(document.querySelector('#pdf-canvas').getBoundingClientRect().height),
    layerW: Math.round(document.querySelector('#text-layer').getBoundingClientRect().width),
    layerH: Math.round(document.querySelector('#text-layer').getBoundingClientRect().height),
    highlightCount: document.querySelectorAll('#highlight-layer .pdf-highlight').length,
    announcer: document.querySelector('#page-announcer').textContent,
    zoomValue: document.querySelector('#zoom-select').value,
    scrollClass: scroll.className,
    prevDisabled: document.querySelector('#prev-page').disabled,
    nextDisabled: document.querySelector('#next-page').disabled,
    printed: document.querySelector('#printed-label').textContent,
    scrollW: scroll.clientWidth,
    scrollH: scroll.clientHeight,
  };
})()`);
const wheel = async (deltaY, opts = {}) => {
  const box = await ev(`(() => { const r = document.querySelector('#pdf-scroll').getBoundingClientRect(); return {x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2)}; })()`);
  await send('Input.dispatchMouseEvent', {
    type: 'mouseWheel', x: opts.x ?? box.x, y: opts.y ?? box.y,
    deltaX: opts.deltaX ?? 0, deltaY: deltaY,
    modifiers: opts.modifiers ?? 0, pointerType: opts.pointerType ?? 'mouse',
  });
  await sleep(opts.wait ?? 700);
};
const key = async (code, keyName) => {
  const map = {ArrowRight: {windowsVirtualKeyCode: 39, key: 'ArrowRight'}, ArrowLeft: {windowsVirtualKeyCode: 37, key: 'ArrowLeft'},
               PageDown: {windowsVirtualKeyCode: 34, key: 'PageDown'}, PageUp: {windowsVirtualKeyCode: 33, key: 'PageUp'}};
  const info = map[keyName];
  await send('Input.dispatchKeyEvent', {type: 'keyDown', code, ...info});
  await send('Input.dispatchKeyEvent', {type: 'keyUp', code, ...info});
  await sleep(700);
};

await send('Page.navigate', {url});
await new Promise(done => setTimeout(done, 800));
await waitForSession();
await sleep(7000);
await send('Emulation.setDeviceMetricsOverride', {width: 1366, height: 768, deviceScaleFactor: 1, mobile: false});
await sleep(1500);

// 1. fit-page default
out.env = await ev('JSON.stringify({vw: window.innerWidth, vh: window.innerHeight, scrollClient: document.querySelector("#pdf-scroll").clientHeight, dpr: window.devicePixelRatio, canvasAttr: [document.querySelector("#pdf-canvas").width, document.querySelector("#pdf-canvas").height], zoomMode: document.querySelector("#zoom-select").value})');
out.initial = await state();
mark('initial-fit-page', out.initial);
out.autoFit = await ev('JSON.stringify({zoom: document.querySelector("#zoom-select").value, hint: (document.querySelector("#fit-hint")||{}).textContent || ""})');

// 2. wheel down turns one page per gesture (three spaced wheel events -> three pages).
// In fit-width the page scrolls first, so start at the bottom edge.
const scrollToBottom = () => ev('(() => { const s = document.querySelector("#pdf-scroll"); s.scrollTop = s.scrollHeight; return s.scrollTop; })()');
const scrollToTop = () => ev('(() => { const s = document.querySelector("#pdf-scroll"); s.scrollTop = 0; return s.scrollTop; })()');
const before = out.initial.page;
out.edgeScroll = {needed: out.initial.scrollOverflow > 2};
await scrollToBottom();
await wheel(120);
const afterOne = await state();
await sleep(700);
await scrollToBottom();
await wheel(120);
const afterTwo = await state();
await sleep(700);
await scrollToBottom();
await wheel(120);
const afterThree = await state();
mark('wheel-one-page-per-gesture', {before, afterOne: afterOne.page, afterTwo: afterTwo.page, afterThree: afterThree.page});
out.wheelForward = {afterOne, afterTwo, afterThree};

// 3. trackpad-sized deltas must not turn pages
const beforeTiny = (await state()).page;
await wheel(4);
await wheel(6);
const afterTiny = await state();
mark('trackpad-small-delta', {before: beforeTiny, after: afterTiny.page});
out.tinyDelta = {before: beforeTiny, after: afterTiny.page};

// 4. a burst within the debounce window turns exactly one page
const beforeBurst = (await state()).page;
await scrollToBottom();
for (let i = 0; i < 5; i++) { await wheel(120, {wait: 40}); }
await sleep(700);
const afterBurst = await state();
mark('wheel-burst-debounce', {before: beforeBurst, after: afterBurst.page});
out.burst = {before: beforeBurst, after: afterBurst.page};

// 5. wheel up goes back one page (again from the top edge of the page)
const beforeUp = (await state()).page;
await scrollToTop();
await wheel(-120);
const afterUp = await state();
mark('wheel-up', {before: beforeUp, after: afterUp.page});
out.wheelBack = {before: beforeUp, after: afterUp.page};

// 6. ctrl+wheel (zoom gesture) must not turn pages
const beforeCtrl = (await state()).page;
await scrollToBottom();
await wheel(120, {modifiers: 2});   // 2 = Ctrl
const afterCtrl = await state();
mark('ctrl-wheel-ignored', {before: beforeCtrl, after: afterCtrl.page});
out.ctrlWheel = {before: beforeCtrl, after: afterCtrl.page};

// 7. keyboard
const beforeKey = (await state()).page;
await ev(`document.querySelector('#pdf-scroll').focus()`);
await key('ArrowRight', 'ArrowRight');
const afterRight = await state();
await key('ArrowLeft', 'ArrowLeft');
const afterLeft = await state();
mark('keyboard-arrows', {before: beforeKey, right: afterRight.page, left: afterLeft.page});
out.keyboard = {before: beforeKey, right: afterRight.page, left: afterLeft.page};

// 8. page-number input, clamping at the last page
await ev(`(() => { const i = document.querySelector('#page-input'); i.value = '3'; i.dispatchEvent(new Event('change', {bubbles: true})); return true; })()`);
await sleep(800);
const afterInput = await state();
await ev(`(() => { const i = document.querySelector('#page-input'); i.value = '9999'; i.dispatchEvent(new Event('change', {bubbles: true})); return true; })()`);
await sleep(900);
const afterClamp = await state();
mark('page-input-and-clamp', {input: afterInput.page, clamped: afterClamp.page, total: afterClamp.total});
out.pageInput = {afterInput: afterInput.page, clamped: afterClamp.page, total: afterClamp.total};

// 9. first page: no backwards step, wheel up must not scroll away or go negative
await ev(`(() => { const i = document.querySelector('#page-input'); i.value = '1'; i.dispatchEvent(new Event('change', {bubbles: true})); return true; })()`);
await sleep(900);
const firstPage = await state();
await wheel(-120);
await sleep(200);
const stillFirst = await state();
mark('first-page-boundary', {page: stillFirst.page, scrollTop: stillFirst.scrollTop, prevDisabled: stillFirst.prevDisabled});
out.firstPage = {state: stillFirst, beforeScrollTop: firstPage.scrollTop};

// 10. rapid paging: six gestures spaced past the debounce lock, then a burst of
// keyboard turns on top, so several renders are cancelled in flight.
for (let i = 0; i < 6; i++) { await scrollToBottom(); await wheel(120, {wait: 950}); }
const afterSix = await state();
for (let i = 0; i < 4; i++) { await key('ArrowRight', 'ArrowRight'); }
await sleep(2500);
const afterRapid = await state();
mark('rapid-paging-consistency', {afterSix: afterSix.page, page: afterRapid.page,
                                  canvasW: afterRapid.canvasW, layerW: afterRapid.layerW});
out.rapid = {page: afterRapid.page, canvas: [afterRapid.canvasW, afterRapid.canvasH],
             layer: [afterRapid.layerW, afterRapid.layerH], announcer: afterRapid.announcer,
             afterSix: afterSix.page};

// 11. manual zoom allows scrolling the overflow; page stays put on wheel at the edges
await ev(`(() => { const s = document.querySelector('#zoom-select'); s.value = '1.5'; s.dispatchEvent(new Event('change', {bubbles: true})); return true; })()`);
await sleep(1200);
const zoomed = await state();
const pageBeforeZoomWheel = zoomed.page;
// warm-up: the first wheel after a zoom change lands while the new layout settles
await wheel(120);
const zoomedWheel = await state();
await wheel(120);
const zoomedWheel2 = await state();
mark('manual-zoom-scrolls', {zoomOverflow: zoomed.scrollOverflow, pageBefore: pageBeforeZoomWheel,
                             pageAfterWheel: zoomedWheel.page, scrollTopAfterFirst: zoomedWheel.scrollTop,
                             pageAfterSecond: zoomedWheel2.page, scrollTopAfterSecond: zoomedWheel2.scrollTop});
out.manualZoom = {overflow: zoomed.scrollOverflow, pageBefore: pageBeforeZoomWheel,
                  page: zoomedWheel.page, scrollTop: zoomedWheel.scrollTop,
                  pageAfterSecond: zoomedWheel2.page, scrollTopAfterSecond: zoomedWheel2.scrollTop,
                  scrollClass: zoomed.scrollClass};
await ev(`(() => { const s = document.querySelector('#zoom-select'); s.value = 'page'; s.dispatchEvent(new Event('change', {bubbles: true})); return true; })()`);
await sleep(1200);
out.backToFitPage = await state();

// 12. selection + annotation: highlight layer must render on the right page
await ev(`(() => { const i = document.querySelector('#page-input'); i.value = '2'; i.dispatchEvent(new Event('change', {bubbles: true})); return true; })()`);
await sleep(1200);
out.selection = await ev(`(() => {
  const layer = document.querySelector('#text-layer');
  const spans = [...layer.querySelectorAll('span')].filter(s => (s.textContent || '').trim().length > 4);
  if (!spans.length) return {ok: false};
  const range = document.createRange();
  range.selectNodeContents(spans[0]);
  const sel = window.getSelection();
  sel.removeAllRanges(); sel.addRange(range);
  const quote = sel.toString().trim();
  document.querySelector('#selection-note').click();
  return {ok: true, quote: quote.slice(0, 24), dialogOpen: document.querySelector('#annotation-dialog').open};
})()`);
await sleep(600);
out.annotation = await ev(`(() => {
  const note = document.querySelector('#annotation-note');
  note.value = '阶段 7 选择测试';
  document.querySelector('#annotation-form').dispatchEvent(new Event('submit', {bubbles: true, cancelable: true}));
  return true;
})()`);
await sleep(1200);
out.afterAnnotation = await state();
out.annotationRects = await ev(`(() => {
  const marks = [...document.querySelectorAll('#highlight-layer .pdf-highlight')];
  return {count: marks.length, first: marks[0] ? marks[0].getAttribute('style') : null};
})()`);
out.paper = paper;

// 13. resize: fit-page must re-fit and keep canvas == text layer
const sizes = [[1920, 1080], [1280, 800], [1366, 768]];
out.resize = [];
for (const [w, h] of sizes) {
  await send('Emulation.setDeviceMetricsOverride', {width: w, height: h, deviceScaleFactor: 1, mobile: false});
  await sleep(1600);
  const s = await state();
  out.resize.push({size: [w, h], canvasW: s.canvasW, canvasH: s.canvasH, layerW: s.layerW, layerH: s.layerH,
                   overflow: s.scrollOverflow, page: s.page});
}
// 14. narrow layout: the notes panel must move below the PDF panel
await send('Emulation.setDeviceMetricsOverride', {width: 900, height: 900, deviceScaleFactor: 1, mobile: false});
await sleep(1600);
out.narrow = await ev(`(() => {
  const pdf = document.querySelector('.pdf-panel').getBoundingClientRect();
  const notes = document.querySelector('.notes-panel').getBoundingClientRect();
  return {pdf: [Math.round(pdf.x), Math.round(pdf.y), Math.round(pdf.width)],
          notes: [Math.round(notes.x), Math.round(notes.y), Math.round(notes.width)],
          stacked: notes.y >= pdf.y + pdf.height - 2, notesWidth: Math.round(notes.width), pdfWidth: Math.round(pdf.width)};
})()`);

const shot = await send('Page.captureScreenshot', {format: 'png'});
out.screenshotData = shot.data;
console.log(JSON.stringify(out, null, 2));
ws.close();
"""


class ReaderPaging(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="phase7-"))
        fresh_library_v1(cls.tmp)
        cls.store = server.Store(cls.tmp)
        cls.manager = analysis.AnalysisManager(
            cls.store, provider_factory=lambda s: providers.DisabledAdapter(),
            log=lambda record: None)
        cls.sessions = session.SessionStore()
        cls.port = free_port()
        cls.httpd = server.BoundedHTTPServer(("127.0.0.1", cls.port),
                                               server.handler_for(cls.store, "",
                                                                  cls.manager, sessions=cls.sessions))
        cls.httpd.daemon_threads = True
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_paging_and_fit_page(self):
        report = self._run_driver()
        OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        initial = report["initial"]

        # 1. default zoom: whole page when it stays readable, otherwise fit-width (B1)
        self.assertIn(initial["zoomValue"], ("page", "width"))
        if initial["zoomValue"] == "page":
            self.assertEqual("pdf-scroll page-fits", initial["scrollClass"])
            self.assertEqual(0, initial["scrollOverflow"], "a fitted page must not scroll")
            self.assertLessEqual(initial["canvasH"], initial["scrollH"],
                                 "the whole page must fit the visible area")
            self.assertLessEqual(initial["canvasW"], initial["scrollW"])
        else:
            self.assertEqual("pdf-scroll", initial["scrollClass"],
                             "fit-width means the page may scroll")
            self.assertLessEqual(initial["canvasW"], initial["scrollW"],
                                 "fit-width must not exceed the pane width")
        self.assertIn("第 1 页", initial["announcer"])

        # 2-3. wheel: one page per gesture, measured from the page edge
        wheel = report["wheelForward"]
        self.assertEqual(report["steps"][1]["before"] + 1, wheel["afterOne"]["page"])
        self.assertEqual(wheel["afterOne"]["page"] + 1, wheel["afterTwo"]["page"])
        self.assertEqual(wheel["afterTwo"]["page"] + 1, wheel["afterThree"]["page"])
        if report["edgeScroll"]["needed"]:
            self.assertGreater(initial["scrollOverflow"], 0,
                               "fit-width is expected to scroll before turning pages")

        # 4. trackpad-sized moves change nothing
        self.assertEqual(report["tinyDelta"]["before"], report["tinyDelta"]["after"])

        # 5. a burst inside the debounce window turns exactly one page
        self.assertEqual(report["burst"]["before"] + 1, report["burst"]["after"])

        # 6. wheel up returns one page
        self.assertEqual(report["wheelBack"]["before"] - 1, report["wheelBack"]["after"])

        # 7. ctrl+wheel (zoom gesture) is left alone
        self.assertEqual(report["ctrlWheel"]["before"], report["ctrlWheel"]["after"])

        # 8. keyboard arrows
        self.assertEqual(report["keyboard"]["before"] + 1, report["keyboard"]["right"])
        self.assertEqual(report["keyboard"]["right"] - 1, report["keyboard"]["left"])

        # 9. page input and clamping at the last page
        self.assertEqual(3, report["pageInput"]["afterInput"])
        self.assertEqual(report["pageInput"]["total"], report["pageInput"]["clamped"])
        self.assertTrue(report["pageInput"]["total"] > 3)

        # 10. first page: no backward step, no leftover scroll
        self.assertEqual(1, report["firstPage"]["state"]["page"])
        self.assertTrue(report["firstPage"]["state"]["prevDisabled"])
        self.assertEqual(0, report["firstPage"]["state"]["scrollTop"])

        # 11. rapid paging: canvas, text layer and page number agree afterwards
        rapid = report["rapid"]
        start = report["steps"][0]["page"]
        self.assertEqual(start + 6, rapid["afterSix"], "six spaced gestures must each turn a page")
        self.assertEqual(start + 10, rapid["page"], "four more turns must each turn a page")
        self.assertEqual(rapid["canvas"], rapid["layer"],
                         "canvas and text layer must have the same geometry after rapid paging")
        self.assertIn(f"第 {rapid['page']} 页", rapid["announcer"])

        # 12. manual zoom scrolls the overflow instead of turning pages
        zoom = report["manualZoom"]
        self.assertGreater(zoom["overflow"], 0, "125%/150% must be allowed to overflow")
        self.assertNotIn("page-fits", zoom["scrollClass"],
                         "a zoomed page must be scrollable, not clipped")
        if report["backToFitPage"]["zoomValue"] == "page":
            self.assertEqual("pdf-scroll page-fits", report["backToFitPage"]["scrollClass"])
        self.assertGreater(zoom["scrollTopAfterSecond"], 0,
                           "the wheel must scroll the zoomed page")
        self.assertEqual(zoom["pageBefore"], zoom["page"],
                         "scrolling inside a zoomed page must not turn it")
        self.assertEqual(zoom["pageBefore"], zoom["pageAfterSecond"])
        if report["backToFitPage"]["zoomValue"] == "page":
            self.assertEqual(0, report["backToFitPage"]["scrollOverflow"])
        else:
            self.assertEqual("width", report["backToFitPage"]["zoomValue"])

        # 13. text selection still produces an annotation with highlight rects
        self.assertTrue(report["selection"]["ok"])
        self.assertGreater(len(report["selection"]["quote"]), 0)
        self.assertTrue(report["selection"]["dialogOpen"])
        self.assertGreater(report["annotationRects"]["count"], 0,
                           "the highlight layer must render the new annotation")
        self.assertEqual(2, report["afterAnnotation"]["page"])

        # 14. resize keeps canvas and text layer identical at every viewport
        for row in report["resize"]:
            self.assertEqual([row["canvasW"], row["canvasH"]], [row["layerW"], row["layerH"]],
                             f"mismatch at {row['size']}")

        # 15. narrow screens stack the panels
        narrow = report["narrow"]
        self.assertTrue(narrow["stacked"], "the notes panel must move below the PDF on narrow screens")
        self.assertGreater(narrow["pdfWidth"], 300)

    def _fragment_url(self):
        """A URL whose fragment carries a one-time capability, like the launcher's."""
        capability = self.sessions.issue_bootstrap(source="browser-test")
        return f"http://127.0.0.1:{self.port}/#bootstrap={capability}"

    def _run_driver(self):
        profile = Path(tempfile.mkdtemp(prefix="cdp-phase7-"))
        driver = Path(tempfile.gettempdir()) / "phase7_paging_driver.mjs"
        driver.write_text(DRIVER, encoding="utf-8")
        debug_port = free_port()
        proc = subprocess.Popen([CHROME, "--headless=new", f"--remote-debugging-port={debug_port}",
                                 f"--user-data-dir={profile}", "--no-first-run",
                                 "--no-default-browser-check", "--hide-scrollbars",
                                 "--window-size=1366,768", "about:blank"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(80):
                try:
                    with urlopen(f"http://127.0.0.1:{debug_port}/json/version", timeout=1) as response:
                        json.load(response)
                    break
                except Exception:                       # noqa: BLE001
                    time.sleep(0.25)
            site = self._fragment_url()
            run = subprocess.run(["node", str(driver), site, str(debug_port), PAPER],
                                 capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", timeout=600)
            if not (run.stdout or "").strip().startswith("{"):
                self.fail("driver produced no JSON:\n" + (run.stdout or "") + (run.stderr or "")[:3000])
            report = json.loads(run.stdout)
            shot = report.pop("screenshotData", None)
            if shot:
                (Path(__file__).resolve().parent / "phase7-fit-page.png").write_bytes(
                    base64.b64decode(shot))
            return report
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
