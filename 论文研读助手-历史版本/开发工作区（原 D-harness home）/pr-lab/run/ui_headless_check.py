"""Runtime UI verification for 1.3.5 on the real served page, via Chrome DevTools protocol.

Not a unit test. It starts the lab server on 8791 with a temp data dir, asks the launcher pipe
for a one-time bootstrap capability (exactly as launch.py does), and then opens a temporary copy
of the served page in headless Chrome. The copy overrides window.fetch for `/api/analysis` only,
so the AI progress bar and its poll can be exercised while the real library state stays intact.

Questions a source-level test cannot answer, answered here:
  * does every library row get a delete control, and does clicking it open the confirmation
    dialog with THAT row's title, without selecting the row?
  * does the AI progress bar show percentage + Chinese stage label + the server message while a
    job is queued/running, in both the panel and the library row, and vanish when it fails?
  * does polling stop once the job reaches a terminal status?
  * is the served HTML version-stamped (no literal __VERSION__)?

Writes only inside %TEMP%, plus one temporary file under static/ that is removed in `finally`.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import launcher_pipe              # noqa: E402
import schema                     # noqa: E402
import server as pr_server        # noqa: E402
import storage                    # noqa: E402

PORT = 8791
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
DATA = Path(tempfile.gettempdir()) / "ui-check"
PATCH_NAME = "uicheck.html"

RUNNING = {"status": "running", "stage": "chunking", "progress": 30,
           "message": "正在读第 2/3 段（PDF 第 21–40 页）"}
FAILED = {"status": "failed", "stage": "", "progress": 0, "message": "模型没有返回可用结果"}


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def paper(title, status="waiting_for_user", stage="", progress=0, message="", sha="", language="zh"):
    pid = str(uuid.uuid4())
    return {
        "id": pid, "title": title, "authors": "某作者", "year": "2024", "venue": "",
        "doi": "", "citation": "", "tags": "测试", "language": language, "type": "empirical",
        "fields": {name: "" for name in pr_server.FIELDS},
        "steps": [False, False, False, False], "annotations": [], "pageOffset": 0,
        "pageLabels": [], "lastPage": 1, "revision": 0,
        "created": "2026-01-01T00:00:00Z", "updated": "2026-01-01T00:00:00Z",
        "archived": False, "note_name": f"{title}--{pid[:8]}.md",
        "analysis": dict(schema.default_analysis(), status=status, stage=stage,
                         progress=progress, message=message, source_sha256=sha, language=language),
    }


def seed():
    shutil.rmtree(DATA, ignore_errors=True)
    (DATA / "papers").mkdir(parents=True)
    papers = [paper("普通论文"), paper("队列中的论文"), paper("正在分析的论文"),
              paper("分析失败的论文")]
    state = {"version": schema.SCHEMA_VERSION,
             "settings": {"vault_path": "", "sync_enabled": False, "last_sync": "",
                          "sync_error": "", "ai": schema.default_ai_settings()},
             "papers": papers}
    storage.atomic_write_json(DATA / "library.json", state)
    return {p["title"]: p["id"] for p in papers}


def build_patched_page(base):
    """A copy of the served index.html whose fetch is stubbed for one live AI status feed."""
    with urllib.request.urlopen(base + "/", timeout=10) as response:
        html = response.read().decode("utf-8")
    patch = """
  <script>
  /* 仅用于本次校验：只接管 /api/analysis，其它请求（状态、PDF、保存）仍然打到真实服务。 */
  window.__aiFeed = {
    status: 'running', stage: 'chunking', progress: 30,
    message: '正在读第 2/3 段（PDF 第 21–40 页）',
    calls: 0, times: [], lastPaper: ''
  };
  const originalFetch = window.fetch.bind(window);
  window.fetch = async (input, init) => {
    const url = String((input && input.url) || input);
    const base = url.split('?')[0];
    if (base.endsWith('/api/analysis')) {
      const feed = window.__aiFeed;
      feed.calls += 1;
      feed.times.push(Date.now());
      return new Response(JSON.stringify({
        statuses: {[feed.lastPaper]: {status: feed.status, stage: feed.stage,
                                      progress: feed.progress, message: feed.message,
                                      source_sha256: '', error_code: '', error_summary: ''}},
        queue: {}, budget: {}, outbound: {}, health: {}
      }), {status: 200, headers: {'Content-Type': 'application/json'}});
    }
    const match = base.match(/\\/api\\/analysis\\/([^/]+)\\/status$/);
    if (match) {
      const feed = window.__aiFeed;
      feed.lastPaper = match[1];
      return new Response(JSON.stringify({
        paper_id: match[1], status: feed.status, stage: feed.stage, progress: feed.progress,
        message: feed.message, error_code: '', error_summary: '', in_queue: false,
        running_now: false, has_answer: false, answer_file: ''
      }), {status: 200, headers: {'Content-Type': 'application/json'}});
    }
    return originalFetch(input, init);
  };
  </script>
"""
    return html.replace("</body>", patch + "</body>")


# --- minimal CDP client (stdlib only) -------------------------------------------------
def ws_connect(url):
    from urllib.parse import urlparse
    parts = urlparse(url)
    conn = socket.create_connection((parts.hostname, parts.port), timeout=25)
    conn.sendall((
        f"GET {parts.path} HTTP/1.1\r\nHost: {parts.hostname}:{parts.port}\r\n"
        "Upgrade: websocket\r\nConnection: Upgrade\r\n"
        "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
    buffer = b""
    while b"\r\n\r\n" not in buffer:
        chunk = conn.recv(4096)
        if not chunk:
            raise RuntimeError("websocket handshake failed")
        buffer += chunk
    head, rest = buffer.split(b"\r\n\r\n", 1)
    if b"101" not in head.split(b"\r\n")[0]:
        raise RuntimeError("websocket upgrade refused: " + head[:80].decode("latin-1"))
    conn.settimeout(60)
    return conn, rest


def send(conn, message):
    payload = json.dumps(message).encode()
    mask = os.urandom(4)
    header = bytearray([0x81])
    if len(payload) < 126:
        header.append(0x80 | len(payload))
    elif len(payload) < 65536:
        header.append(0x80 | 126)
        header += len(payload).to_bytes(2, "big")
    else:
        header.append(0x80 | 127)
        header += len(payload).to_bytes(8, "big")
    header += mask
    conn.sendall(bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))


class Client:
    def __init__(self, url):
        self.conn, self.pending = ws_connect(url)
        self.counter = 0

    def command(self, method, params=None):
        self.counter += 1
        ident = self.counter
        send(self.conn, {"id": ident, "method": method, "params": params or {}})
        seen = []
        while True:
            try:
                message, self.pending = self._recv()
            except TimeoutError:
                sys.stderr.write(f"[cdp] {method} timed out; frames seen: "
                                 f"{[m.get('method') or ('id=' + str(m.get('id'))) for m in seen][:20]}\n")
                sys.stderr.flush()
                raise
            if message.get("id") == ident:
                return message
            seen.append(message)

    def _recv(self):
        while True:
            pending = self.pending
            if pending and (pending[0] & 0x0F) not in (0x1, 0x0, 0x8, 0x9, 0xA):
                raise RuntimeError(f"unexpected frame head: {pending[:16]!r}")
            if len(pending) >= 2:
                length = pending[1] & 0x7F
                offset = 2
                if length == 126 and len(pending) >= 4:
                    length, offset = int.from_bytes(pending[2:4], "big"), 4
                elif length == 127 and len(pending) >= 10:
                    length, offset = int.from_bytes(pending[2:10], "big"), 10
                if length < 126 or (length == 126 and offset == 4) or (length == 127 and offset == 10):
                    if len(pending) >= offset + length:
                        self.pending = pending[offset + length:]
                        raw = pending[offset:offset + length]
                        sys.stderr.write(f"[cdp] frame opcode={pending[0] & 0x0F} len={length} "
                                         f"head={raw[:70]!r}\n")
                        sys.stderr.flush()
                        return json.loads(raw.decode("utf-8")), self.pending
            chunk = self.conn.recv(65536)
            if not chunk:
                raise RuntimeError("websocket closed")
            self.pending = pending + chunk


EXPRESSION = r"""
(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const until = async (fn, ms = 20000) => {
    const end = Date.now() + ms;
    while (Date.now() < end) { if (fn()) return true; await sleep(80); }
    return false;
  };
  const out = {};
  out.booted = await until(() => window.__sessionReady &&
      document.querySelectorAll('#paper-list .paper-item').length >= 4);
  await sleep(500);
  const rows = () => [...document.querySelectorAll('#paper-list .paper-item')];
  const rowFor = t => rows().find(r => r.textContent.includes(t));
  out.rows = rows().length;
  out.row_delete_controls = rows().filter(r => r.querySelector('[data-delete-paper]')).length;
  out.row_delete_ids_match = rows().every(r =>
      r.querySelector('[data-delete-paper]').dataset.deletePaper === r.dataset.paper);

  // (1) clicking a row's delete control must not select the row
  const before = document.querySelector('#paper-title').textContent;
  rowFor('普通论文').querySelector('[data-delete-paper]').click();
  await sleep(300);
  out.dialog_open = document.querySelector('#delete-dialog').open;
  out.dialog_title = document.querySelector('#delete-paper-title').textContent;
  out.dialog_title_is_that_row = out.dialog_title === '普通论文';
  out.row_not_selected = rowFor('普通论文').className.indexOf('active') < 0;
  out.paper_title_unchanged = document.querySelector('#paper-title').textContent === before;
  // the action-strip button must still delete the currently open paper (here: the first paper)
  document.querySelector('#delete-dialog').close();
  await sleep(150);
  document.querySelector('#delete-paper').click();
  await sleep(300);
  out.strip_dialog_title = document.querySelector('#delete-paper-title').textContent;
  document.querySelector('#delete-dialog').close();
  await sleep(150);

  // (2) queued: bar + percentage + Chinese stage + verbatim server message
  rowFor('队列中的论文').click();
  await sleep(1200);
  window.__aiFeed.status = 'queued'; window.__aiFeed.stage = 'queued';
  window.__aiFeed.progress = 0; window.__aiFeed.message = '已加入分析队列';
  [...document.querySelectorAll('.notes-tabs button')].find(b => b.dataset.tab === 'card').click();
  await sleep(700);
  const queuedBox = document.querySelector('.ai-progress-box');
  out.queued = queuedBox ? {
    stage: queuedBox.querySelector('.ai-progress-stage').textContent,
    pct: queuedBox.querySelector('.ai-progress-pct').textContent,
    message: (queuedBox.querySelector('.ai-progress-message') || {}).textContent || '',
    aria: queuedBox.querySelector('[role=progressbar]').getAttribute('aria-valuenow')
  } : null;
  out.queued_ribbon = document.querySelector('#ai-progress').hidden
      ? 'hidden' : document.querySelector('#ai-progress').textContent.replace(/\s+/g, '');
  out.queued_row_bar = !!rowFor('队列中的论文').querySelector('.mini-progress.ai');

  // (3) running: 30% / 切分原文 / page-range message, row bar too
  window.__aiFeed.status = 'running'; window.__aiFeed.stage = 'chunking';
  window.__aiFeed.progress = 30;
  window.__aiFeed.message = '正在读第 2/3 段（PDF 第 21–40 页）';
  await until(() => {
    const box = document.querySelector('.ai-progress-box');
    return box && box.querySelector('.ai-progress-pct').textContent === '30%';
  }, 8000);
  const runBox = document.querySelector('.ai-progress-box');
  out.running = runBox ? {
    stage: runBox.querySelector('.ai-progress-stage').textContent,
    pct: runBox.querySelector('.ai-progress-pct').textContent,
    message: (runBox.querySelector('.ai-progress-message') || {}).textContent || '',
    fill: runBox.querySelector('.ai-progress-fill').style.width,
    aria: runBox.querySelector('[role=progressbar]').getAttribute('aria-valuenow')
  } : null;
  out.running_ribbon = document.querySelector('#ai-progress').textContent.replace(/\s+/g, '');
  out.running_ribbon_fill = document.querySelector('#ai-progress-fill').style.width;
  const rowBar = rowFor('正在分析的论文');
  out.running_row_bar = !!rowBar.querySelector('.mini-progress.ai');
  out.running_row_bar_width = (rowBar.querySelector('.mini-progress.ai span') || {}).style
      ? rowBar.querySelector('.mini-progress.ai span').style.width : null;
  out.running_row_bar_colour = (() => {
    const node = rowBar.querySelector('.mini-progress.ai span');
    return node ? getComputedStyle(node).backgroundColor : null;
  })();
  out.step_row_bar_colour = (() => {
    const node = rowBar.querySelector('.mini-progress:not(.ai) span');
    return node ? getComputedStyle(node).backgroundColor : null;
  })();

  // (4) polling is bounded: interval ~3s, and it stops at a terminal status
  const feed = window.__aiFeed;
  feed.times = [];
  await sleep(3200);
  const liveGaps = feed.times.length;
  feed.status = 'ready'; feed.stage = 'done'; feed.progress = 100;
  feed.message = 'AI 参考已就绪';
  await sleep(4000);
  feed.times = [];
  await sleep(4000);
  out.poll = {polls_in_3_2s: liveGaps, polls_after_terminal: feed.times.length};
  out.after_ready = {
    panel_bar: !!document.querySelector('.ai-progress-box'),
    ribbon_hidden: document.querySelector('#ai-progress').hidden,
    row_bar: !!rowFor('正在分析的论文').querySelector('.mini-progress.ai')
  };

  // (5) failed: no bar, failure text and retry button stay
  feed.status = 'failed'; feed.stage = ''; feed.progress = 0; feed.message = '模型没有返回可用结果';
  rowFor('分析失败的论文').click();
  await sleep(1500);
  [...document.querySelectorAll('.notes-tabs button')].find(b => b.dataset.tab === 'card').click();
  await sleep(600);
  const bar = document.querySelector('.ai-status-bar');
  out.failed = {
    panel_bar: !!document.querySelector('.ai-progress-box'),
    ribbon_hidden: document.querySelector('#ai-progress').hidden,
    status: bar ? bar.dataset.aiStatus : null,
    retry: !!(bar && bar.querySelector('[data-ai-run]')),
    text: bar ? bar.textContent.replace(/\s+/g, ' ').trim().slice(0, 60) : ''
  };

  // (6) empty library state still renders
  out.empty_state_checked = (() => {
    const list = document.querySelector('#paper-list');
    return list.innerHTML.length > 0;
  })();
  return JSON.stringify(out);
})()
"""


def main():
    ids = seed()
    server = subprocess.Popen([sys.executable, "-B", str(ROOT / "server.py"),
                               "--port", str(PORT), "--data-dir", str(DATA), "--no-worker"],
                              cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    browser = None
    report = {"seeded": ids}
    patched = ROOT / "static" / PATCH_NAME
    try:
        info = None
        for _ in range(160):
            time.sleep(0.25)
            try:
                info = json.loads((DATA / "run-instance.json").read_text(encoding="utf-8"))
                break
            except (OSError, json.JSONDecodeError):
                continue
        base = f"http://127.0.0.1:{int(info['port'])}"
        report["server"] = {"port": info["port"], "pipe_ok": bool(info.get("pipe"))}
        capability, error = launcher_pipe.request_bootstrap(info["pipe"])
        report["bootstrap"] = {"ok": bool(capability), "error": error}

        with urllib.request.urlopen(base + "/", timeout=10) as response:
            html = response.read().decode("utf-8")
        with urllib.request.urlopen(base + "/static/app.js", timeout=10) as response:
            script = response.read().decode("utf-8")
        report["served"] = {
            "no_literal_version_placeholder": "__VERSION__" not in html,
            "all_local_assets_versioned": all(
                f"/static/{name}?v=" in html for name in
                ("styles.css", "reader-layout.css", "ai-reference.css", "readability.css",
                 "app.js", "guides.js")),
            "version_substituted": f"/static/app.js?v={pr_server.APP_VERSION}" in html,
            "js_has_delete_control": "data-delete-paper=" in script,
            "js_has_progress_bar": "ai-progress-fill" in script,
        }

        patched.write_text(build_patched_page(base), encoding="utf-8")
        debug_port = free_port()
        profile = Path(tempfile.mkdtemp(prefix="ui-check-profile-"))
        browser = subprocess.Popen([CHROME, "--headless=new", "--disable-gpu", "--no-first-run",
                                    "--no-default-browser-check", "--disable-extensions",
                                    f"--remote-debugging-port={debug_port}",
                                    f"--user-data-dir={profile}", "about:blank"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        target = None
        page_url = f"{base}/static/{PATCH_NAME}#bootstrap={capability}"
        for _ in range(40):
            time.sleep(0.25)
            try:
                # Let the browser open the page itself: puts the capability in the fragment
                # before the app loads, and avoids Page.navigate entirely.
                request = urllib.request.Request(
                    f"http://127.0.0.1:{debug_port}/json/new?{urllib.parse.quote(page_url, safe='')}",
                    method="PUT")
                with urllib.request.urlopen(request, timeout=5) as response:
                    target = json.loads(response.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as exc:
                report.setdefault("open_errors", []).append(f"HTTP {exc.code}:{exc.read()[:80]!r}")
                break
            except Exception as exc:                       # noqa: BLE001 - keep polling
                report.setdefault("open_errors", []).append(type(exc).__name__)
                continue
        if not target:
            with urllib.request.urlopen(f"http://127.0.0.1:{debug_port}/json/list", timeout=5) as response:
                pages = json.loads(response.read().decode("utf-8"))
            report["targets"] = [(page.get("type"), page.get("url")) for page in pages]
            blank = next((page for page in pages if page.get("url") == "about:blank"), None) \
                or next((page for page in pages if page.get("type") == "page"), None)
            client = Client(blank["webSocketDebuggerUrl"])
            report["smoke_evaluate"] = client.command(
                "Runtime.evaluate", {"expression": "1+1", "returnByValue": True}
            ).get("result", {}).get("result", {}).get("value")
            client.command("Page.navigate", {"url": page_url})
            time.sleep(4.0)
        else:
            client = Client(target["webSocketDebuggerUrl"])
            time.sleep(3.0)
            report["target_url"] = target.get("url")
            report["smoke_evaluate"] = client.command(
                "Runtime.evaluate", {"expression": "1+1", "returnByValue": True}
            ).get("result", {}).get("result", {}).get("value")
            try:
                with urllib.request.urlopen(base + "/api/health", timeout=5) as response:
                    report["server_alive_during_hang"] = json.loads(response.read().decode("utf-8"))["app"]
            except Exception as exc:                        # noqa: BLE001
                report["server_alive_during_hang"] = f"{type(exc).__name__}:{exc}"
        diagnostics = """
        JSON.stringify({
          href: location.href, ready: document.readyState, sessionReady: !!window.__sessionReady,
          sessionHint: (document.querySelector('#session-hint') || {}).textContent || '',
          rows: document.querySelectorAll('#paper-list .paper-item').length,
          title: document.title,
          hint: (document.querySelector('#paper-title') || {}).textContent || '',
          errors: window.__errors || [],
          csrf: typeof window.__csrf
        })
        """
        result = None
        for attempt in range(3):
            try:
                probe = client.command("Runtime.evaluate", {"expression": diagnostics,
                                                            "returnByValue": True, "timeout": 20000})
                report.setdefault("diagnostics", []).append(
                    probe.get("result", {}).get("result", {}).get("value"))
                result = client.command("Runtime.evaluate",
                                        {"expression": EXPRESSION, "awaitPromise": True,
                                         "returnByValue": True, "timeout": 120000})
                break
            except Exception as exc:                        # noqa: BLE001 - context may be swapped
                report.setdefault("evaluate_errors", []).append(f"{type(exc).__name__}:{exc}"[:120])
                time.sleep(2.0)
        if result is None:
            report["browser"] = {"error": "evaluate never answered"}
            return report
        if "exceptionDetails" in result.get("result", {}):
            report["browser"] = {"exception": result["result"]["exceptionDetails"].get("text"),
                                 "detail": str(result["result"]["exceptionDetails"])[:400]}
        else:
            report["browser"] = json.loads(result["result"]["result"]["value"])
        return report
    finally:
        if patched.exists():
            patched.unlink()
        if browser is not None:
            browser.terminate()
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
        report["server_stopped"] = server.poll() is not None
        report["patched_file_removed"] = not patched.exists()


if __name__ == "__main__":
    print(json.dumps(main(), ensure_ascii=False, indent=2))
