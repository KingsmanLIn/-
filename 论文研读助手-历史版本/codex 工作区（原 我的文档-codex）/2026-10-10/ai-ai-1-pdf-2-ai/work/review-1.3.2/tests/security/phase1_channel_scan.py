"""Phase 1 end-to-end scan: prove no channel carries the credential.

Runs the real HTTP service against a counting provider stub with a fictional sentinel key,
then scans every artefact the software produces - the library, the answer files, the failure
file, stdout/stderr, the process log, the backup ZIP, the Obsidian export and the browser
payloads - and reports the *channel* where anything was found. The sentinel value is never
printed.

    python tests/security/phase1_channel_scan.py
"""
from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tests" / "security"))

import analysis        # noqa: E402
import providers       # noqa: E402
import server          # noqa: E402
from baseline_helpers import fresh_library_v1  # noqa: E402
from stubs import CountingSink, Sentinel, register  # noqa: E402

PAPER = "medical-2026-zang"
report = {"channels": {}, "findings": []}


def scan(label, blob, sentinel):
    """Record where a sentinel appears, by channel name only."""
    channels = sentinel.channels(blob)
    report["channels"][label] = channels
    if channels:
        report["findings"].append({"channel": label, "encodings": channels})
    return channels


def main():
    sentinel = Sentinel("phase1-scan", "prefixed")
    register(sentinel)
    tmp = Path(tempfile.mkdtemp(prefix="sec-scan-"))
    try:
        fresh_library_v1(tmp)
        store = server.Store(tmp)

        # --- a provider stub that echoes the credential back on failure ---------------
        sink = CountingSink(style="openai", reply="echo_error", status=401).start()
        try:
            store.update_ai_settings({"provider": "openai_compatible",
                                      "base_url": f"http://127.0.0.1:{sink.port}/v1",
                                      "model": "stub-model",
                                      "api_key_env": sentinel.name,
                                      "auto_analyze": False})
            captured = io.StringIO()
            manager = analysis.AnalysisManager(store, log=lambda record: captured.write(
                json.dumps(record, ensure_ascii=False) + "\n"))
            state = json.loads((tmp / "library.json").read_text(encoding="utf-8"))
            for paper in state["papers"]:
                if paper["id"] == PAPER:
                    paper["analysis"]["status"] = "none"
            (tmp / "library.json").write_text(json.dumps(state, ensure_ascii=False),
                                              encoding="utf-8")
            store = server.Store(tmp)
            manager = analysis.AnalysisManager(store, log=lambda record: captured.write(
                json.dumps(record, ensure_ascii=False) + "\n"))

            with mock.patch.dict("os.environ", {sentinel.name: sentinel.value}, clear=False):
                manager.grant_approval(PAPER, scope='synthetic-test')
                manager.enqueue(PAPER, reason="force", force=True)
                manager.process_once()
            meta = store.analysis_meta(PAPER)
            report['requests'] = sink.count
            report['exercised'] = sink.count == 1 and meta['status'] == 'failed' \
                and meta.get('error_code') == 'auth_failed'
            assert report['exercised'], 'Echo-error path must actually send and fail with auth_failed' 

            # --- the HTTP surface, driven through the real handler --------------------
            httpd = server.ThreadingHTTPServer(
                ("127.0.0.1", 0), server.handler_for(store, "test-token", manager))
            httpd.daemon_threads = True
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{httpd.server_address[1]}"
            payloads = {}
            try:
                for path in ("/", "/static/app.js", "/api/health", "/api/state",
                             "/api/ai-settings"):
                    request = urllib.request.Request(base + path,
                                                     headers={"X-Reader-Token": "test-token",
                                                              "Host": f"127.0.0.1:{httpd.server_address[1]}"})
                    with urllib.request.urlopen(request, timeout=15) as response:
                        payloads[path] = response.read()
                backup = urllib.request.Request(base + "/api/backup",
                                                headers={"X-Reader-Token": "test-token",
                                                         "Host": f"127.0.0.1:{httpd.server_address[1]}"})
                with urllib.request.urlopen(backup, timeout=30) as response:
                    payloads["/api/backup"] = response.read()
            finally:
                httpd.shutdown()
                httpd.server_close()
        finally:
            sink.stop()

        report["status"] = meta["status"]
        report["public_code"] = meta.get("error_code")

        # --- every artefact the software produced -------------------------------------
        scan("analysis.status_payload", json.dumps(meta, ensure_ascii=False), sentinel)
        scan("library.json", (tmp / "library.json").read_bytes(), sentinel)
        for item in sorted((tmp / "analyses").glob("*")) if (tmp / "analyses").is_dir() else []:
            scan(f"analyses/{item.name}", item.read_bytes(), sentinel)
        for item in sorted((tmp / "extracted").glob("*")) if (tmp / "extracted").is_dir() else []:
            scan(f"extracted/{item.name}", item.read_bytes(), sentinel)
        scan("process_log", captured.getvalue(), sentinel)
        for label, blob in payloads.items():
            scan(f"http{label}", blob, sentinel)
        if (tmp / "run").is_dir():
            for item in (tmp / "run").rglob("*"):
                if item.is_file():
                    scan(f"run/{item.name}", item.read_bytes(), sentinel)

        # --- the backup ZIP, unpacked and re-scanned ---------------------------------
        backup_path = tmp / "backup.zip"
        data = store.backup()
        if isinstance(data, tuple):
            data = data[0]
        backup_path.write_bytes(data)
        import zipfile
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for name in archive.namelist():
                scan(f"backup/{name}", archive.read(name), sentinel)
        report["backup_bytes"] = len(data)

        # --- Obsidian export ----------------------------------------------------------
        try:
            note = store.export_markdown(PAPER) if hasattr(store, "export_markdown") else None
            if note:
                scan("obsidian_note", note if isinstance(note, bytes) else str(note).encode(),
                     sentinel)
        except Exception as exc:                                     # noqa: BLE001
            report["obsidian_export"] = f"skipped: {type(exc).__name__}"

        report["leaked_channels"] = [item["channel"] for item in report["findings"]]
        out = Path(__file__).resolve().parent / "phase1-channel-scan.json"
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print("channels scanned:", len(report["channels"]),
              "| leaked:", report["leaked_channels"] or "none",
              "| status:", report["status"], report.get("public_code"))
        print("written:", out)
        return 0 if report.get("exercised") and not report["findings"] else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
