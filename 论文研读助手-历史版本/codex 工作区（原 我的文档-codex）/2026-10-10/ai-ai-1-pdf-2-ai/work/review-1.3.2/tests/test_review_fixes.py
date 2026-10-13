"""Phase 9 regression tests: the fixes from the independent review (B1, M1–M5, O6).

Each test names the review finding it locks down.

    python -m unittest discover -s tests -p "test_review_fixes.py"
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from io import BytesIO
from pathlib import Path
from urllib.request import urlopen

from baseline_helpers import PROJECT_ROOT, free_port, fresh_library_v1

sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import analysis      # noqa: E402
import providers     # noqa: E402
import reading_card  # noqa: E402
import server
import session        # noqa: E402
from card_fixtures import build_card  # noqa: E402

PAPER = "medical-2026-zang"


def read(relpath):
    return (PROJECT_ROOT / relpath).read_text(encoding="utf-8")


class B1ZoomDecision(unittest.TestCase):
    """B1: the default must not shrink text below legibility."""

    def test_threshold_and_switch_exist(self):
        app = read("static/app.js")
        # measured on-screen text is about 9.5 x scale, so the threshold sits where the
        # page stops being readable; raised from 0.62 to 0.78 in the focus release
        self.assertIn("const MIN_READABLE_SCALE=0.78", app)
        self.assertIn("mirrorZoomSelect('width')", app, "the toolbar must show the real mode")
        self.assertIn("整页显示会把文字缩到看不清", app, "the user must be told why")

    def test_manual_choice_is_never_overridden(self):
        app = read("static/app.js")
        # the change handler sets the manual flag, so later renders keep the user's choice
        handler = app[app.index("$('#zoom-select').addEventListener('change'"):]
        handler = handler[:handler.index("});")]
        self.assertIn("autoFitDecided=true", handler)
        self.assertIn("autoFitManual=true", handler)

    def test_decision_is_re_evaluated_on_layout_change(self):
        """A bigger window must go back to whole-page display, unless the user chose."""
        app = read("static/app.js")
        body = app[app.index("function fitScale(viewport)"):]
        body = body[:body.index("function announcePage")]
        self.assertIn("if(!autoFitManual)autoFitMode(viewport,fitted);", body,
                      "the decision must run in both modes, not only while zoomMode==='page'")
        self.assertIn("zoomMode='page';mirrorZoomSelect('page')", body)
        self.assertIn("noteFit('窗口变大", body)

    def test_manual_zoom_is_relative_to_the_fitted_page(self):
        app = read("static/app.js")
        self.assertIn("return fitted*zoom;", app)

    def test_hint_target_exists_in_the_document(self):
        self.assertIn('id="fit-hint"', read("static/index.html"))


class M1RenderCancellation(unittest.TestCase):
    """M1: the previous render must finish before the canvas is reused."""

    def test_previous_render_is_awaited(self):
        app = read("static/app.js")
        body = app[app.index("async function renderPage()"):]
        body = body[:body.index("function renderHighlights()")]
        self.assertIn("pdfRender.cancel()", body)
        self.assertIn("await pdfRender.promise", body,
                      "a cancelled render must be awaited before rendering again")
        self.assertLess(body.index("await pdfRender.promise"), body.index("page.render("))

    def test_no_unhandled_canvas_conflict_in_a_live_run(self):
        """Drive rapid page turns and require a clean console."""
        report = run_cdp(RAPID_DRIVER)
        self.assertEqual([], report["errors"], report["errors"])
        self.assertTrue(report["consistent"], report)


class M2WheelBoundary(unittest.TestCase):
    """M2: a swallowed wheel must not scroll the shell; the boundary must be reported."""

    def test_boundary_and_locked_events_are_prevented(self):
        app = read("static/app.js")
        body = app[app.index("function onPdfWheel(event)"):]
        body = body[:body.index("function selectedQuote()")]
        self.assertIn("const insideLock=now<wheelLockUntil;", body)
        self.assertIn("if(insideLock||next<1||next>pdf.numPages)", body)
        self.assertIn("event.preventDefault()", body)
        # the swallow branch must prevent default when there is nothing to scroll
        self.assertRegex(body, r"if\(overflows<=2\|\|next<1\|\|next>pdf\.numPages\)event\.preventDefault\(\)")

    def test_first_page_up_does_not_scroll_anything(self):
        report = run_cdp(BOUNDARY_DRIVER)
        self.assertEqual(1, report["pageAfterUp"])
        self.assertEqual(0, report["windowScrollY"])
        self.assertEqual(0, report["notesScrollTop"], "the notes panel must not move either")
        self.assertEqual([], report["errors"])


class M3MissingAnswerFile(unittest.TestCase):
    """M3: a `ready` badge without an answer file must become retryable."""

    def test_recover_marks_missing_answer_as_stale(self):
        tmp = Path(tempfile.mkdtemp(prefix="fix-m3-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        fresh_library_v1(tmp)
        store = server.Store(tmp)
        store.update_ai_settings({"provider": "ollama", "model": "fix-model", "auto_analyze": False})
        fake = providers.FakeProvider(script=["success"], model="fix-model", payload={})
        fake.payload_for = lambda request, payload: build_card(
            pages_from_prompt(request.user))
        manager = analysis.AnalysisManager(store, provider_factory=lambda s: fake,
                                           log=lambda record: None)
        manager.grant_approval(PAPER, model="fix-model", scope="test")
        manager.enqueue(PAPER, reason="manual", force=True)
        manager.process_once()
        self.assertEqual("ready", store.analysis_meta(PAPER)["status"])

        # the answer file disappears (disk cleanup, partial restore, manual delete)
        store.files.delete_analysis(PAPER)
        store2 = server.Store(tmp)
        manager2 = analysis.AnalysisManager(store2, provider_factory=lambda s: fake,
                                            log=lambda record: None)
        outcome = manager2.recover()
        meta = store2.analysis_meta(PAPER)
        self.assertIn(PAPER, outcome["stale"])
        self.assertEqual("stale", meta["status"])
        self.assertEqual("answer_missing", meta["error_code"])
        self.assertIn("丢失", meta["message"])

        # and the retry path really regenerates it
        manager2.grant_approval(PAPER, model="fix-model", scope="test")
        manager2.enqueue(PAPER, reason="retry")
        manager2.process_once()
        self.assertEqual("ready", store2.analysis_meta(PAPER)["status"])
        self.assertTrue(store2.files.has_analysis(PAPER))

    def test_ghost_file_name_is_also_caught(self):
        tmp = Path(tempfile.mkdtemp(prefix="fix-m3b-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        fresh_library_v1(tmp)
        store = server.Store(tmp)
        state = json.loads((tmp / "library.json").read_text(encoding="utf-8"))
        for item in state["papers"]:
            if item["id"] == PAPER:
                item.setdefault("analysis", {})
                item["analysis"].update({"status": "ready", "answer_file": "ghost.json",
                                         "source_sha256": "a" * 64})
        (tmp / "library.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        store2 = server.Store(tmp)
        manager = analysis.AnalysisManager(store2, provider_factory=lambda s: None,
                                           log=lambda record: None)
        manager.recover()
        meta = store2.analysis_meta(PAPER)
        self.assertEqual("stale", meta["status"])
        self.assertEqual("answer_missing", meta["error_code"])

    def test_browser_offers_retry_for_a_stale_answer(self):
        app = read("static/app.js")
        banner = app[app.index("function aiCardBanner(p)"):]
        banner = banner[:banner.index("function aiPanelHTML")]
        self.assertIn("'stale'", banner)
        self.assertIn("生成 / 重试", banner)

    def test_stale_clears_the_cached_answer(self):
        app = read("static/app.js")
        body = app[app.index("async function aiRefreshStatus"):]
        body = body[:body.index("function aiCopyToField")]
        self.assertIn("aiState.answer=null", body)


class M4PromptTruncation(unittest.TestCase):
    """M4: the answer must say which pages the model actually saw."""

    def make_chunks(self, count, pages_per_chunk=2, chars=1500):
        class Chunk:
            def __init__(self, index):
                self.start_page = index * pages_per_chunk + 1
                self.end_page = (index + 1) * pages_per_chunk
                self.text = "本文这一段用于测量提示词预算。" * (chars // 16)

        return [Chunk(index) for index in range(count)]

    def test_report_lists_used_pages_and_truncation(self):
        paper = {"title": "长论文", "language": "zh", "type": "empirical"}
        chunks = self.make_chunks(40)               # 80 pages, far beyond the budget
        request = reading_card.build_request(paper, chunks, 80, model="m")
        report = request.input_report
        self.assertTrue(report["truncated"])
        self.assertLess(report["last_page"], 80)
        self.assertLessEqual(report["chars"], reading_card.PROMPT_BUDGET_ZH)
        self.assertIn("PDF 第 1-", request.user)

    def test_answer_records_the_input_block(self):
        pages = [{"pdf_page": 1, "text": "第一页正文内容用于核对引文。"},
                 {"pdf_page": 2, "text": "第二页正文内容用于核对引文。"},
                 {"pdf_page": 3, "text": "第三页正文内容用于核对引文。"}]
        card = build_card(pages)
        answer = reading_card.validate_reading_card(
            card, __import__("quotes").PageTextIndex(pages), 3,
            input_report={"pages_used": [1, 2], "first_page": 1, "last_page": 2,
                          "truncated": True, "chars": 5000, "budget": 28000,
                          "chunks_used": 1, "chunks_total": 30})
        self.assertTrue(answer["input"]["truncated"])
        self.assertIn("只送入", answer["input"]["note"])
        self.assertEqual([1, 2], answer["input"]["pages_used"])

    def test_english_gets_a_larger_budget(self):
        self.assertEqual(reading_card.PROMPT_BUDGET_EN,
                         reading_card.prompt_budget({"language": "en"}))
        self.assertEqual(reading_card.PROMPT_BUDGET_ZH,
                         reading_card.prompt_budget({"language": "zh"}))
        self.assertGreater(reading_card.PROMPT_BUDGET_EN, reading_card.PROMPT_BUDGET_ZH)

    def test_ready_message_mentions_the_truncation(self):
        source = read("analysis.py")
        self.assertIn("本次只送入第", source)

    def test_full_paper_is_not_reported_as_truncated(self):
        paper = {"title": "短文", "language": "zh", "type": "empirical"}
        report = reading_card.build_request(paper, self.make_chunks(1), 2).input_report
        self.assertFalse(report["truncated"])
        note = reading_card.input_block(report, 2)["note"]
        self.assertIn("全部页面", note)


class M5UpstreamBody(unittest.TestCase):
    """M5: only a short first line of an upstream error body may be stored."""

    def test_upstream_body_is_never_read(self):
        """Phase 1 replaced `_first_line` truncation with not reading the body at all."""
        source = (Path(__file__).resolve().parent.parent / "providers.py").read_text(
            encoding="utf-8")
        self.assertNotIn("_first_line", source)
        self.assertNotIn("exc.read(", source,
                         "an upstream error body must not be read into a message")
        self.assertIn("def _request_id(", source)

    def test_error_message_stays_short_and_redacted(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        secret = "sk-live-review-secret-123456"

        class Stub(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def do_POST(self):
                body = json.dumps({"error": "bad key", "authorization":
                                   self.headers.get("Authorization", ""),
                                   "prompt_echo": "分级诊疗制度下的医疗控费机制",
                                   "hint": f"key {secret}"}).encode()
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        stub = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
        stub.daemon_threads = True
        threading.Thread(target=stub.serve_forever, daemon=True).start()
        self.addCleanup(stub.shutdown)
        tmp = Path(tempfile.mkdtemp(prefix="fix-m5-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        fresh_library_v1(tmp)
        store = server.Store(tmp)
        store.update_ai_settings({"provider": "openai_compatible", "model": "m",
                                  "base_url": f"http://127.0.0.1:{stub.server_address[1]}/v1"})
        manager = analysis.AnalysisManager(store, provider_factory=None, log=lambda r: None)
        manager.grant_approval(PAPER, model="fix-model", scope="test")
        manager.enqueue(PAPER, reason="manual", force=True)
        manager.process_once()
        meta = store.analysis_meta(PAPER)
        library = (tmp / "library.json").read_text(encoding="utf-8")
        self.assertEqual("failed", meta["status"])
        self.assertLessEqual(len(meta["error_message"]), 200)
        self.assertNotIn(secret, meta["error_message"])
        self.assertNotIn(secret, library)
        self.assertNotIn("分级诊疗", meta["error_message"],
                         "an echoed prompt must not be stored in the library")


class O6LogRotation(unittest.TestCase):
    """O6: the launcher must not let startup.log grow without bound."""

    def test_rotation_code_present(self):
        launch = read("launch.py")
        self.assertIn("startup.old.log", launch)
        self.assertIn("2 * 1024 * 1024", launch)


class EvidenceTrail(unittest.TestCase):
    """The corrected page is kept for the audit trail, and pages are listed."""

    def test_verified_evidence_records_the_claimed_page(self):
        pages = [{"pdf_page": 1, "text": "第一页有这句话用于核对引文。"},
                 {"pdf_page": 2, "text": "第二页有另一句话用于核对引文。"}]
        index = __import__("quotes").PageTextIndex(pages)
        card = build_card(pages)
        # claim page 1 for a quote that really lives on page 1 -> no correction marker
        answer = reading_card.validate_reading_card(card, index, 2)
        for item in answer["answers"].values():
            for entry in item["evidence"]:
                self.assertIsNone(entry.get("claimed_page"))
        self.assertIn("evidence_pages", answer["verification"])


# ---------------------------------------------------------------------------
# CDP drivers for the browser-side checks
# ---------------------------------------------------------------------------

RAPID_DRIVER = r"""
const url = process.argv[2], port = process.argv[3];
const list = await (await fetch('http://127.0.0.1:' + port + '/json/list')).json();
const page = list.find(t => t.type === 'page');
const ws = new WebSocket(page.webSocketDebuggerUrl);
let id = 0; const pending = new Map(); const errors = [];
const send = (m, p = {}) => new Promise((res, rej) => { const i = ++id; pending.set(i, {res, rej}); ws.send(JSON.stringify({id: i, method: m, params: p})); });
ws.addEventListener('message', ev => {
  const m = JSON.parse(ev.data);
  if (m.id && pending.has(m.id)) { const {res, rej} = pending.get(m.id); pending.delete(m.id); m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result); return; }
  if (m.method === 'Runtime.consoleAPICalled' && m.params.type === 'error') errors.push(m.params.args.map(a => a.value || a.description || '').join(' '));
  if (m.method === 'Runtime.exceptionThrown') errors.push('exception: ' + (m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text));
});
await new Promise(r => ws.addEventListener('open', r));
await send('Runtime.enable'); await send('Page.enable');
const ev = async (e) => { const r = await send('Runtime.evaluate', {expression: e, awaitPromise: true, returnByValue: true}); if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails.exception?.description)); return r.result.value; };
const sleep = ms => new Promise(r => setTimeout(r, ms));
await send('Page.navigate', {url});

// the page exchanges the capability in its fragment for a session before it can be driven
for (let attempt = 0; attempt < 80; attempt += 1) {
  const probe = await send('Runtime.evaluate', {expression: 'window.__csrf || ""', returnByValue: true});
  if (probe && probe.result && probe.result.value) break;
  await sleep(250);
}

await sleep(7000);
const state = () => ev('(() => { const c = document.querySelector("#pdf-canvas").getBoundingClientRect(); const l = document.querySelector("#text-layer").getBoundingClientRect(); return {page: Number(document.querySelector("#page-input").value), canvas: [Math.round(c.width), Math.round(c.height)], layer: [Math.round(l.width), Math.round(l.height)], announcer: document.querySelector("#page-announcer").textContent, blank: document.querySelector("#pdf-canvas").width === 0}; })()');
// hammer the pager: keyboard turns with no delay, twice
for (let round = 0; round < 2; round++) {
  for (let i = 0; i < 8; i++) {
    await send('Input.dispatchKeyEvent', {type: 'keyDown', code: 'ArrowRight', key: 'ArrowRight', windowsVirtualKeyCode: 39});
    await send('Input.dispatchKeyEvent', {type: 'keyUp', code: 'ArrowRight', key: 'ArrowRight', windowsVirtualKeyCode: 39});
  }
  await sleep(400);
}
await sleep(4000);
const final = await state();
console.log(JSON.stringify({errors, final, consistent: JSON.stringify(final.canvas) === JSON.stringify(final.layer) && !final.blank && final.page > 1}, null, 2));
ws.close();
"""

BOUNDARY_DRIVER = r"""
const url = process.argv[2], port = process.argv[3];
const list = await (await fetch('http://127.0.0.1:' + port + '/json/list')).json();
const page = list.find(t => t.type === 'page');
const ws = new WebSocket(page.webSocketDebuggerUrl);
let id = 0; const pending = new Map(); const errors = [];
const send = (m, p = {}) => new Promise((res, rej) => { const i = ++id; pending.set(i, {res, rej}); ws.send(JSON.stringify({id: i, method: m, params: p})); });
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

// the page exchanges the capability in its fragment for a session before it can be driven
for (let attempt = 0; attempt < 80; attempt += 1) {
  const probe = await send('Runtime.evaluate', {expression: 'window.__csrf || ""', returnByValue: true});
  if (probe && probe.result && probe.result.value) break;
  await sleep(250);
}

await sleep(7000);
const box = await ev('(() => { const r = document.querySelector("#pdf-scroll").getBoundingClientRect(); return {x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2)}; })()');
// on page 1, scroll up twice: neither may scroll the shell or the notes panel
for (let i = 0; i < 2; i++) {
  await send('Input.dispatchMouseEvent', {type: 'mouseWheel', x: box.x, y: box.y, deltaX: 0, deltaY: -120, pointerType: 'mouse'});
  await sleep(300);
}
await sleep(600);
console.log(JSON.stringify(await ev('(() => ({pageAfterUp: Number(document.querySelector("#page-input").value), windowScrollY: window.scrollY, notesScrollTop: document.querySelector(".notes-content").scrollTop}))()').then(r => ({...r, errors})), null, 2));
ws.close();
"""


def run_cdp(driver_source):
    tmp = Path(tempfile.mkdtemp(prefix="fix-cdp-"))
    profile = Path(tempfile.mkdtemp(prefix="cdp-fix-"))
    fresh_library_v1(tmp)
    store = server.Store(tmp)
    manager = analysis.AnalysisManager(store, provider_factory=lambda s: providers.DisabledAdapter(),
                                       log=lambda record: None)
    port = free_port()
    sessions = session.SessionStore()
    httpd = server.BoundedHTTPServer(("127.0.0.1", port),
                                     server.handler_for(store, "", manager, sessions=sessions))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    debug_port = free_port()
    # The page receives a one-time capability in its fragment and exchanges it for a session,
    # exactly as the launcher does it; there is no page token to pass in the query string.
    capability = sessions.issue_bootstrap(source="browser-test")
    page_url = f"http://127.0.0.1:{port}/#bootstrap={capability}"
    import subprocess
    chrome = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    driver = Path(tempfile.gettempdir()) / "fix_driver.mjs"
    driver.write_text(driver_source, encoding="utf-8")
    proc = subprocess.Popen([chrome, "--headless=new", f"--remote-debugging-port={debug_port}",
                             f"--user-data-dir={profile}", "--no-first-run", "--hide-scrollbars",
                             "--no-default-browser-check", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(80):
            try:
                with urlopen(f"http://127.0.0.1:{debug_port}/json/version", timeout=1) as response:
                    json.load(response)
                break
            except Exception:                       # noqa: BLE001
                time.sleep(0.25)
        run = subprocess.run(["node", str(driver), page_url,
                              str(debug_port)], capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=300)
        if not (run.stdout or "").strip().startswith("{"):
            raise AssertionError("driver failed:\n" + (run.stdout or "") + (run.stderr or "")[:1500])
        return json.loads(run.stdout)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        httpd.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(profile, ignore_errors=True)


def pages_from_prompt(prompt):
    class Page:
        __slots__ = ("pdf_page", "text", "char_count", "backend")

        def __init__(self, number, text):
            self.pdf_page = number
            self.text = text
            self.char_count = len(text)
            self.backend = "prompt"

    result = []
    blocks = re.split(r"PDF 第 (\d+)-(\d+) 页", prompt or "")
    for index in range(1, len(blocks) - 2, 3):
        text = blocks[index + 2]
        if len(text.strip()) >= 20:
            result.append(Page(int(blocks[index]), text))
    return result


if __name__ == "__main__":
    import unittest
    unittest.main(verbosity=2)
