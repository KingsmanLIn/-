"""Read-only helpers for the paper-reader test suite.

Rules for this whole tests/ directory (phase 0-8):
* never write into the user's real library (`data/library.json` or `data/papers/`);
  a temporary data directory is passed to a throwaway server process instead;
* the pristine release zip is never opened for writing;
* helpers here only read files and speak GET to the local service.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

TESTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_DIR.parent
SRC_ROOT = PROJECT_ROOT / "static"
DATA_DIR = PROJECT_ROOT / "data"
FIXTURES = TESTS_DIR / "fixtures"
# Frozen copies. The live data/library.json migrates forward as the project evolves, so
# tests must never use it as a version 1 fixture.
FIXTURE_LIBRARY_V1 = FIXTURES / "library-v1.json"
FIXTURE_PAPERS = DATA_DIR / "papers"

BOOT_RE = re.compile(r'<script id="boot" type="application/json">(.*?)</script>', re.S)


def fresh_library_v1(destination):
    """Copy the frozen version 1 library and its PDFs into `destination`."""
    destination = Path(destination)
    (destination / "papers").mkdir(parents=True, exist_ok=True)
    shutil.copy2(FIXTURE_LIBRARY_V1, destination / "library.json")
    for pdf in sorted(FIXTURE_PAPERS.glob("*.pdf")):
        shutil.copy2(pdf, destination / "papers" / pdf.name)
    return destination


def read_source(relpath):
    """Read a project file as UTF-8 text."""
    return (PROJECT_ROOT / relpath).read_text(encoding="utf-8")


def locate(needle, relpath, nth=1):
    """Return (line_number, line_text) of the nth occurrence of needle."""
    hits = []
    for index, line in enumerate(read_source(relpath).splitlines(), start=1):
        if needle in line:
            hits.append((index, line.strip()))
    if len(hits) < nth:
        raise AssertionError(f"{needle!r} not found ({nth}) in {relpath}")
    return hits[nth - 1]


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def get_json(url, token=None, timeout=10):
    """GET JSON. `token` may be a header token or a whole service dict.

    A service dict attaches the session cookie and the CSRF token, which is what the page
    uses now; a plain string still works as the legacy header token."""
    req = Request(url)
    if isinstance(token, dict):
        for name, value in auth_headers(token, csrf=False).items():
            req.add_header(name, value)
    elif token:
        req.add_header("X-Reader-Token", token)
    with urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def get_text(url, timeout=10):
    with urlopen(url, timeout=timeout) as response:
        return response.read().decode("utf-8")


# --- session helpers ------------------------------------------------------------------
def read_pipe_name(data_dir):
    """The public launcher pipe name for a data directory, or ''.

    A service records it next to its own library, so several instances (and a test suite that
    starts several) each ask their own pipe instead of whichever one wrote run/server.json
    last. The repository-level file is only a fallback for the installed copy.
    """
    candidates = [Path(data_dir) / "run-instance.json", PROJECT_ROOT / "run" / "server.json"]
    for candidate in candidates:
        try:
            info = json.loads(candidate.read_text(encoding="utf-8"))
        except Exception:                                        # noqa: BLE001
            continue
        name = str(info.get("pipe") or "")
        if name:
            return name
    return ""


def request_bootstrap(data_dir, timeout=5.0):
    """Ask the service for a one-time capability over its launcher pipe.

    This mirrors what launch.py does. It is the only way to obtain management authority, so a
    test that needs to act like the browser must go through it - there is no page token any
    more. Returns (capability, error).
    """
    sys.path.insert(0, str(PROJECT_ROOT))
    try:
        import launcher_pipe
    except Exception as exc:                                     # noqa: BLE001
        return "", f"import:{type(exc).__name__}"
    name = read_pipe_name(data_dir)
    if not name:
        return "", "no_pipe"
    try:
        return launcher_pipe.request_bootstrap(name, timeout=timeout)
    except Exception as exc:                                     # noqa: BLE001
        return "", f"request:{type(exc).__name__}"


def exchange_bootstrap(base, capability):
    """Exchange a capability for a session. Returns the caller's credential set."""
    body = json.dumps({"capability": capability}).encode("utf-8")
    request = Request(base + "/api/session/bootstrap", data=body, method="POST",
                      headers={"Content-Type": "application/json",
                               "Host": base.split("/")[2]})
    try:
        with urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
            cookie = response.headers.get("Set-Cookie", "").split(";", 1)[0]
            return {"token": "", "csrf": payload.get("csrf", ""), "cookie": cookie,
                    "bootstrap_error": ""}
    except HTTPError as exc:
        return {"token": "", "csrf": "", "cookie": "",
                "bootstrap_error": f"http_{exc.code}"}
    except URLError as exc:                                      # pragma: no cover
        return {"token": "", "csrf": "", "cookie": "",
                "bootstrap_error": f"url:{type(exc.reason).__name__}"}


def auth_headers(service, csrf=True):
    """Headers a browser would send for this service."""
    headers = {}
    if service.get("cookie"):
        headers["Cookie"] = service["cookie"]
    if csrf and service.get("csrf"):
        headers["X-CSRF-Token"] = service["csrf"]
    if service.get("token"):
        headers["X-Reader-Token"] = service["token"]
    return headers


@contextmanager
def local_service(data_dir=None, port=None):
    """Start a throwaway server on a private port with an isolated data directory.

    Yields a dict with the base url, token and the data directory used. The
    directory is deleted afterwards unless it was supplied by the caller.
    """
    import subprocess
    import time

    own_dir = data_dir is None
    directory = Path(data_dir) if data_dir else Path(tempfile.mkdtemp(prefix="paper-reader-test-"))
    directory.mkdir(parents=True, exist_ok=True)
    port = port or free_port()
    env = dict(os.environ, PAPER_READER_NO_BROWSER="1", PAPER_READER_NO_DIALOG="1")
    # The child's output goes to a file, never to a pipe nobody drains: a pipe that fills up
    # blocks the server mid-request (it writes a JSON line per event), which showed up as a
    # client-side connection abort during large uploads.
    log_path = directory / "server-test.log"
    log_handle = log_path.open("wb")
    proc = subprocess.Popen(
        [sys.executable, str(PROJECT_ROOT / "server.py"), "--port", str(port),
         "--data-dir", str(directory), "--max-concurrency", "32"],
        cwd=str(PROJECT_ROOT), env=env, stdout=log_handle, stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"

    def log_text():
        try:
            return log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
        except OSError:
            return ""

    try:
        state = None
        for _ in range(100):
            if proc.poll() is not None:
                raise RuntimeError("server exited early: " + log_text())
            try:
                state = get_json(base + "/api/health", timeout=1)
                break
            except (URLError, HTTPError, OSError):
                time.sleep(0.15)
        if state is None:
            raise RuntimeError("server did not become healthy")
        # The page no longer carries a management token. A caller that wants to act like the
        # browser asks the launcher pipe for a bootstrap and exchanges it - the same path
        # launch.py and the page use. Nothing hands out authority over plain HTTP.
        capability, pipe_error = request_bootstrap(directory)
        session = {"token": "", "csrf": "", "cookie": "", "bootstrap_error": pipe_error}
        if capability:
            session = exchange_bootstrap(base, capability)
        yield {"base": base, "token": session["token"], "csrf": session["csrf"],
               "cookie": session["cookie"], "data_dir": directory, "health": state,
               "bootstrap_error": session.get("bootstrap_error", "")}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_handle.close()
        if own_dir:
            shutil.rmtree(directory, ignore_errors=True)
