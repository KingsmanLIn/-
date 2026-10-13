"""Phase 8 acceptance: the reader's PDF behaviour, in a real Chrome.

Covers the last acceptance item that is browser-only: fit-page plus wheel paging plus
keyboard plus page-number restore, all in one session and together with the older
features (selection, highlight, notes, export).

    python -m unittest discover -s tests -p "test_acceptance_browser.py"
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
ZH_PAPER = "medical-2026-zang"
OUT = Path(__file__).resolve().parent / "phase8-reader-report.json"

DRIVER = r"""
const url = process.argv[2], port = process.argv[3];
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
await send('Runtime.enable'); await send('Page.enable');
const ev = async (expression) => {
  const r = await send('Runtime.evaluate', {expression, awaitPromise: true, returnByValue: true});
  if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails.exception?.description || r.exceptionDetails.text));
  return r.result.value;
};
const sleep = ms => new Promise(r => setTimeout(r, ms));
const out = {};
const state = () => ev('(() => { const s = document.querySelector("#pdf-scroll"); return {page: Number(document.querySelector("#page-input").value), overflow: s.scrollHeight - s.clientHeight, scrollTop: s.scrollTop, canvas: [Math.round(document.querySelector("#pdf-canvas").getBoundingClientRect().width), Math.round(document.querySelector("#pdf-canvas").getBoundingClientRect().height)], layer: [Math.round(document.querySelector("#text-layer").getBoundingClientRect().width), Math.round(document.querySelector("#text-layer").getBoundingClientRect().height)], announcer: document.querySelector("#page-announcer").textContent, highlights: document.querySelectorAll("#highlight-layer .pdf-highlight").length}; })()');
const wheel = async (dy, wait = 850) => {
  const box = await ev('(() => { const r = document.querySelector("#pdf-scroll").getBoundingClientRect(); return {x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2)}; })()');
  await send('Input.dispatchMouseEvent', {type: 'mouseWheel', x: box.x, y: box.y, deltaX: 0, deltaY: dy, pointerType: 'mouse'});
  await sleep(wait);
};
const key = async (k) => {
  const map = {ArrowRight: 39, ArrowLeft: 37};
  await send('Input.dispatchKeyEvent', {type: 'keyDown', code: k, key: k, windowsVirtualKeyCode: map[k]});
  await send('Input.dispatchKeyEvent', {type: 'keyUp', code: k, key: k, windowsVirtualKeyCode: map[k]});
  await sleep(750);
};
const setPage = async (n) => {
  await ev('(() => { const i = document.querySelector("#page-input"); i.value = "' + n + '"; i.dispatchEvent(new Event("change", {bubbles: true})); return true; })()');
  await sleep(1000);
};

await send('Page.navigate', {url});
await new Promise(done => setTimeout(done, 800));
await waitForSession();
await sleep(7000);
await send('Emulation.setDeviceMetricsOverride', {width: 1366, height: 768, deviceScaleFactor: 1, mobile: false});
await sleep(1500);

out.fitPage = await state();
out.fitPageZoom = await ev('document.querySelector("#zoom-select").value');

// wheel paging: in fit-width the page scrolls first, so use the page edge
const scrollToBottom = () => ev('(() => { const s = document.querySelector("#pdf-scroll"); s.scrollTop = s.scrollHeight; return s.scrollTop; })()');
const scrollToTop = () => ev('(() => { const s = document.querySelector("#pdf-scroll"); s.scrollTop = 0; return s.scrollTop; })()');
out.initialZoom = await ev('document.querySelector("#zoom-select").value');
out.initialOverflow = (await state()).overflow;
await scrollToBottom();
await wheel(120);
out.afterWheelDown = await state();
await scrollToTop();
await wheel(-120);
out.afterWheelUp = await state();

// keyboard paging
await key('ArrowRight');
out.afterArrowRight = await state();

// page-number input and the restore of lastPage
await setPage(5);
out.afterInput = await state();
const savedLastPage = await ev('(async () => { const r = await fetch("/api/state", {credentials: "same-origin"}); const s = await r.json(); const p = s.papers.find(x => x.id === "' + '__PAPER__' + '"); return p.lastPage; })()');
out.savedLastPage = savedLastPage;

// switch away and back: the page must be restored
await ev('document.querySelector("[data-paper=\\"finance-2021-zhang\\"]").click()');
await sleep(3000);
await ev('document.querySelector("[data-paper=\\"' + '__PAPER__' + '\\"]").click()');
await sleep(3500);
out.afterPaperSwitch = await state();

// full reload: lastPage comes from the library, not from memory
await send('Page.navigate', {url});
await sleep(7000);
out.afterReload = await state();

// selection -> annotation -> highlight, on the restored page
out.selection = await ev('(() => { const layer = document.querySelector("#text-layer"); const spans = [...layer.querySelectorAll("span")].filter(s => (s.textContent || "").trim().length > 4); if (!spans.length) return {ok: false}; const range = document.createRange(); range.selectNodeContents(spans[0]); const sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(range); document.querySelector("#selection-note").click(); return {ok: true, dialog: document.querySelector("#annotation-dialog").open, quote: sel.toString().trim().slice(0, 20)}; })()');
await sleep(500);
await ev('(() => { document.querySelector("#annotation-note").value = "一体化验收批注"; document.querySelector("#annotation-form").dispatchEvent(new Event("submit", {bubbles: true, cancelable: true})); return true; })()');
await sleep(1500);
out.afterAnnotation = await state();
out.annotationPersisted = await ev('(async () => { const r = await fetch("/api/state", {credentials: "same-origin"}); const s = await r.json(); const p = s.papers.find(x => x.id === "' + '__PAPER__' + '"); return p.annotations.length; })()');

// reading card still works next to the PDF, and the AI answer stays hidden until clicked
await ev('document.querySelector("[data-tab=\\"card\\"]").click()');
await sleep(700);
out.card = await ev('(() => ({fields: document.querySelectorAll(".card-field").length, aiButtons: document.querySelectorAll("[data-ai-field]").length, panels: document.querySelectorAll(".ai-panel").length, statusChip: document.querySelector(".ai-chip") ? document.querySelector(".ai-chip").textContent : null}))()');

// manual fields survive a reload
await ev('document.querySelector("[data-tab=\\"steps\\"]").click()');
await sleep(400);
await ev('(() => { const t = document.querySelector("#field-question"); t.value = "验收：人工记录"; t.dispatchEvent(new Event("input", {bubbles: true})); return true; })()');
await sleep(1500);
await send('Page.navigate', {url});
await sleep(7000);
await ev('document.querySelector("[data-tab=\\"steps\\"]").click()');
await sleep(500);
out.manualAfterReload = await ev('document.querySelector("#field-question") ? document.querySelector("#field-question").value : null');
out.finalPage = (await state()).page;

const shot = await send('Page.captureScreenshot', {format: 'png'});
out.screenshotData = shot.data;
console.log(JSON.stringify(out, null, 2));
ws.close();
"""


class AcceptanceBrowser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="phase8-browser-"))
        fresh_library_v1(cls.tmp)
        for leftover in (cls.tmp / "analyses").glob("*.json"):
            leftover.unlink()
        cls.store = server.Store(cls.tmp)
        cls.store.update_ai_settings({"provider": "none", "model": "", "auto_analyze": False})
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

    def test_reader_behaviour_end_to_end(self):
        report = self._run_driver()
        OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

        # 7a. default zoom: whole page when readable, otherwise fit-width (review fix B1)
        self.assertIn(report["fitPageZoom"], ("page", "width"))
        if report["fitPageZoom"] == "page":
            self.assertEqual(0, report["fitPage"]["overflow"])
        self.assertEqual(report["fitPage"]["canvas"], report["fitPage"]["layer"])

        # 7b. wheel and keyboard paging
        self.assertIn(report["initialZoom"], ("page", "width"))
        self.assertEqual(report["fitPage"]["page"] + 1, report["afterWheelDown"]["page"])
        self.assertEqual(report["fitPage"]["page"], report["afterWheelUp"]["page"])
        self.assertEqual(report["fitPage"]["page"] + 1, report["afterArrowRight"]["page"])

        # 7c. page-number input, persistence and restore
        self.assertEqual(5, report["afterInput"]["page"])
        self.assertEqual(5, report["savedLastPage"], "lastPage must be written to the library")
        self.assertEqual(5, report["afterPaperSwitch"]["page"],
                         "switching papers and back must restore the reading position")
        self.assertEqual(5, report["afterReload"]["page"],
                         "a full reload must restore lastPage from the library")
        self.assertIn("第 5 页", report["afterReload"]["announcer"])

        # 7d. selection, annotation and highlight still work on the restored page
        self.assertTrue(report["selection"]["ok"])
        self.assertTrue(report["selection"]["dialog"])
        self.assertGreaterEqual(report["afterAnnotation"]["highlights"], 1,
                                "the highlight layer must show the new annotation")
        self.assertEqual(1, report["annotationPersisted"])

        # 7e. the reading card is intact and the AI answer stays hidden
        self.assertEqual(13, report["card"]["fields"])
        self.assertEqual(13, report["card"]["aiButtons"])
        self.assertEqual(0, report["card"]["panels"], "no AI panel before a click")
        self.assertIsNotNone(report["card"]["statusChip"])

        # 7f. manual records survive the reload
        self.assertEqual("验收：人工记录", report["manualAfterReload"])
        self.assertEqual(5, report["finalPage"])

    def _fragment_url(self):
        """A URL whose fragment carries a one-time capability, like the launcher's."""
        capability = self.sessions.issue_bootstrap(source="browser-test")
        return f"http://127.0.0.1:{self.port}/#bootstrap={capability}"

    def _run_driver(self):
        profile = Path(tempfile.mkdtemp(prefix="cdp-phase8-"))
        driver = Path(tempfile.gettempdir()) / "phase8_reader_driver.mjs"
        driver.write_text(DRIVER.replace("__PAPER__", ZH_PAPER), encoding="utf-8")
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
            run = subprocess.run(["node", str(driver), site, str(debug_port)],
                                 capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", timeout=600)
            if not (run.stdout or "").strip().startswith("{"):
                self.fail("driver produced no JSON:\n" + (run.stdout or "") + (run.stderr or "")[:3000])
            report = json.loads(run.stdout)
            shot = report.pop("screenshotData", None)
            if shot:
                (Path(__file__).resolve().parent / "phase8-reader.png").write_bytes(
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
