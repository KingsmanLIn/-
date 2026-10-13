"""Phase 10 checks: the reader's vertical budget and the collapsible notes column.

    python -m unittest discover -s tests -p "test_reader_focus.py"
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
OUT = Path(__file__).resolve().parent / "phase10-reader-report.json"

DRIVER = r"""
const url = process.argv[2], port = process.argv[3];
const list = await (await fetch('http://127.0.0.1:' + port + '/json/list')).json();
const page = list.find(t => t.type === 'page');
const ws = new WebSocket(page.webSocketDebuggerUrl);
let id = 0; const pending = new Map(); const errors = [];
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

ws.addEventListener('message', ev => {
  const m = JSON.parse(ev.data);
  if (m.id && pending.has(m.id)) { const {res, rej} = pending.get(m.id); pending.delete(m.id); m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result); return; }
  if (m.method === 'Runtime.consoleAPICalled' && m.params.type === 'error') errors.push(m.params.args.map(a => a.value || a.description || '').join(' '));
});
await new Promise(r => ws.addEventListener('open', r));
await send('Runtime.enable'); await send('Page.enable');
const ev = async (e) => { const r = await send('Runtime.evaluate', {expression: e, awaitPromise: true, returnByValue: true}); if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails.exception?.description)); return r.result.value; };
const sleep = ms => new Promise(r => setTimeout(r, ms));
await send('Page.navigate', {url});
await new Promise(done => setTimeout(done, 800));
await waitForSession();
await sleep(7500);
await send('Emulation.setDeviceMetricsOverride', {width: 1366, height: 768, deviceScaleFactor: 1, mobile: false});
await sleep(1500);

const measure = () => ev(`(() => {
  const rect = (sel) => { const el = document.querySelector(sel); if (!el) return null; const r = el.getBoundingClientRect(); return {top: Math.round(r.top), h: Math.round(r.height), w: Math.round(r.width)}; };
  const scroll = document.querySelector('#pdf-scroll');
  const canvas = document.querySelector('#pdf-canvas').getBoundingClientRect();
  const spans = [...document.querySelectorAll('#text-layer span')].filter(s => (s.textContent || '').trim().length > 6);
  const px = spans.map(s => parseFloat(getComputedStyle(s).fontSize)).filter(v => v > 0).sort((a, b) => a - b);
  const parts = {topbar: rect('.topbar'), header: rect('.paper-strip'), ribbon: rect('.reading-ribbon'),
                 toolbar: rect('.pdf-toolbar'), subbar: rect('.pdf-subbar'), footer: rect('.pdf-footer')};
  return {
    viewport: [innerWidth, innerHeight],
    chrome: Object.values(parts).reduce((sum, p) => sum + (p ? p.h : 0), 0),
    parts, pdf: rect('.pdf-panel'), notes: rect('.notes-panel'), scroll: rect('#pdf-scroll'),
    canvas: [Math.round(canvas.width), Math.round(canvas.height)],
    medianFontPx: px.length ? Math.round(px[Math.floor(px.length / 2)] * 100) / 100 : null,
    wholePage: (scroll.scrollHeight - scroll.clientHeight) <= 2,
    zoom: document.querySelector('#zoom-select').value,
    collapsed: document.querySelector('.workspace').classList.contains('notes-collapsed'),
    buttonText: document.querySelector('#toggle-notes').textContent.trim(),
    notesVisible: (() => { const n = document.querySelector('.notes-panel'); return n ? getComputedStyle(n).display !== 'none' : false; })(),
  };
})()`);

const out = {};
out.wide = await measure();
out.toolbarHasToggle = await ev("!!document.querySelector('#toggle-notes')");

// collapse: the PDF must take the whole workspace and the notes column must disappear
await ev("document.querySelector('#toggle-notes').click()");
await sleep(1400);
out.collapsed = await measure();
out.manualFieldsStillSaved = await ev(`(async () => {
  const t = document.querySelector('[data-tab="steps"]');
  return true;
})()`);

// the choice survives a reload
await send('Page.navigate', {url});
await sleep(7000);
out.afterReload = await measure();

// expand again
await ev("document.querySelector('#toggle-notes').click()");
await sleep(1400);
out.expandedAgain = await measure();

// records stay editable while collapsed
await ev("document.querySelector('#toggle-notes').click()");
await sleep(900);
await ev("(() => { const el = document.querySelector('[data-tab=\"steps\"]'); if (el) el.click(); return true; })()");
await sleep(400);
out.notesHiddenButReachable = await ev("(() => { const n = document.querySelector('.notes-panel'); return n ? getComputedStyle(n).display : null; })()");
await ev("document.querySelector('#toggle-notes').click()");
await sleep(1200);
await ev("(() => { const el = document.querySelector('[data-tab=\"steps\"]'); if (el) el.click(); return true; })()");
await sleep(400);
out.afterExpand = await ev(`(() => {
  const field = document.querySelector('#field-question');
  if (!field) return {ok: false};
  field.value = '阶段 10 记录测试';
  field.dispatchEvent(new Event('input', {bubbles: true}));
  return {ok: true, value: field.value};
})()`);
await sleep(1500);
out.savedValue = await ev(`(async () => {
  
  const id = localStorage.getItem('paper-reader:last') || '';
  const r = await fetch('/api/state', {credentials: 'same-origin'});
  const s = await r.json();
  const p = s.papers.find(x => x.id === id) || s.papers[0];
  return p ? p.fields.question : null;
})()`);

const shot = await send('Page.captureScreenshot', {format: 'png'});
out.expandedShot = shot.data;
await ev("document.querySelector('#toggle-notes').click()");
await sleep(1400);
const shot2 = await send('Page.captureScreenshot', {format: 'png'});
out.collapsedShot = shot2.data;
out.errors = errors;
console.log(JSON.stringify(out, null, 2));
ws.close();
"""


class ReaderFocus(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="phase10-"))
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

    def test_vertical_focus_and_collapse(self):
        report = self._run_driver()
        OUT.write_text(json.dumps({k: v for k, v in report.items() if "Shot" not in k},
                                  ensure_ascii=False, indent=2), encoding="utf-8")

        wide = report["wide"]
        # the chrome above the page must stay small, and the pane must dominate the height
        self.assertLessEqual(wide["chrome"], 300, f"chrome above the page: {wide['chrome']}px")
        self.assertGreaterEqual(wide["scroll"]["h"] / wide["viewport"][1], 0.60,
                                f"pane is only {wide['scroll']['h']}px of {wide['viewport'][1]}")
        self.assertGreaterEqual(wide["scroll"]["h"], 500)
        self.assertGreaterEqual(wide["medianFontPx"], 9.0,
                                "the default view must show readable text")
        self.assertTrue(report["toolbarHasToggle"])

        # collapsing hands the whole workspace width to the page
        collapsed = report["collapsed"]
        self.assertTrue(collapsed["collapsed"])
        self.assertFalse(collapsed["notesVisible"], "the notes column must be hidden")
        self.assertGreater(collapsed["pdf"]["w"], wide["pdf"]["w"] + 200,
                           "collapsing must widen the source pane")
        self.assertGreater(collapsed["canvas"][0], wide["canvas"][0],
                           "the page itself must get wider")
        self.assertIn("展开", collapsed["buttonText"])

        # the choice is remembered
        self.assertTrue(report["afterReload"]["collapsed"])
        self.assertFalse(report["afterReload"]["notesVisible"])
        self.assertFalse(report["expandedAgain"]["collapsed"])
        self.assertTrue(report["expandedAgain"]["notesVisible"])

        # records stay editable once the column is back
        self.assertTrue(report["afterExpand"]["ok"])
        self.assertEqual("阶段 10 记录测试", report["savedValue"],
                         "the reading card must save normally after collapsing/expanding")
        self.assertEqual([], report["errors"], report["errors"])

    def _fragment_url(self):
        """A URL whose fragment carries a one-time capability, like the launcher's."""
        capability = self.sessions.issue_bootstrap(source="browser-test")
        return f"http://127.0.0.1:{self.port}/#bootstrap={capability}"

    def _run_driver(self):
        profile = Path(tempfile.mkdtemp(prefix="cdp-phase10-"))
        driver = Path(tempfile.gettempdir()) / "phase10_driver.mjs"
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
            run = subprocess.run(["node", str(driver), site, str(debug_port)],
                                 capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", timeout=400)
            if not (run.stdout or "").strip().startswith("{"):
                self.fail("driver produced no JSON:\n" + (run.stdout or "") + (run.stderr or "")[:2000])
            report = json.loads(run.stdout)
            for key, name in (("expandedShot", "phase10-reader-expanded.png"),
                              ("collapsedShot", "phase10-reader-collapsed.png")):
                shot = report.pop(key, None)
                if shot:
                    (Path(__file__).resolve().parent / name).write_bytes(base64.b64decode(shot))
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
