"""Phase 7 layout measurement: PDF share of the workspace and fit-page behaviour.

Read-only for the real library: it starts its own server over a temporary data copy.

    python tests/measure_pdf_layout.py [--label after] [--out tests/phase7-layout.json]
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
SIZES = [(1366, 768), (1920, 1080), (1280, 800), (1024, 768), (900, 900), (430, 900)]

PROBE = r"""
const url = process.argv[2], port = process.argv[3];
const sizes = JSON.parse(process.argv[4]);
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
await send('Page.navigate', {url});
await sleep(7000);
const out = [];
for (const [w, h] of sizes) {
  await send('Emulation.setDeviceMetricsOverride', {width: w, height: h, deviceScaleFactor: 1, mobile: false});
  await sleep(1600);
  out.push(await ev(`(() => {
    const rect = (sel) => { const el = document.querySelector(sel); if (!el) return null; const r = el.getBoundingClientRect(); return {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)}; };
    const pct = (a, b) => (a && b && b.w > 0) ? Math.round(a.w / b.w * 1000) / 10 : null;
    const workspace = rect('.workspace'), panel = rect('.pdf-panel'), scroll = rect('#pdf-scroll');
    const canvas = rect('#pdf-canvas'), layer = rect('#text-layer'), notes = rect('.notes-panel');
    const scrollEl = document.querySelector('#pdf-scroll');
    return {
      viewport: {w: window.innerWidth, h: window.innerHeight},
      workspace, panel, scroll, canvas, layer, notes,
      panelShare: pct(panel, workspace),
      canvasShare: pct(canvas, workspace),
      notesShare: pct(notes, workspace),
      fitsInsideScroll: !!canvas && !!scroll && canvas.h <= scroll.h + 1 && canvas.w <= scroll.w + 1,
      scrollOverflow: scrollEl.scrollHeight - scrollEl.clientHeight,
      scrollClass: scrollEl.className,
      zoomValue: document.querySelector('#zoom-select').value,
      announcer: document.querySelector('#page-announcer').textContent,
      canvasMatchesLayer: !!canvas && !!layer && Math.abs(canvas.w - layer.w) <= 1 && Math.abs(canvas.h - layer.h) <= 1,
      printed: document.querySelector('#printed-label').textContent,
    };
  })()`));
  const shot = await send('Page.captureScreenshot', {format: 'png'});
  out[out.length - 1].shot = shot.data;
}
console.log(JSON.stringify(out, null, 2));
ws.close();
"""


def run_probe(site, debug_port, sizes):
    driver = Path(tempfile.gettempdir()) / "phase7_layout_probe.mjs"
    driver.write_text(PROBE, encoding="utf-8")
    run = subprocess.run(["node", str(driver), site, str(debug_port), json.dumps(sizes)],
                         capture_output=True, text=True, encoding="utf-8", errors="replace",
                         timeout=400)
    if not (run.stdout or "").strip().startswith("["):
        raise SystemExit("probe failed:\n" + (run.stdout or "") + (run.stderr or "")[:2000])
    return json.loads(run.stdout)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(Path(__file__).resolve().parent /
                                             "phase7-layout.json"))
    parser.add_argument("--label", default="")
    args = parser.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="phase7-layout-"))
    profile = Path(tempfile.mkdtemp(prefix="cdp-phase7-"))
    fresh_library_v1(tmp)
    store = server.Store(tmp)
    manager = analysis.AnalysisManager(store, provider_factory=lambda s: providers.DisabledAdapter(),
                                       log=lambda record: None)
    port = free_port()
    httpd = server.ThreadingHTTPServer(("127.0.0.1", port),
                                       server.handler_for(store, "layout-token", manager))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    debug_port = free_port()
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
        report = run_probe(f"http://127.0.0.1:{port}/?token=layout-token", debug_port, SIZES)
        payload = {"label": args.label, "rows": report} if args.label else report
        Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        import base64
        for row in report:
            vp = row["viewport"]
            print(f"{vp['w']}x{vp['h']}: panel {row['panelShare']}% | canvas {row['canvas']['w']}x"
                  f"{row['canvas']['h']} share {row['canvasShare']}% | notes {row['notesShare']}%"
                  f" | fits={row['fitsInsideScroll']} overflow={row['scrollOverflow']}"
                  f" | layer={row['canvasMatchesLayer']} | zoom={row['zoomValue']}")
            shot = row.pop("shot", None)
            size = f"{vp['w']}x{vp['h']}"
            if shot:
                target = Path(__file__).resolve().parent / f"phase7-layout-{size}.png"
                target.write_bytes(base64.b64decode(shot))
                print("    screenshot:", target.name)
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
