"""Stage 1 helper: dump the current reader DOM skeleton + zoom-control inventory.

Produces a machine-checkable inventory of every interactive control in the reader view
so that layout work can be proven not to drop any of them.
"""
from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import urlopen

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
APP = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765/"
OUT = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(r"D:\harness home\stage0-recon\dom-inventory.json")

DRIVER = r"""
const url = process.argv[2], port = process.argv[3];
const list = await (await fetch('http://127.0.0.1:' + port + '/json/list')).json();
const page = list.find(t => t.type === 'page') || list.find(t => t.type === 'tab');
const ws = new WebSocket(page.webSocketDebuggerUrl);
let id = 0; const pending = new Map();
const send = (m, p = {}) => new Promise((res, rej) => { const i = ++id; pending.set(i, {res, rej}); ws.send(JSON.stringify({id: i, method: m, params: p})); });
ws.addEventListener('message', ev => { const m = JSON.parse(ev.data); if (m.id && pending.has(m.id)) { const {res, rej} = pending.get(m.id); pending.delete(m.id); m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result); } });
await new Promise(r => ws.addEventListener('open', r));
await send('Runtime.enable'); await send('Page.enable');
await send('Page.navigate', {url});
await new Promise(r => setTimeout(r, 7000));
const ev = async (e) => { const r = await send('Runtime.evaluate', {expression: e, awaitPromise: true, returnByValue: true}); return r.exceptionDetails ? {exception: JSON.stringify(r.exceptionDetails)} : r.result.value; };
const out = await ev(`(() => {
  const controls = [...document.querySelectorAll('#reader-view button, #reader-view input, #reader-view select')].map(el => ({
    tag: el.tagName.toLowerCase(), id: el.id || null, cls: el.className || null,
    type: el.type || null, title: el.title || null, text: (el.textContent || '').trim().slice(0, 24),
    value: el.value ?? null, options: el.tagName === 'SELECT' ? [...el.options].map(o => [o.value, o.textContent]) : null,
    visible: !!(el.offsetWidth || el.offsetHeight),
  }));
  const skeleton = el => {
    if (!el) return null;
    const kids = [...el.children].map(skeleton).filter(Boolean);
    return {tag: el.tagName.toLowerCase(), id: el.id || null, cls: el.className || null, kids};
  };
  return {
    pdfPanelSkeleton: skeleton(document.querySelector('.pdf-panel')),
    pdfScrollHTML: document.querySelector('#pdf-scroll')?.innerHTML?.slice(0, 900),
    pdfPageChildren: [...(document.querySelector('#pdf-page')?.children || [])].map(c => ({tag: c.tagName.toLowerCase(), id: c.id, cls: c.className})),
    controls,
    controlIds: controls.map(c => c.id).filter(Boolean),
    highlights: [...document.querySelectorAll('.pdf-highlight')].length,
  };
})()`);
console.log(JSON.stringify(out, null, 2));
ws.close();
"""


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def main():
    port, profile = free_port(), Path(tempfile.mkdtemp(prefix="cdp-profile-"))
    driver = Path(tempfile.gettempdir()) / "stage1_dom_inventory.mjs"
    driver.write_text(DRIVER, encoding="utf-8")
    proc = subprocess.Popen([CHROME, "--headless=new", f"--remote-debugging-port={port}",
                             f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
                             "--disable-extensions", "--hide-scrollbars", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(60):
            try:
                with urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1) as r:
                    json.load(r)
                break
            except Exception:
                time.sleep(0.25)
        out = subprocess.run(["node", str(driver), APP, str(port)], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=180)
        data = json.loads((out.stdout or "{}").strip())
        OUT.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print("controls:", data.get("controlIds"))
        print("zoom options:", next((c["options"] for c in data["controls"] if c["id"] == "zoom-select"), None))
        print("pdf-page children:", data.get("pdfPageChildren"))
        print("written:", OUT)
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
