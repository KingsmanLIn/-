"""Phase 6 browser test: the reading card must not fetch AI answers on its own.

Drives a real Chrome over CDP against a throwaway server with a fake provider, counts
requests to /api/analysis/... and checks the click, expand, copy and page-jump flows.

    python tests/test_card_browser.py
"""
from __future__ import annotations

import json
import shutil
import socket
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
from card_fixtures import build_card  # noqa: E402
from storage import DataStore  # noqa: E402

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PAPER = "medical-2026-zang"
DRIVER = r"""
const url = process.argv[2], port = process.argv[3], paper = process.argv[4];
const list = await (await fetch('http://127.0.0.1:' + port + '/json/list')).json();
const page = list.find(t => t.type === 'page') || list.find(t => t.type === 'tab');
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

const events = [];
const savedBodies = [];
const consoleErrors = [];
ws.addEventListener('message', ev => {
  const m = JSON.parse(ev.data);
  if (m.id && pending.has(m.id)) { const {res, rej} = pending.get(m.id); pending.delete(m.id); m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result); return; }
  if (m.method === 'Network.requestWillBeSent' && m.params.request.url.includes('/api/analysis')) {
    events.push({url: m.params.request.url, method: m.params.request.method});
  }
  if (m.method === 'Runtime.consoleAPICalled' && m.params.type === 'error') {
    consoleErrors.push(m.params.args.map(a => a.value || a.description || '').join(' '));
  }
  if (m.method === 'Runtime.exceptionThrown') {
    consoleErrors.push('exception: ' + (m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text));
  }
  if (m.method === 'Network.requestWillBeSent' && m.params.request.url.endsWith('/api/save')) {
    savedBodies.push(m.params.request.postData || '');
  }
});
await new Promise(r => ws.addEventListener('open', r));
await send('Runtime.enable'); await send('Page.enable'); await send('Network.enable');
const ev = async (expr) => {
  const r = await send('Runtime.evaluate', {expression: expr, awaitPromise: true, returnByValue: true});
  if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails.exception?.description || r.exceptionDetails.text));
  return r.result.value;
};
const sleep = ms => new Promise(r => setTimeout(r, ms));
const click = async (selector) => {
  const ok = await ev(`(() => { const el = document.querySelector(${JSON.stringify(selector)}); if (!el) return false; el.click(); return true; })()`);
  if (!ok) throw new Error('missing selector ' + selector);
  await sleep(350);
};
const out = {steps: []};
const mark = (name, extra = {}) => out.steps.push({name, requests: events.length, ...extra});

await send('Page.navigate', {url});
await new Promise(done => setTimeout(done, 800));
await waitForSession();
await sleep(7000);
const apiHits = () => events.filter(e => e.method === 'GET' && !e.url.endsWith('/status')).length;

mark('after-load', {apiHits: apiHits(), total: events.length});

// open the reading card
await click('[data-tab="card"]');
mark('after-opening-card', {apiHits: apiHits(), total: events.length});
out.cardHtmlLength = await ev(`document.querySelector('#notes-content').innerHTML.length`);
out.fieldCount = await ev(`document.querySelectorAll('.card-field').length`);
out.aiButtons = await ev(`document.querySelectorAll('[data-ai-field]').length`);
out.statusBarText = await ev(`document.querySelector('.ai-status-bar')?.innerText || ''`);

// make one field ready in the background
const runExpression = `(function(){
  return new Promise(function(resolve, reject){
    var xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/analysis/' + "__PAPER__" + '/generate');
    xhr.withCredentials = true;
    xhr.setRequestHeader('X-CSRF-Token', window.__csrf || '');
    xhr.setRequestHeader('Content-Type', 'application/json');
    xhr.onload = function(){ resolve(xhr.status); };
    xhr.onerror = function(){ reject(new Error('xhr failed')); };
    xhr.send(JSON.stringify({force: true}));
  });
})()`;
await ev(runExpression);
await sleep(4000);
const beforeClick = apiHits();

// click one field only
await click('[data-ai-field="summary"]');
mark('after-click-summary', {apiHits: apiHits(), delta: apiHits() - beforeClick});
out.openPanels = await ev(`document.querySelectorAll('.ai-panel').length`);
out.expandedButtons = await ev(`[...document.querySelectorAll('[data-ai-field]')].filter(b => b.getAttribute('aria-expanded') === 'true').map(b => b.dataset.aiField)`);
out.panelText = await ev(`document.querySelector('.ai-panel')?.innerText || ''`);
out.panelPagesSummary = await ev(`[...document.querySelectorAll('.ai-page')].map(b => b.textContent.trim())`);
out.panelHasPages = out.panelPagesSummary;
out.manualFieldsUntouched = await ev(`[...document.querySelectorAll('.card-field > p')].length`);

// switch to another field: only that one stays open
await click('[data-ai-field="data"]');
out.afterSwitch = {
  openPanels: await ev(`document.querySelectorAll('.ai-panel').length`),
  expanded: await ev(`[...document.querySelectorAll('[data-ai-field]')].filter(b => b.getAttribute('aria-expanded') === 'true').map(b => b.dataset.aiField)`),
  requests: apiHits(),
  pageButtons: await ev(`[...document.querySelectorAll('.ai-page')].map(b => b.dataset.jump)`),
  panelQuotes: await ev(`[...document.querySelectorAll('.ai-quote')].map(q => q.textContent.slice(0, 12))`),
};

// screenshot evidence: a field whose references carry a page and a quote
await ev("(() => { const el = document.querySelector('.ai-panel'); if (el) el.scrollIntoView({block: 'center'}); return true; })()");
await sleep(500);
const shot = await send('Page.captureScreenshot', {format: 'png'});
out.screenshot = 'phase6-card-ai-panel.png';
out.screenshotData = shot.data;

// evidence page jump uses the existing pager
out.pageBefore = await ev(`document.querySelector('#page-input').value`);
const jumped = await ev(`(() => { const b = document.querySelector('.ai-page'); if (!b) return null; const page = b.dataset.jump; b.click(); return page; })()`);
await sleep(1200);
out.jumpTarget = jumped;
out.pageAfter = await ev(`document.querySelector('#page-input').value`);
out.printedLabel = await ev(`document.querySelector('#printed-label').textContent`);

// copy is explicit and appends rather than overwriting: expand the field we read back
await click('[data-ai-field="summary"]');
await click('[data-ai-copy="summary"]');
await sleep(1200);
out.consoleErrors = consoleErrors;
// read the manual field from the steps tab textarea and from the save request body
await click('[data-tab="steps"]');
await click('[data-stage="3"]');
out.copy = {
  textareaValue: await ev(`document.querySelector('#field-summary')?.value || ''`),
  stepsChecked: await ev(`[...document.querySelectorAll('.stage-tab')].map(t => t.classList.contains('done'))`),
  annotationCount: await ev(`document.querySelector('#annotation-count')?.textContent || '0'`),
  saveBodies: savedBodies,
};
await click('[data-tab="card"]');

// switching papers must clear the AI panel state
await click('[data-paper="finance-2021-zhang"]');
await sleep(2500);
await click('[data-tab="card"]');
await sleep(700);
out.afterSwitchPaper = {
  openPanels: await ev(`document.querySelectorAll('.ai-panel').length`),
  statusText: await ev(`document.querySelector('.ai-status-bar')?.innerText || ''`),
  answerInDom: await ev(`document.querySelector('#notes-content').innerText.includes('AI 参考')`),
  requests: apiHits(),
};

// reload: manual records survive, AI expansion is gone by design.
// Go back to the paper that carries the manual record first, so the reload restores it.
await click('[data-paper="' + paper + '"]');
await sleep(2500);
out.beforeReload = {
  openPanels: await ev(`document.querySelectorAll('.ai-panel').length`),
  requests: apiHits(),
};
await send('Page.navigate', {url});
await sleep(7000);
await click('[data-tab="card"]');
await sleep(500);
out.afterReload = {
  openPanels: await ev(`document.querySelectorAll('.ai-panel').length`),
  summaryText: await ev(`(() => { const fields = [...document.querySelectorAll('.card-field')]; const f = fields.find(x => x.querySelector('[data-ai-field="summary"]')); return f ? f.querySelector('p').textContent : null; })()`),
  manualFieldInSteps: await ev(`(async () => { document.querySelector('[data-tab="steps"]').click(); await new Promise(r => setTimeout(r, 200)); document.querySelector('[data-stage="3"]').click(); await new Promise(r => setTimeout(r, 200)); return document.querySelector('#field-summary')?.value || ''; })()`),
  requests: apiHits(),
};
out.hits = events;
console.log(JSON.stringify(out, null, 2));
ws.close();
"""


def card_for(paper_id):
    directory = Path(tempfile.mkdtemp(prefix="phase6-card-"))
    extractor = __import__("extraction").PdfTextExtractor(
        files=DataStore(directory).ensure_directories(), prefer=["builtin"])
    result = extractor.extract(PROJECT_ROOT / "data" / "papers" / f"{paper_id}.pdf",
                               paper_id=paper_id)
    return build_card(result.pages, not_found=("robustness", "summary"))


class CardBrowser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="phase6-"))
        fresh_library_v1(cls.tmp)
        # start from a clean slate: no analysis answers, no extracted cache
        for leftover in (cls.tmp / "analyses").glob("*.json"):
            leftover.unlink()
        for leftover in (cls.tmp / "extracted").glob("*.json"):
            leftover.unlink()
        cls.store = server.Store(cls.tmp)
        cls.store.update_ai_settings({"provider": "ollama", "model": "fake-model",
                                      "auto_analyze": False})
        cls.fake = providers.FakeProvider(script=["success"], payload=card_for(PAPER))
        cls.manager = analysis.AnalysisManager(cls.store,
                                               provider_factory=lambda settings: cls.fake,
                                               log=lambda record: None)
        cls.manager.start()
        cls.sessions = session.SessionStore()
        cls.port = free_port()
        cls.httpd = server.BoundedHTTPServer(("127.0.0.1", cls.port),
                                               server.handler_for(cls.store, "",
                                                                  cls.manager, sessions=cls.sessions))
        cls.httpd.daemon_threads = True
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        # a manual record so the reload check has something to find
        paper = cls.store.paper(PAPER)
        cls.store.save_paper({"id": PAPER, "revision": paper["revision"],
                              "patch": {"fields": dict(paper["fields"],
                                                       summary="人工复述：刷新后必须还在")}})

    @classmethod
    def tearDownClass(cls):
        cls.manager.stop()
        cls.httpd.shutdown()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_reading_card_flow(self):
        self.report = self._run_driver()
        out = self.report
        self.assertIn("cardHtmlLength", out)

        # 1. opening the card and moving around fetches no answer body
        self.assertEqual(0, out["steps"][0]["apiHits"], "initial page load must not fetch an answer")
        self.assertEqual(0, out["steps"][1]["apiHits"], "opening the card must not fetch an answer")
        self.assertEqual(13, out["fieldCount"])
        self.assertEqual(13, out["aiButtons"])
        self.assertIn("AI", out["statusBarText"])

        # 2. clicking one field fetches once and expands only that field
        click_step = next(step for step in out["steps"] if step["name"] == "after-click-summary")
        self.assertEqual(1, click_step["delta"], "one click must cause exactly one answer request")
        self.assertEqual(1, out["openPanels"])
        self.assertEqual(["summary"], out["expandedButtons"])
        self.assertIn("置信度", out["panelText"])
        self.assertEqual(["3"], out["afterSwitch"]["pageButtons"],
                         "the citation panel must offer its own PDF page")
        self.assertTrue(out["afterSwitch"]["panelQuotes"], "the quote must be shown")
        self.assertTrue(out["panelText"], "the answer text must be shown")

        # 3. switching fields keeps a single panel open and does not refetch
        self.assertEqual(1, out["afterSwitch"]["openPanels"])
        self.assertEqual(["data"], out["afterSwitch"]["expanded"])
        self.assertEqual(1, out["afterSwitch"]["requests"])

        # 4. evidence jump drives the existing pager with the physical page
        self.assertNotEqual(out["pageBefore"], out["pageAfter"])
        self.assertEqual(out["jumpTarget"], out["pageAfter"])
        self.assertIn("PDF 第", out["printedLabel"])

        # 5. copy is explicit, appends, and changes nothing else
        copied = out["copy"]["textareaValue"]
        self.assertIn("人工复述", copied, "the manual text must stay" if copied else "field was empty")
        self.assertIn("AI 参考", copied, "the AI text must be appended")
        self.assertLess(copied.index("人工复述"), copied.index("AI 参考"), "append, never overwrite")
        self.assertEqual([False, False, False, False], out["copy"]["stepsChecked"],
                         "copying must not change the reading steps")
        self.assertEqual("0", out["copy"]["annotationCount"])

        # 6. switching papers clears the panel and shows the new paper's own state
        self.assertEqual(0, out["afterSwitchPaper"]["openPanels"])
        self.assertNotIn("AI 参考已就绪", out["afterSwitchPaper"]["statusText"])

        # 7. reload: manual record survives, AI expansion is cleared by design
        self.assertEqual([], out["consoleErrors"], "the page must stay error free")
        self.assertEqual(0, out["afterReload"]["openPanels"])
        self.assertIn("人工复述：刷新后必须还在", out["afterReload"]["summaryText"] or "")

    def _fragment_url(self):
        """A URL whose fragment carries a one-time capability, like the launcher's."""
        capability = self.sessions.issue_bootstrap(source="browser-test")
        return f"http://127.0.0.1:{self.port}/#bootstrap={capability}"

    def _run_driver(self):
        profile = Path(tempfile.mkdtemp(prefix="cdp-phase6-"))
        driver = Path(tempfile.gettempdir()) / "phase6_card_driver.mjs"
        driver.write_text(DRIVER.replace("__PAPER__", PAPER), encoding="utf-8")
        debug_port = free_port()
        proc = subprocess.Popen([CHROME, "--headless=new", f"--remote-debugging-port={debug_port}",
                                 f"--user-data-dir={profile}", "--no-first-run",
                                 "--no-default-browser-check", "--disable-extensions",
                                 "--hide-scrollbars", "about:blank"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(80):
                try:
                    with urlopen(f"http://127.0.0.1:{debug_port}/json/version", timeout=1) as response:
                        json.load(response)
                    break
                except Exception:                       # noqa: BLE001 - retry until ready
                    time.sleep(0.25)
            site = self._fragment_url()
            env = dict(**__import__("os").environ,
                       PHASE6_OUT=str(Path(__file__).resolve().parent))
            run = subprocess.run(["node", str(driver), site, str(debug_port), PAPER],
                                 capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", timeout=300, env=env)
            if not (run.stdout or "").strip().startswith("{"):
                self.fail("driver produced no JSON:\n" + (run.stdout or "") + (run.stderr or "")[:2000])
            report = json.loads(run.stdout)
            import base64
            shot = report.pop("screenshotData", None)
            if shot:
                (Path(__file__).resolve().parent / "phase6-card-ai-panel.png").write_bytes(
                    base64.b64decode(shot))
                out_shot = Path(__file__).resolve().parent / "phase6-card-ai-panel.png"
                print("screenshot:", out_shot, out_shot.stat().st_size, "bytes")
            (Path(__file__).resolve().parent / "phase6-card-browser.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
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
