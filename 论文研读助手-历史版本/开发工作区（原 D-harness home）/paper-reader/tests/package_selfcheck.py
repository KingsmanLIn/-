"""Prove the shipped package actually runs: unpack, start, talk to it as the app would.

A build that hashes correctly can still be broken - a missing module, a page that will not
load, an API that refuses its own launcher. This runs the real ZIP the way a user would:
extract to a fresh directory, start `launch.py`-style, fetch the page, pull the bootstrap over
the named pipe, exchange it for a session, and read the state.

Nothing here touches the live install or the working copy: the extraction target and the data
directory are both temporary.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(r"D:\harness home\paper-reader")
ZIP = Path(r"D:\harness home\releases\1.3.2\论文研读助手-本地版-1.3.2-全新安装.zip")


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def main():
    scratch = Path(tempfile.mkdtemp(prefix="pkg-check-"))
    app = scratch / "app"
    data = scratch / "data"
    app.mkdir(parents=True)
    print("unpacking to", scratch)
    with zipfile.ZipFile(ZIP) as archive:
        archive.extractall(app)
    roots = [item for item in app.iterdir() if item.is_dir()]
    if len(roots) == 1:
        app = roots[0]
    print("app root:", app.name)
    for name in ("server.py", "launch.py", "job_policy.py", "backup_guard.py",
                 "extract_subprocess.py", "static/app.js", "vendor/pdfjs/pdf.min.mjs"):
        assert (app / name).is_file(), f"missing from the package: {name}"
    print("required files present")

    port = free_port()
    environment = dict(os.environ)
    environment["PAPER_READER_NO_DIALOG"] = "1"
    process = subprocess.Popen(
        [sys.executable, str(app / "server.py"), "--port", str(port),
         "--data-dir", str(data), "--max-concurrency", "8"],
        cwd=str(app), env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        deadline = time.time() + 60
        record = data / "run-instance.json"
        while time.time() < deadline and not record.is_file():
            if process.poll() is not None:
                output = process.stdout.read().decode("utf-8", "replace")
                raise SystemExit(f"server exited early:\n{output[-2000:]}")
            time.sleep(0.2)
        assert record.is_file(), "the service never wrote its instance record"
        info = json.loads(record.read_text(encoding="utf-8"))
        print("instance record:", sorted(info))
        assert not any(key in info for key in ("token", "secret", "key")), info

        base = f"http://127.0.0.1:{port}"

        def get(path, headers=None, method="GET", body=None, health=True):
            request = Request(base + path, method=method,
                              data=json.dumps(body).encode() if body else None,
                              headers=dict({"Content-Type": "application/json"},
                                           **(headers or {})))
            try:
                with urlopen(request, timeout=15) as response:
                    return response.status, dict(response.headers), response.read()
            except Exception as exc:                              # noqa: BLE001
                code = getattr(exc, "code", 0)
                return code, {}, getattr(exc, "read", lambda: b"")()

        status, headers, body = get("/api/health")
        health = json.loads(body.decode("utf-8"))
        print(f"health: {status} version={health.get('version')} root={health.get('root')}")
        assert status == 200 and health.get("version") == "1.3.2", health

        status, headers, body = get("/")
        page = body.decode("utf-8")
        print(f"page: {status} cache-control={headers.get('Cache-Control')} "
              f"bytes={len(page)} has_token={'X-Reader-Token' in page}")
        assert status == 200 and headers.get("Cache-Control") == "no-store"
        assert "X-Reader-Token" not in page, "the page must not carry a management token"
        assert "session-hint" in page

        status, _headers, body = get("/api/state")
        print("unauthenticated /api/state:", status)
        assert status == 403, status

        sys.path.insert(0, str(ROOT))
        sys.path.insert(0, str(ROOT / "tests"))
        from baseline_helpers import exchange_bootstrap, read_pipe_name, request_bootstrap
        pipe = read_pipe_name(data)
        print("pipe from the instance record:", bool(pipe))
        capability, bootstrap_error = request_bootstrap(data)
        assert capability, f"no capability over the pipe: {bootstrap_error}"
        service = exchange_bootstrap(base, capability)
        assert service.get("csrf") and service.get("cookie"), service
        print("session established, csrf present:", bool(service.get("csrf")))

        def authed(path, headers=None, method="GET", body=None):
            merged = {"Cookie": service.get("cookie", ""),
                      "X-CSRF-Token": service.get("csrf", "")}
            merged.update(headers or {})
            return get(path, headers=merged, method=method, body=body)

        status, _headers, body = authed("/api/state")
        state = json.loads(body.decode("utf-8"))
        print(f"authenticated /api/state: {status} papers={len(state.get('papers', []))}")
        assert status == 200 and state.get("papers") is not None

        status, _headers, body = authed("/api/ai-settings")
        settings = json.loads(body.decode("utf-8"))
        print("ai settings keys:", sorted(settings["ai"]))
        assert "limits" in settings["ai"] and settings["pause"] == {"paused": False}

        status, _headers, body = authed("/api/ai/pause", method="POST", body={"paused": True})
        assert json.loads(body.decode("utf-8"))["paused"] is True
        status, _headers, body = authed("/api/analysis")
        analysis = json.loads(body.decode("utf-8"))
        print("paused outbound:", analysis["outbound"])
        assert analysis["outbound"]["blocked"] is True
        authed("/api/ai/pause", method="POST", body={"paused": False})
        print("pause round-trip OK")

        os.environ["PAPER_READER_NO_DIALOG"] = "1"
        print("\nPACKAGE SELF-CHECK: PASS")
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    main()
