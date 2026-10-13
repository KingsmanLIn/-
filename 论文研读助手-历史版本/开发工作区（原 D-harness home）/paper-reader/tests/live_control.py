"""Control and verify a running paper-reader instance through its own interface.

Usage:
  python live_control.py stop  "<package root>"
  python live_control.py verify "<package root>"

`stop` uses the launcher pipe to get a bootstrap capability, exchanges it for a cookie
session and POSTs /api/shutdown - the same path as the page's 保存并退出软件 button.
`verify` reads the provider status the page shows, checks the served UI, and confirms the
delete route rejects an empty id without touching the library.
"""
import json
import sys
import urllib.error
import urllib.request
import http.cookiejar
from pathlib import Path

action = sys.argv[1]
ROOT = Path(sys.argv[2]).resolve()
sys.path.insert(0, str(ROOT))
import launcher_pipe  # noqa: E402

OUT = Path(sys.argv[3]) if len(sys.argv) > 3 else None
info = json.loads((ROOT / "run" / "server.json").read_text(encoding="utf-8"))
base = f"http://127.0.0.1:{int(info['port'])}"
report = {"action": action, "port": info["port"], "pid": info["pid"]}

jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def call(path, payload=None, headers=None):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(base + path, data=data,
                                     method="POST" if data is not None else "GET")
    request.add_header("Content-Type", "application/json")
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    try:
        with opener.open(request, timeout=60) as response:
            body = response.read().decode("utf-8", "replace")
            return response.status, (json.loads(body) if body.strip().startswith(("{", "[")) else body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, body[:200]


capability, error = launcher_pipe.request_bootstrap(str(info.get("pipe") or ""))
report["bootstrap"] = bool(capability) or error
status, body = call("/api/session/bootstrap", {"capability": capability})
report["session_status"] = status
csrf = str(body.get("csrf") or "") if isinstance(body, dict) else ""
report["version"] = (body or {}).get("version") if isinstance(body, dict) else None

if action == "stop":
    report["shutdown"] = call("/api/shutdown", {}, {"X-CSRF-Token": csrf})
else:
    status, settings = call("/api/ai-settings")
    if isinstance(settings, dict):
        ai = settings.get("ai") or {}
        credential = settings.get("credential") or {}
        report["ai"] = {key: ai.get(key) for key in
                        ("provider", "mode", "base_url", "model", "max_output_tokens")}
        report["credential_state"] = credential.get("credential_state")
    status, analysis = call("/api/analysis")
    if isinstance(analysis, dict):
        health = analysis.get("health") or {}
        report["health"] = {key: health.get(key) for key in
                            ("provider", "ready", "ok", "category", "detail")}
        report["papers"] = len(analysis.get("statuses") or {})
    status, html = call("/")
    report["index_status"] = status
    report["ui_delete_button"] = isinstance(html, str) and "删除论文" in html
    report["ui_delete_dialog"] = isinstance(html, str) and "delete-dialog" in html
    status, js = call("/static/app.js")
    report["ui_js_delete"] = isinstance(js, str) and "confirmDeletePaper" in js
    report["delete_empty_id"] = call("/api/delete", {"paper_id": ""}, {"X-CSRF-Token": csrf})

text = json.dumps(report, ensure_ascii=False, indent=2)
print(text)
if OUT:
    OUT.write_text(text, encoding="utf-8")
