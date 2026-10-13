"""End-to-end HTTP check on the lab instance: import a real PDF, then delete it.

Exercises the real routes with a real cookie session obtained the way the launcher does it.
Writes only inside the lab copy (data dir of the 8790 instance).
"""
import json
import sys
import urllib.error
import urllib.request
import http.cookiejar
from pathlib import Path

ROOT = Path(r"C:\Users\ASUS\OneDrive\Desktop\论文研读助手\论文研读助手-历史版本\开发工作区（原 D-harness home）\pr-lab")
OUT = Path(r"C:\Users\ASUS\OneDrive\Desktop\论文研读助手\论文研读助手-历史版本\开发工作区（原 D-harness home）\pr-lab\run\e2e-report.json")
sys.path.insert(0, str(ROOT))
import launcher_pipe  # noqa: E402

report = {}
info = json.loads((ROOT / "run" / "server.json").read_text(encoding="utf-8"))
base = f"http://127.0.0.1:{int(info['port'])}"
report["port"] = info["port"]
report["pid"] = info["pid"]

jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def call(path, payload=None, headers=None, raw=None, method=None):
    if raw is not None:
        data = raw
    elif payload is not None:
        data = json.dumps(payload).encode("utf-8")
    else:
        data = None
    request = urllib.request.Request(base + path, data=data,
                                     method=method or ("POST" if data is not None else "GET"))
    if raw is None:
        request.add_header("Content-Type", "application/json")
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    try:
        with opener.open(request, timeout=90) as response:
            body = response.read().decode("utf-8", "replace")
            return response.status, (json.loads(body) if body.strip().startswith(("{", "[")) else body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, body[:300]


capability, error = launcher_pipe.request_bootstrap(str(info.get("pipe") or ""))
report["bootstrap"] = bool(capability) or error
status, body = call("/api/session/bootstrap", {"capability": capability})
report["session_status"] = status
csrf = str(body.get("csrf") or "") if isinstance(body, dict) else ""
report["has_csrf"] = bool(csrf)

status, html = call("/")
report["index_status"] = status
report["index_has_delete_button"] = isinstance(html, str) and "删除论文" in html
report["index_has_delete_dialog"] = isinstance(html, str) and "delete-dialog" in html
status, js = call("/static/app.js")
report["app_js_status"] = status
report["app_js_has_delete"] = isinstance(js, str) and "confirmDeletePaper" in js

status, body = call("/api/delete", {"paper_id": "not-a-valid-id"},
                    {"X-CSRF-Token": csrf})
report["delete_bad_id"] = {"status": status, "body": body}

status, body = call("/api/delete", {"paper_id": ""}, {"X-CSRF-Token": csrf})
report["delete_empty_id"] = {"status": status, "body": body}

source = sorted((ROOT / "data" / "papers").glob("*.pdf"))[0]
blob = source.read_bytes()
report["import_source"] = source.name
report["import_bytes"] = len(blob)
status, body = call("/api/import", headers={"X-File-Name": "e2e-delete-test.pdf",
                                            "X-CSRF-Token": csrf}, raw=blob)
report["import_status"] = status
paper_id = str((body or {}).get("paper", {}).get("id") or "") if isinstance(body, dict) else ""
report["imported_paper_id"] = paper_id
report["imported_title"] = (body or {}).get("paper", {}).get("title") if isinstance(body, dict) else None
report["imported_metadata"] = ({key: (body or {}).get("paper", {}).get(key)
                               for key in ("authors", "year", "venue", "doi")}
                              if isinstance(body, dict) else None)

files = {"pdf": ROOT / "data" / "papers" / f"{paper_id}.pdf",
         "analysis": ROOT / "data" / "analyses" / f"{paper_id}.json",
         "extracted": ROOT / "data" / "extracted" / f"{paper_id}.json"}
report["files_before"] = {name: path.is_file() for name, path in files.items()}
if files["extracted"].parent.is_dir():
    for path in files["extracted"].parent.glob(f"{paper_id}*"):
        report.setdefault("extracted_extras", []).append(path.name)

status, body = call("/api/delete", {"paper_id": paper_id}, {"X-CSRF-Token": csrf})
report["delete_status"] = status
report["delete_body_deleted"] = (body or {}).get("deleted") if isinstance(body, dict) else body
report["delete_body_title"] = (body or {}).get("title") if isinstance(body, dict) else None
state = (body or {}).get("state") or {}
report["paper_gone_from_state"] = paper_id not in [p.get("id") for p in state.get("papers", [])]
report["files_after"] = {name: path.is_file() for name, path in files.items()}

status, body = call("/api/delete", {"paper_id": paper_id}, {"X-CSRF-Token": csrf})
report["second_delete"] = {"status": status, "body": body}

OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print("report:", OUT)
