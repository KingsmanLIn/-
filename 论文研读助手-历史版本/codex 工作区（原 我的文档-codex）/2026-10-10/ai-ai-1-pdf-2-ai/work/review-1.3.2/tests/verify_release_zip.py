"""Verify a built release zip: extract it and exercise it as a user would.

    python tests/verify_release_zip.py "<zip path>" [--expected-library-hash <sha256>]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

BOOT_RE = re.compile(r'<script id="boot" type="application/json">(.*?)</script>', re.S)
FORBIDDEN = re.compile(r"(tests/|docs/|__pycache__|\.pyc$|audit-|phase[0-9]-|instance\.lock|"
                       r"credentials\.json|library\.previous\.json|startup\.log)")


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("zip")
    parser.add_argument("--expected-library-hash")
    parser.add_argument("--port", type=int, default=8798)
    args = parser.parse_args()

    zip_path = Path(args.zip)
    report = {"zip": str(zip_path), "checks": []}

    def check(name, ok, detail=""):
        report["checks"].append({"name": name, "ok": bool(ok), "detail": str(detail)})
        print(("PASS  " if ok else "FAIL  ") + name + (f"  {detail}" if detail else ""))

    with zipfile.ZipFile(zip_path) as archive:
        names = archive.namelist()
        bad = [name for name in names if FORBIDDEN.search(name)]
        check("archive has no development material", not bad, bad[:5])
        check("archive has one top-level folder",
              len({name.split("/")[0] for name in names}) == 1,
              sorted({name.split("/")[0] for name in names})[:3])
        check("SHA256.txt present", any(name.endswith("SHA256.txt") for name in names))
        check("release notes present", any(name.endswith("发布说明.md") for name in names))
        check("app files present",
              all(any(name.endswith(part) for name in names) for part in
                  ("server.py", "launch.py", "credentials.py", "secret_guard.py", "endpoint_policy.py", "secure_transport.py", "session.py", "launcher_pipe.py", "job_policy.py", "backup_guard.py", "extract_subprocess.py", "backup_guard.py", "extract_subprocess.py", "job_policy.py", "backup_guard.py", "extract_subprocess.py", "backup_guard.py", "extract_subprocess.py", "session.py", "launcher_pipe.py", "job_policy.py", "backup_guard.py", "extract_subprocess.py", "backup_guard.py", "extract_subprocess.py", "job_policy.py", "backup_guard.py", "extract_subprocess.py", "backup_guard.py", "extract_subprocess.py", "endpoint_policy.py", "secure_transport.py",
                   "static/app.js", "static/ai-reference.css",
                   "static/reader-layout.css", "vendor/pdfjs/pdf.min.mjs",
                   "启动论文研读助手.vbs", "使用说明.md")))
        check("library + papers present",
              any(name.endswith("data/library.json") for name in names)
              and sum(1 for name in names if name.endswith(".pdf")) == 3,
              f"{sum(1 for name in names if name.endswith('.pdf'))} pdf")
        root = Path(tempfile.mkdtemp(prefix="verify-zip-"))
        archive.extractall(root)
    folder = next(p for p in root.iterdir() if p.is_dir())
    report["extracted"] = str(folder)

    library = folder / "data" / "library.json"
    if args.expected_library_hash:
        import hashlib
        got = hashlib.sha256(library.read_bytes()).hexdigest().upper()
        check("bundled library matches the source",
              got == args.expected_library_hash.upper(), got[:24])

    env = dict(os.environ, PAPER_READER_NO_BROWSER="1")
    launcher = subprocess.Popen([sys.executable, str(folder / "server.py"), "--port", str(args.port)],
                                cwd=str(folder), env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                errors="replace")
    try:
        base = None
        deadline = time.time() + 90
        candidate = f"http://127.0.0.1:{args.port}"
        while time.time() < deadline:
            if launcher.poll() is not None:
                raise SystemExit("server exited: " + (launcher.stdout.read() if launcher.stdout else ""))
            try:
                with urlopen(candidate + "/api/health", timeout=2) as response:
                    health = json.load(response)
                if health.get("app") == "paper-reader":
                    base, version = candidate, health["version"]
                    break
            except Exception:                       # noqa: BLE001
                time.sleep(0.3)
        check("service starts from the extracted folder", base is not None, base or "no health")
        if base is None:
            raise SystemExit("service did not start")
        report["version"] = version

        def get(path, headers=None):
            request = Request(base + path, headers=headers or {"X-Reader-Token": token})
            try:
                with urlopen(request, timeout=20) as response:
                    return response.status, response.read()
            except HTTPError as exc:
                return exc.code, exc.read()

        page = urlopen(base + "/", timeout=10).read().decode("utf-8")
        token = json.loads(BOOT_RE.search(page).group(1))["token"]
        status, blob = get("/api/state")
        state = json.loads(blob)
        check("library opens", status == 200,
              f"v{state['version']} / {len(state['papers'])} papers / {version}")
        check("upgraded to schema v2", state["version"] == 2)
        paper = state["papers"][0]
        check("human fields and analysis block present",
              len(paper["fields"]) == 13 and "analysis" in paper,
              f"analysis={paper['analysis']['status']}")
        status, blob = get(f"/api/pdf/{paper['id']}")
        check("pdf served", status == 200 and blob.startswith(b"%PDF-"), f"{len(blob)}B")
        status, _ = get("/api/state", headers={"X-Reader-Token": "bad"})
        check("token enforced", status == 403, status)
        check("no credentials file created", not (folder / "data" / "credentials.json").exists())
        report["ok"] = all(item["ok"] for item in report["checks"])
    finally:
        launcher.terminate()
        try:
            launcher.wait(timeout=15)
        except subprocess.TimeoutExpired:
            launcher.kill()
        pass
        shutil.rmtree(root, ignore_errors=True)

    print("\nresult:", "OK" if report.get("ok") else "FAILED")
    out = Path(__file__).resolve().parent / "release-verify.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("written:", out)
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
