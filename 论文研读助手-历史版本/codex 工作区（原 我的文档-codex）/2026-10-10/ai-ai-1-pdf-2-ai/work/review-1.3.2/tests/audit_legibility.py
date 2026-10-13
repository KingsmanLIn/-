"""Audit E: is the fit-page text legible? Measure the rendered glyph size per paper.

`getComputedStyle(span).fontSize` on the PDF.js text layer is the PDF's own font size in
CSS px (the layer applies the page scale through a transform), so it is a direct measure of
how large the text appears on screen.

    python tests/audit_legibility.py
"""
from __future__ import annotations

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
PAPERS = ["medical-2026-zang", "fertility-2026-coffey", "finance-2021-zhang"]
SIZES = [(1366, 768), (1920, 1080), (2560, 1440)]
OUT = Path(__file__).resolve().parent / "audit-legibility.json"

DRIVER = r"""
const url = process.argv[2], port = process.argv[3], papers = JSON.parse(process.argv[4]);
const sizes = JSON.parse(process.argv[5]);
const list = await (await fetch('http://127.0.0.1:' + port + '/json/list')).json();
const page = list.find(t => t.type === 'page');
const ws = new WebSocket(page.webSocketDebuggerUrl);
let id = 0; const pending = new Map();
const send = (m, p = {}) => new Promise((res, rej) => { const i = ++id; pending.set(i, {res, rej}); ws.send(JSON.stringify({id: i, method: m, params: p})); });
ws.addEventListener('message', ev => { const m = JSON.parse(ev.data); if (m.id && pending.has(m.id)) { const {res, rej} = pending.get(m.id); pending.delete(m.id); m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result); } });
await new Promise(r => ws.addEventListener('open', r));
await send('Runtime.enable'); await send('Page.enable');
const ev = async (e) => { const r = await send('Runtime.evaluate', {expression: e, awaitPromise: true, returnByValue: true}); if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails.exception?.description || r.exceptionDetails.text)); return r.result.value; };
const sleep = ms => new Promise(r => setTimeout(r, ms));
await send('Page.navigate', {url});
await sleep(7500);

const measure = () => ev(`(() => {
  const layer = document.querySelector('#text-layer');
  const spans = [...layer.querySelectorAll('span')].filter(s => (s.textContent || '').trim().length > 6);
  if (!spans.length) return {ok: false, spans: 0};
  const sizes = spans.map(s => parseFloat(getComputedStyle(s).fontSize)).filter(v => v > 0);
  sizes.sort((a, b) => a - b);
  const canvas = document.querySelector('#pdf-canvas').getBoundingClientRect();
  const scroll = document.querySelector('#pdf-scroll');
  const scale = parseFloat(getComputedStyle(document.querySelector('#pdf-page')).getPropertyValue('--scale-factor')) || 0;
  return {ok: true, spans: spans.length, medianFontPx: sizes[Math.floor(sizes.length / 2)],
          p10FontPx: sizes[Math.floor(sizes.length * 0.1)], maxFontPx: sizes[sizes.length - 1],
          scaleFactor: Math.round(scale * 1000) / 1000,
          canvas: [Math.round(canvas.width), Math.round(canvas.height)],
          pageVisible: scroll.scrollHeight - scroll.clientHeight <= 2,
          zoom: document.querySelector('#zoom-select').value};
})()`);

const out = {papers: {}, notes: 'medianFontPx is the on-screen size of the PDF body text in CSS px'};
for (const paper of papers) {
  out.papers[paper] = {};
  // actually open that paper, otherwise every measurement is the same document
  const opened = await ev("(() => { const el = document.querySelector('[data-paper=\"" + paper + "\"]'); if (!el) return false; el.click(); return true; })()");
  out.papers[paper].opened = opened;
  await sleep(4500);
  out.papers[paper].title = await ev('document.querySelector("#view-name") ? (document.querySelector(".paper-heading h1") || {}).textContent || "" : ""');
  for (const [w, h] of sizes) {
    await send('Emulation.setDeviceMetricsOverride', {width: w, height: h, deviceScaleFactor: 1, mobile: false});
    await sleep(1200);
    out.papers[paper][w + 'x' + h] = await measure();
  }
}
// one paper at fit-width for comparison
await send('Emulation.setDeviceMetricsOverride', {width: 1366, height: 768, deviceScaleFactor: 1, mobile: false});
await sleep(1200);
await ev('(() => { const s = document.querySelector("#zoom-select"); s.value = "width"; s.dispatchEvent(new Event("change", {bubbles: true})); return true; })()');
await sleep(1500);
out.fitWidthAt1366 = await measure();
await ev('(() => { const s = document.querySelector("#zoom-select"); s.value = "1.25"; s.dispatchEvent(new Event("change", {bubbles: true})); return true; })()');
await sleep(1500);
out.zoom125At1366 = await measure();
console.log(JSON.stringify(out, null, 2));
ws.close();
"""


def main():
    tmp = Path(tempfile.mkdtemp(prefix="audit-e-"))
    profile = Path(tempfile.mkdtemp(prefix="cdp-audit-e-"))
    fresh_library_v1(tmp)
    store = server.Store(tmp)
    manager = analysis.AnalysisManager(store, provider_factory=lambda s: providers.DisabledAdapter(),
                                       log=lambda record: None)
    port = free_port()
    httpd = server.ThreadingHTTPServer(("127.0.0.1", port),
                                       server.handler_for(store, "audit-e-token", manager))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    debug_port = free_port()
    driver = Path(tempfile.gettempdir()) / "audit_e_driver.mjs"
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
        run = subprocess.run(["node", str(driver), f"http://127.0.0.1:{port}/?token=audit-e-token",
                              str(debug_port), json.dumps(PAPERS), json.dumps(SIZES)],
                             capture_output=True, text=True, encoding="utf-8", errors="replace",
                             timeout=400)
        if not (run.stdout or "").strip().startswith("{"):
            raise SystemExit("driver failed:\n" + run.stdout + run.stderr[:2000])
        report = json.loads(run.stdout)
        OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        for paper, sizes in report["papers"].items():
            print(f"{paper}: opened={sizes.get('opened')} title={str(sizes.get('title'))[:40]}")
            for size, data in sizes.items():
                if not isinstance(data, dict):
                    continue
                if data.get("ok"):
                    print(f"{paper:24s} {size:10s} median {data['medianFontPx']:.2f}px  "
                          f"p10 {data['p10FontPx']:.2f}px  scale {data['scaleFactor']}  "
                          f"canvas {data['canvas']}  whole page {data['pageVisible']}")
                else:
                    print(f"{paper:24s} {size:10s} no text layer")
        for key in ("fitWidthAt1366", "zoom125At1366"):
            data = report.get(key, {})
            if data.get("ok"):
                print(f"{key:24s} median {data['medianFontPx']:.2f}px  scale {data['scaleFactor']}  "
                      f"canvas {data['canvas']}  whole page {data['pageVisible']}")
        print("written:", OUT)
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
