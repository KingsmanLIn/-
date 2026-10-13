"""Re-run the analysis of one paper on the live install and report what happens.

Used to verify the field-level repair against the paper that failed with
"模型没有按要求输出 JSON": the passes are re-sent, and if the card fails validation the repair
now asks only for the broken fields.

Usage:  runtime\\python.exe tests\\live_rerun.py <paper_id_prefix>
"""
import http.cookiejar
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

try:                                    # the console here is GBK; titles contain ﬁ, –, CJK
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

LAB = Path(r"C:\Users\ASUS\OneDrive\Desktop\论文研读助手\论文研读助手-历史版本\开发工作区（原 D-harness home）\pr-lab")
LIVE = Path(r"C:\Users\ASUS\OneDrive\Desktop\论文研读助手\1.3.8版本\论文研读助手-本地版-1.3.8-完整便携版")
sys.path.insert(0, str(LAB))
import launcher_pipe  # noqa: E402

prefix = sys.argv[1] if len(sys.argv) > 1 else ""
instance = json.loads((LIVE / "run" / "server.json").read_text(encoding="utf-8"))
base = f"http://127.0.0.1:{instance['port']}"
library = json.loads((LIVE / "data" / "library.json").read_text(encoding="utf-8"))
paper = next((item for item in library["papers"] if item["id"].startswith(prefix)), None)
if paper is None:
    sys.exit(f"no paper starts with {prefix!r}")
paper_id = paper["id"]
report = {"paper": paper["title"][:60], "id": paper_id[:8],
          "status_before": paper["analysis"]["status"]}

capability, error = launcher_pipe.request_bootstrap(instance.get("pipe") or "")
jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def call(path, payload=None, csrf=None, timeout=60):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(base + path, data=data, method="POST" if data else "GET")
    request.add_header("Content-Type", "application/json")
    if csrf:
        request.add_header("X-CSRF-Token", csrf)
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
            return response.status, json.loads(body) if body.strip().startswith(("{", "[")) else body
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")[:300]


_status, session = call("/api/session/bootstrap", {"capability": capability})
csrf = str(session.get("csrf") or "")
_status, started = call(f"/api/analysis/{paper_id}/generate", {"force": True}, csrf)
report["generate"] = started.get("run", {}).get("status") if isinstance(started, dict) else started

seen = []
deadline = time.time() + 900
while time.time() < deadline:
    time.sleep(5)
    _status, state = call("/api/state", timeout=30)
    item = next((p for p in state.get("papers", []) if p["id"] == paper_id), None)
    if item is None:
        break
    meta = item["analysis"]
    step = (meta.get("status"), meta.get("stage"), meta.get("progress"), meta.get("message"))
    if not seen or seen[-1] != step:
        seen.append(step)
        print(f"  {meta.get('status'):<16} {meta.get('progress') or 0:>3}%  "
              f"{meta.get('message', '')[:110]}", flush=True)
    if meta.get("status") in ("ready", "failed", "cancelled", "waiting_for_user"):
        break
report["transitions"] = [list(step) for step in seen]
report["status_after"] = seen[-1][0] if seen else ""
report["message_after"] = seen[-1][3] if seen else ""

if report["status_after"] == "ready":
    answer_dir = LIVE / "data" / "analyses"
    path = answer_dir / f"{paper_id}.json"
    if path.is_file():
        card = json.loads(path.read_text(encoding="utf-8"))
        report["fields_found"] = sum(1 for item in (card.get("answers") or {}).values()
                                     if item.get("status") == "found")
        report["verified_quotes"] = card.get("verification", {}).get("verified_quotes")
        report["figure_pages"] = card.get("input", {}).get("figure_pages")
else:
    failed = sorted((LIVE / "data" / "analyses").glob(f"{paper_id}*.failed.json"))
    if failed:
        record = json.loads(failed[0].read_text(encoding="utf-8"))
        report["failure_errors"] = record.get("errors")

log = LIVE / "run" / "startup.log"
if log.is_file():
    report["log_tail"] = [line for line in log.read_text(encoding="utf-8", errors="replace")
                          .splitlines()[-60:]
                          if any(marker in line for marker in
                                 ("provider_call", "repair_call", "validation_failed", "figures_",
                                  '"event": "failed"'))][-10:]
print(json.dumps(report, ensure_ascii=False, indent=2))
