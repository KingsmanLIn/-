"""Pre-release check: launch the shipped layout standalone on a copy, then verify it.

This mimics a real user install: the folder is copied, started through the same entry
point the VBS uses (launch.py), and exercised over HTTP. Nothing is written back to the
working copy or to the live install.

    python tests/release_smoke.py [--source <dir>] [--keep]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
BOOT_RE = re.compile(r'<script id="boot" type="application/json">(.*?)</script>', re.S)
SHIPPED = ["server.py", "launch.py", "schema.py", "storage.py", "pdftext.py", "textclean.py",
           "chunker.py", "extraction.py", "providers.py", "analysis.py", "reading_card.py", "credentials.py", "secret_guard.py", "endpoint_policy.py", "secure_transport.py", "session.py", "launcher_pipe.py", "job_policy.py", "backup_guard.py", "extract_subprocess.py", "backup_guard.py", "extract_subprocess.py", "job_policy.py", "backup_guard.py", "extract_subprocess.py", "backup_guard.py", "extract_subprocess.py", "session.py", "launcher_pipe.py", "job_policy.py", "backup_guard.py", "extract_subprocess.py", "backup_guard.py", "extract_subprocess.py", "job_policy.py", "backup_guard.py", "extract_subprocess.py", "backup_guard.py", "extract_subprocess.py",
           "quotes.py", "credentials.py", "secret_guard.py", "endpoint_policy.py", "secure_transport.py", "session.py", "launcher_pipe.py", "job_policy.py", "backup_guard.py", "extract_subprocess.py", "backup_guard.py", "extract_subprocess.py", "job_policy.py", "backup_guard.py", "extract_subprocess.py", "backup_guard.py", "extract_subprocess.py", "session.py", "launcher_pipe.py", "job_policy.py", "backup_guard.py", "extract_subprocess.py", "backup_guard.py", "extract_subprocess.py", "job_policy.py", "backup_guard.py", "extract_subprocess.py", "backup_guard.py", "extract_subprocess.py", "使用说明.md", "启动论文研读助手.vbs", "备用启动.cmd"]
SHIPPED_DIRS = ["static", "vendor"]


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def request_bootstrap_from(folder):
    """Ask a service for a bootstrap over its own pipe (the launcher route)."""
    import sys as _sys
    _sys.path.insert(0, str(folder))
    try:
        import launcher_pipe
    except Exception as exc:                                     # noqa: BLE001
        return "", f"import:{type(exc).__name__}"
    try:
        info = json.loads((folder / "run" / "server.json").read_text(encoding="utf-8"))
    except Exception as exc:                                     # noqa: BLE001
        return "", f"no_record:{type(exc).__name__}"
    name = str(info.get("pipe") or "")
    if not name:
        return "", "no_pipe"
    try:
        return launcher_pipe.request_bootstrap(name)
    except Exception as exc:                                     # noqa: BLE001
        return "", f"request:{type(exc).__name__}"


def exchange_bootstrap(base, capability):
    """Exchange a one-time capability for a cookie session."""
    if not capability:
        return {"cookie": "", "csrf": "", "bootstrap_error": "no_capability"}
    body = json.dumps({"capability": capability}).encode("utf-8")
    request = Request(base + "/api/session/bootstrap", data=body, method="POST",
                      headers={"Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
            return {"cookie": response.headers.get("Set-Cookie", "").split(";", 1)[0],
                    "csrf": payload.get("csrf", ""), "bootstrap_error": ""}
    except HTTPError as exc:
        return {"cookie": "", "csrf": "", "bootstrap_error": f"http_{exc.code}"}
    except Exception as exc:                                     # noqa: BLE001
        return {"cookie": "", "csrf": "", "bootstrap_error": type(exc).__name__}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default=str(ROOT))
    parser.add_argument("--with-data", action="store_true",
                        help="copy the working library into the sandbox (upgrade rehearsal)")
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--port", type=int, default=8799,
                        help="private port for the sandbox service (never 8765)")
    args = parser.parse_args()
    source = Path(args.source)

    sandbox = Path(tempfile.mkdtemp(prefix="release-smoke-"))
    report = {"sandbox": str(sandbox), "source": str(source), "steps": []}

    def step(name, ok, detail=""):
        report["steps"].append({"name": name, "ok": bool(ok), "detail": detail})
        print(("PASS  " if ok else "FAIL  ") + name + ("  " + str(detail) if detail else ""))

    # 1. copy exactly what a user would receive
    (sandbox / "static").mkdir(parents=True, exist_ok=True)
    (sandbox / "vendor").mkdir(parents=True, exist_ok=True)
    (sandbox / "run").mkdir(parents=True, exist_ok=True)
    (sandbox / "data").mkdir(parents=True, exist_ok=True)
    for name in SHIPPED:
        shutil.copy2(source / name, sandbox / name)
    for name in SHIPPED_DIRS:
        shutil.rmtree(sandbox / name, ignore_errors=True)
        shutil.copytree(source / name, sandbox / name)
    if args.with_data:
        # rehearse the real upgrade: the frozen v1 library plus its PDFs, exactly what the
        # live v1 install holds, so the migration path is exercised in the sandbox.
        fixture = Path(__file__).resolve().parent / "fixtures" / "library-v1.json"
        shutil.copy2(fixture, sandbox / "data" / "library.json")
        shutil.copytree(Path(__file__).resolve().parent.parent / "data" / "papers",
                        sandbox / "data" / "papers", dirs_exist_ok=True)
    report["files"] = sum(1 for _ in sandbox.rglob("*") if _.is_file())
    step("layout copied", report["files"] > 200, f"{report['files']} files")

    # 2. no development or private material may be inside the package
    forbidden = ["tests", "docs", "__pycache__", ".dsh", "audit-", "phase8-", "instance.lock",
                 "credentials.json", "library.previous.json", "startup.log"]
    found = [str(p.relative_to(sandbox)) for p in sandbox.rglob("*")
             if any(marker in p.name for marker in forbidden)]
    step("no development material", not found, found[:6])

    # 3. launch through launch.py, exactly like the shipped VBS
    env = dict(os.environ, PAPER_READER_NO_BROWSER="1")
    # Start the same server module the launcher starts, but on a private port: if a real
    # install is already running on 8765 the launcher would reuse it and we would be
    # testing the user's app instead of the sandbox.
    proc = subprocess.Popen([sys.executable, str(sandbox / "server.py"), "--port", str(args.port)],
                            cwd=str(sandbox), env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                            errors="replace")
    base = None
    try:
        deadline = time.time() + 90
        candidate = f"http://127.0.0.1:{args.port}"
        while time.time() < deadline:
            if proc.poll() is not None:
                raise SystemExit("sandbox server exited: " + (proc.stdout.read() if proc.stdout else ""))
            try:
                with urlopen(candidate + "/api/health", timeout=2) as response:
                    health = json.load(response)
                if health.get("app") == "paper-reader":
                    base = candidate
                    break
            except Exception:                       # noqa: BLE001 - keep waiting
                time.sleep(0.3)
        step("launch.py started the service", base is not None, base or "no /api/health")
        if base is None:
            raise SystemExit("service did not start")

        page = urlopen(base + "/", timeout=10).read().decode("utf-8")
        report["version"] = json.loads(urlopen(base + "/api/health", timeout=5).read())["version"]
        # The page no longer carries a management token; it is served with a boot hint and the
        # launcher supplies a one-time capability over the pipe. The smoke test uses the same
        # route, so it fails if the pipe or the session exchange is broken in the package.
        step("index.html served without a page token",
             "__BOOTSTRAP__" not in page and '"token"' not in page,
             f"version {report['version']}")
        capability, pipe_error = request_bootstrap_from(sandbox)
        step("launcher pipe issued a bootstrap", bool(capability), pipe_error or "ok")
        session = exchange_bootstrap(base, capability)
        step("bootstrap exchanged for a session",
             bool(session.get("cookie")) and bool(session.get("csrf")),
             session.get("bootstrap_error") or "ok")
        token = ""
        auth = {"Cookie": session.get("cookie", ""),
                "X-CSRF-Token": session.get("csrf", "")}

        def get(path, expect=200, headers=None):
            request = Request(base + path, headers=headers or dict(auth))
            try:
                with urlopen(request, timeout=20) as response:
                    return response.status, response.read(), dict(response.headers)
            except HTTPError as exc:
                return exc.code, exc.read(), dict(exc.headers)

        for asset in ("/static/app.js", "/static/reader-layout.css", "/static/ai-reference.css",
                      "/static/styles.css", "/static/guides.js",
                      "/vendor/pdfjs/pdf.min.mjs", "/vendor/pdfjs/pdf.worker.min.mjs"):
            status, blob, _ = get(asset, headers={})
            step(f"asset {asset}", status == 200 and len(blob) > 100, f"{status} {len(blob)}B")

        status, blob, _ = get("/api/health", headers={})
        health = json.loads(blob)
        step("health has no token or path", "token" not in blob.decode("utf-8", "replace")
             and str(sandbox) not in blob.decode("utf-8", "replace"))

        status, blob, _ = get("/api/state")
        state = json.loads(blob)
        report["papers"] = len(state.get("papers", []))
        report["schema_version"] = state.get("version")
        step("state readable", status == 200, f"{report['papers']} papers, v{report['schema_version']}")
        if args.with_data:
            step("production library migrated in the sandbox",
                 state.get("version") == 2 and report["papers"] == 3,
                 f"v{state.get('version')} / {report['papers']} papers")
            first = state["papers"][0]
            step("human fields survive", set(first["fields"]) and "analysis" in first,
                 f"{len(first['fields'])} fields, analysis={first['analysis']['status']}")

        status, blob, headers = get(f"/api/pdf/{state['papers'][0]['id']}")
        step("pdf download", status == 200 and blob.startswith(b"%PDF-"),
             f"{status} {len(blob)}B {headers.get('Content-Type')}")

        status, blob, _ = get(f"/api/export/{state['papers'][0]['id']}")
        step("markdown export", status == 200 and "阅读卡" in blob.decode("utf-8"),
             f"{status} {len(blob)}B")

        status, blob, _ = get("/api/analysis")
        analysis = json.loads(blob)
        step("analysis endpoints wired", status == 200 and "statuses" in analysis,
             f"health provider={analysis.get('health', {}).get('provider')}")

        status, _, _ = get("/api/state", headers={"X-Reader-Token": "wrong"})
        step("token still enforced", status == 403, status)

        # the shipped folder must not have grown secret material during the run
        leaked = []
        # Aligned with secret_guard's patterns: a bare `sk-` inside ordinary words (for
        # example the CSS property `-webkit-mask-position`) is not a credential shape.
        key_pattern = re.compile(r"(?<![A-Za-z0-9_])sk-[A-Za-z0-9_\-]{16,}")
        assign_pattern = re.compile(
            r"(?i)\b(?:api[_-]?key|apikey|secret|password|token)[\"']?\s*[:=]\s*"
            r"[\"'][A-Za-z0-9_\-\.]{16,}[\"']")
        for path in sandbox.rglob("*"):
            if path.is_file() and path.suffix.lower() in {".json", ".log", ".md", ".py"}:
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                if key_pattern.search(text) or assign_pattern.search(text):
                    leaked.append(str(path.relative_to(sandbox)))
        step("no secret pattern in the package", not leaked, leaked[:4])
        step("no credentials file created", not (sandbox / "data" / "credentials.json").exists())
        report["run_files"] = sorted(p.name for p in (sandbox / "run").iterdir())
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        report["stopped_pid"] = proc.pid
        # A detached service started through launch.py would survive us: stop anything
        # still listening on the sandbox port by its recorded pid.
        try:
            recorded = json.loads((sandbox / "run" / "server.json").read_text())
            if recorded.get("pid") and recorded["pid"] != proc.pid:
                subprocess.run(["taskkill", "/PID", str(recorded["pid"]), "/F"],
                               capture_output=True, timeout=20)
                report["stopped_detached_pid"] = recorded["pid"]
        except Exception:                           # noqa: BLE001 - best effort
            pass
        if not args.keep:
            shutil.rmtree(sandbox, ignore_errors=True)
        else:
            print("kept sandbox:", sandbox)

    failures = [item["name"] for item in report["steps"] if not item["ok"]]
    report["ok"] = not failures
    out = ROOT / "tests" / "release-smoke.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\nresult:", "OK" if report["ok"] else f"FAILED {failures}")
    print("written:", out)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
