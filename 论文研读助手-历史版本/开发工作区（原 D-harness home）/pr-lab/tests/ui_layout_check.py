"""Measure the reader's two-pane layout in a real browser.

The reported failure: after the drag handle was added as a third grid child, the notes panel
dropped to a second row (记录栏掉到原文下面). CSS source could look right and still wrap, so this
drives headless Chrome against the served page over the DevTools protocol and measures the
boxes: side by side, handle between the two panes, one pane when collapsed, and stacked only on
a narrow viewport.

Run:  D:\\harness home\\pr-lab\\runtime\\python.exe D:\\harness home\\pr-lab\\tests\\ui_layout_check.py
"""
import json
import re
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

LAB = Path(r"C:\Users\ASUS\OneDrive\Desktop\论文研读助手\论文研读助手-历史版本\开发工作区（原 D-harness home）\pr-lab")
sys.path.insert(0, str(LAB))
import launcher_pipe  # noqa: E402

CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
WIDE = (1500, 950)
#: The default split lives in app.js; read it so this check cannot drift from the source.
DEFAULT_SPLIT = float(re.search(
    r"const SPLIT_DEFAULT=(\d+(?:\.\d+)?);",
    (LAB / "static" / "app.js").read_text(encoding="utf-8")).group(1))
report = {}


# --- minimal CDP client (stdlib only): HTTP upgrade + unmasked-free client frames ---------
def ws_connect(url):
    parts = urllib.parse.urlparse(url)
    conn = socket.create_connection((parts.hostname, parts.port), timeout=25)
    conn.sendall((f"GET {parts.path} HTTP/1.1\r\nHost: {parts.hostname}:{parts.port}\r\n"
                  "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                  "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n"
                  ).encode())
    buffer = b""
    while b"\r\n\r\n" not in buffer:
        chunk = conn.recv(4096)
        if not chunk:
            raise RuntimeError("websocket handshake failed")
        buffer += chunk
    head, rest = buffer.split(b"\r\n\r\n", 1)
    if b"101" not in head.split(b"\r\n")[0]:
        raise RuntimeError("websocket upgrade refused")
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
    conn.sendall(bytes(header) + bytes(byte ^ mask[index % 4]
                                       for index, byte in enumerate(payload)))


class Client:
    def __init__(self, url):
        self.conn, self.pending = ws_connect(url)
        self.counter = 0

    def _recv(self):
        buffer = self.pending
        while len(buffer) < 2:
            buffer += self.conn.recv(65536)
        length = buffer[1] & 0x7F
        offset = 2
        if length == 126:
            while len(buffer) < 4:
                buffer += self.conn.recv(65536)
            length = int.from_bytes(buffer[2:4], "big")
            offset = 4
        elif length == 127:
            while len(buffer) < 10:
                buffer += self.conn.recv(65536)
            length = int.from_bytes(buffer[2:10], "big")
            offset = 10
        while len(buffer) < offset + length:
            buffer += self.conn.recv(65536)
        payload = buffer[offset:offset + length]
        return json.loads(payload.decode("utf-8", "replace")), buffer[offset + length:]

    def command(self, method, params=None):
        self.counter += 1
        ident = self.counter
        send(self.conn, {"id": ident, "method": method, "params": params or {}})
        while True:
            message, self.pending = self._recv()
            if message.get("id") == ident:
                return message


MEASURE = r"""
(() => {
  const box = (selector) => {
    const el = document.querySelector(selector);
    if (!el) return null;
    const r = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height),
            display: style.display, visible: style.display !== 'none' && r.width > 0 && r.height > 0};
  };
  const workspace = document.querySelector('.workspace');
  return JSON.stringify({
    viewport: [window.innerWidth, window.innerHeight],
    gridTemplateColumns: workspace ? getComputedStyle(workspace).gridTemplateColumns : '',
    gridTemplateRows: workspace ? getComputedStyle(workspace).gridTemplateRows : '',
    childCount: workspace ? workspace.children.length : 0,
    childClasses: workspace ? [...workspace.children].map(el => el.className || el.id) : [],
    pdfSplitVar: workspace ? workspace.style.getPropertyValue('--pdf-split') : '',
    pdf: box('.pdf-panel'), notes: box('.notes-panel'), splitter: box('#splitter'),
    workspaceBox: box('.workspace'),
    collapsed: workspace ? workspace.classList.contains('notes-collapsed') : null,
  });
})()
"""


def evaluate(client, expression):
    result = client.command("Runtime.evaluate", {"expression": expression, "returnByValue": True,
                                                "awaitPromise": True, "timeout": 30000})
    value = result.get("result", {}).get("result", {}).get("value")
    return json.loads(value) if isinstance(value, str) else value


def verdict(shot):
    """What "left and right" means, in numbers."""
    pdf, notes, split = shot.get("pdf"), shot.get("notes"), shot.get("splitter")
    out = {"childCount": shot.get("childCount"), "tracks": shot.get("gridTemplateColumns"),
           "pdf": pdf, "notes": notes, "splitter": split, "viewport": shot.get("viewport")}
    if not pdf or not notes:
        out["ok"] = False
        out["why"] = "panel missing"
        return out
    same_row = abs(pdf["y"] - notes["y"]) <= 2
    to_the_right = notes["x"] >= pdf["x"] + pdf["w"] - 2
    both_full = pdf["h"] > 120 and notes["h"] > 120
    out["ok"] = bool(same_row and to_the_right and both_full)
    out["same_row"] = same_row
    out["notes_to_the_right"] = to_the_right
    out["both_panels_tall"] = both_full
    if split:
        centre = split["x"] + split["w"] / 2
        out["splitter_between"] = bool(pdf["x"] + pdf["w"] - 10 <= centre <= notes["x"] + 10)
    return out


def main():
    data_dir = Path(tempfile.mkdtemp(prefix="ui-layout-"))
    port = 8799
    server = subprocess.Popen([str(LAB / "runtime" / "python.exe"), str(LAB / "server.py"),
                               "--port", str(port), "--data-dir", str(data_dir), "--no-worker"],
                              cwd=str(LAB), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    browser = None
    profile = Path(tempfile.mkdtemp(prefix="ui-layout-profile-"))
    try:
        base = f"http://127.0.0.1:{port}"
        for _ in range(60):
            time.sleep(0.4)
            try:
                with urllib.request.urlopen(base + "/api/health", timeout=2) as response:
                    json.loads(response.read().decode())
                break
            except Exception:                                     # noqa: BLE001
                continue
        instance = json.loads((data_dir / "run-instance.json").read_text(encoding="utf-8"))
        capability, error = launcher_pipe.request_bootstrap(instance.get("pipe") or "")
        report["bootstrap_ok"] = bool(capability) and not error

        debug_port = 9333
        browser = subprocess.Popen([str(CHROME), "--headless=new", "--disable-gpu",
                                    "--no-first-run", "--no-default-browser-check",
                                    "--disable-extensions", "--remote-allow-origins=*",
                                    f"--window-size={WIDE[0]},{WIDE[1]}",
                                    f"--remote-debugging-port={debug_port}",
                                    f"--user-data-dir={profile}", "about:blank"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        page_url = f"{base}/#bootstrap={capability}"
        target = None
        for _ in range(60):
            time.sleep(0.25)
            try:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{debug_port}/json/new?{urllib.parse.quote(page_url, safe='')}",
                    method="PUT")
                with urllib.request.urlopen(request, timeout=5) as response:
                    target = json.loads(response.read().decode())
                break
            except Exception:                                     # noqa: BLE001
                continue
        if not target:
            report["error"] = "chrome target never appeared"
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return
        client = Client(target["webSocketDebuggerUrl"])
        client.command("Emulation.setDeviceMetricsOverride",
                       {"width": WIDE[0], "height": WIDE[1], "deviceScaleFactor": 1,
                        "mobile": False})
        time.sleep(4.0)

        report["wide"] = verdict(evaluate(client, MEASURE))
        report["wide_grid"] = evaluate(client, MEASURE).get("gridTemplateColumns")

        evaluate(client, "document.querySelector('#toggle-notes').click()")
        time.sleep(0.8)
        collapsed = evaluate(client, MEASURE)
        report["collapsed"] = {
            "notes_visible": bool(collapsed["notes"] and collapsed["notes"]["visible"]),
            "splitter_visible": bool(collapsed["splitter"] and collapsed["splitter"]["visible"]),
            "pdf_width": collapsed["pdf"]["w"] if collapsed["pdf"] else None,
            "workspace_width": collapsed["workspaceBox"]["w"] if collapsed["workspaceBox"] else None,
        }
        evaluate(client, "document.querySelector('#toggle-notes').click()")
        time.sleep(0.8)

        client.command("Emulation.setDeviceMetricsOverride",
                       {"width": 900, "height": 900, "deviceScaleFactor": 1, "mobile": False})
        time.sleep(1.2)
        narrow = evaluate(client, MEASURE)
        report["narrow"] = {
            "splitter_visible": bool(narrow["splitter"] and narrow["splitter"]["visible"]),
            "stacked": bool(narrow["pdf"] and narrow["notes"]
                            and narrow["notes"]["y"] >= narrow["pdf"]["y"] + narrow["pdf"]["h"] - 4),
            "grid": narrow["gridTemplateColumns"],
        }
        client.command("Emulation.clearDeviceMetricsOverride")
        # Back to a wide viewport: the ratio must return to the default, not stay stuck at the
        # value the narrow window forced (the "ratchet" defect: a page that first laid out in a
        # small window kept that clamped ratio forever).
        client.command("Emulation.setDeviceMetricsOverride",
                       {"width": WIDE[0], "height": WIDE[1], "deviceScaleFactor": 1,
                        "mobile": False})
        time.sleep(1.2)
        restored = evaluate(client, MEASURE)
        workspace_width = restored["workspaceBox"]["w"] or 1
        share = round(restored["pdf"]["w"] / workspace_width * 100, 1)
        report["after_narrow_restore"] = {
            "pdf_share_percent": share, "tracks": restored["gridTemplateColumns"],
            "expected_default": DEFAULT_SPLIT,
            "ok": abs(share - DEFAULT_SPLIT) <= 2,
        }
        # The AI settings screen: the figure controls must sit inline, not stretched across the
        # row (a generic `.modal-body label input{width:100%}` used to win over the inline rule).
        client.command("Runtime.evaluate",
                       {"expression": "document.querySelector('#settings-button').click()"})
        time.sleep(1.2)
        client.command("Runtime.evaluate", {
            "expression": "document.querySelector('#ai-vision').scrollIntoView({block:'center'})"})
        time.sleep(0.8)
        report["settings"] = evaluate(client, r"""
        (() => {
          const input = document.querySelector('#ai-max-figures');
          const label = input.closest('label');
          const body = document.querySelector('#settings-dialog .modal-body');
          const box = (el) => { const r = el.getBoundingClientRect();
            return {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width),
                    h: Math.round(r.height)}; };
          return JSON.stringify({
            input: box(input), label: box(label),
            same_row: Math.abs(box(input).y - box(label).y) <= 24,
            input_compact: box(input).w <= 120,
            body_overflows: body.scrollWidth > body.clientWidth + 1,
            vision_checked: document.querySelector('#ai-vision').checked,
            vision_label: document.querySelector('#ai-vision').closest('label').textContent.trim(),
          });
        })()
        """)
        client.command("Runtime.evaluate",
                       {"expression": "document.querySelector('#settings-dialog').close()"})
        # The left control panel collapses from the control that now lives *inside* the sidebar,
        # and the always-reachable rail handle must survive the collapse: at the sidebar's seam
        # while expanded, at the window's left edge once collapsed.
        RAIL = r"""
        (() => {
          const rail = document.querySelector('#sidebar-rail');
          const side = document.querySelector('#sidebar');
          const r = rail.getBoundingClientRect(), s = side.getBoundingClientRect();
          const style = getComputedStyle(rail);
          return JSON.stringify({
            visible: style.display !== 'none' && r.width > 0,
            x: Math.round(r.x), w: Math.round(r.width),
            sidebar_right: Math.round(s.x + s.width),
            sidebar_visible: getComputedStyle(side).display !== 'none',
            at_seam: Math.abs(r.x - (s.x + s.width)) <= 3,
          });
        })()
        """
        report["rail_expanded"] = evaluate(client, RAIL)
        client.command("Runtime.evaluate",
                       {"expression": "document.querySelector('#toggle-sidebar').click()"})
        time.sleep(1.0)
        collapsed_panel = evaluate(client, MEASURE)
        collapsed_rail = evaluate(client, RAIL)
        report["rail_collapsed"] = collapsed_rail
        report["sidebar_collapsed"] = {
            "rail_visible": collapsed_rail["visible"],
            "rail_at_left_edge": collapsed_rail["x"] <= 1,
            "sidebar_visible": evaluate(client, r"""
            (() => { const el = document.querySelector('#sidebar');
              const r = el.getBoundingClientRect();
              return JSON.stringify({visible: getComputedStyle(el).display !== 'none' && r.width > 0});
            })()
            """)["visible"],
            "main_x": evaluate(client, r"""
            (() => { const r = document.querySelector('.main').getBoundingClientRect();
              return JSON.stringify({x: Math.round(r.x), w: Math.round(r.width)});
            })()
            """)["x"],
            "page_width": collapsed_panel["workspaceBox"]["w"] if collapsed_panel["workspaceBox"] else None,
            "still_side_by_side": bool(collapsed_panel["pdf"] and collapsed_panel["notes"]
                                       and abs(collapsed_panel["pdf"]["y"] - collapsed_panel["notes"]["y"]) <= 2
                                       and collapsed_panel["notes"]["x"] > collapsed_panel["pdf"]["x"]),
            "stored": evaluate(client, r"""
            (() => JSON.stringify({v: localStorage.getItem('paper-reader:sidebar-collapsed')}))()
            """)["v"],
        }
        client.command("Runtime.evaluate",
                       {"expression": "document.querySelector('#toggle-sidebar').click()"})
        time.sleep(0.8)
        restored_panel = evaluate(client, MEASURE)
        report["sidebar_restored"] = {
            "sidebar_visible": evaluate(client, r"""
            (() => { const el = document.querySelector('#sidebar');
              return JSON.stringify({visible: getComputedStyle(el).display !== 'none'});
            })()
            """)["visible"],
            "workspace_width": restored_panel["workspaceBox"]["w"] if restored_panel["workspaceBox"] else None,
        }
    finally:
        if browser is not None:
            browser.terminate()
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
        import shutil
        shutil.rmtree(profile, ignore_errors=True)
        shutil.rmtree(data_dir, ignore_errors=True)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
