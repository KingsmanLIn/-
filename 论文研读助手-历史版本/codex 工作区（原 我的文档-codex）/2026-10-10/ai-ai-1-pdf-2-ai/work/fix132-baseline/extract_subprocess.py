"""Restricted, time-bounded PDF extraction for the restore path.

The in-process extractor is already bounded (object count, text density, no script execution,
no external URL fetch), but a hostile PDF still shares this process. For the restore path -
where the input is by definition untrusted - the PDF is parsed in a short-lived child process
with a scrubbed environment, a hard time limit and a bounded output, so a crash or a
deliberately slow file cannot take the service with it.

The child never receives a credential: it is started with an environment that is built from
scratch rather than inherited, and it is told nothing but the file path and the library.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

EXTRACT_TIMEOUT_SECONDS = 120
MAX_CHILD_OUTPUT_BYTES = 96 * 1024 * 1024
CHILD_FLAG = "--extract-for-verify"

CHILD_SCRIPT = r'''
"""Child entry point: extract one PDF and write the cache payload to stdout as JSON."""
import json, sys
sys.path.insert(0, sys.argv[1])
import extraction, storage

pdf_path, paper_id, out_path = sys.argv[2], sys.argv[3], sys.argv[4]
extractor = extraction.PdfTextExtractor(allow_cache=False)
result = extractor.extract(pdf_path, paper_id=paper_id, use_cache=False)
payload = {
    "code": result.code,
    "message": result.message,
    "backend": result.backend,
    "page_count": len(result.pages or []),
    "chars": result.total_chars,
    "cache": result.as_cache_payload("") if result.ok else None,
}
storage.atomic_write_json(out_path, payload)
'''


def scrub_environment(extra=None):
    """A minimal environment for a child process: no credentials, no proxy, no user profile."""
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "SystemRoot": os.environ.get("SystemRoot", ""),
        "TEMP": os.environ.get("TEMP", tempfile.gettempdir()),
        "TMP": os.environ.get("TMP", tempfile.gettempdir()),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PAPER_READER_NO_DIALOG": "1",
        "NO_PROXY": "*",
    }
    if extra:
        environment.update(extra)
    return {key: value for key, value in environment.items() if value}


def extract_in_subprocess(pdf_path, paper_id, project_root=None, timeout=EXTRACT_TIMEOUT_SECONDS,
                          python_executable=None):
    """Run the extractor in a child process. Returns the same shape as rebuild_cache.

    A timeout or a crashed child is reported as an extraction failure, never as a successful
    extraction of partial data.
    """
    root = Path(project_root or Path(__file__).resolve().parent)
    scratch = Path(tempfile.mkdtemp(prefix="extract-"))
    script = scratch / "child.py"
    output = scratch / "result.json"
    script.write_text(CHILD_SCRIPT, encoding="utf-8")
    command = [python_executable or sys.executable, str(script), str(root), str(pdf_path),
               str(paper_id), str(output)]
    try:
        completed = subprocess.run(
            command, capture_output=True, timeout=timeout, env=scrub_environment(),
            cwd=str(scratch), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return None, {"code": "extract_timeout", "pages": 0, "chars": 0}, None
    except OSError:
        return None, {"code": "extract_failed", "pages": 0, "chars": 0}, None
    if completed.returncode != 0 or not output.is_file():
        return None, {"code": "no_text_layer", "pages": 0, "chars": 0}, None
    if output.stat().st_size > MAX_CHILD_OUTPUT_BYTES:
        return None, {"code": "extract_too_large", "pages": 0, "chars": 0}, None
    try:
        payload = json.loads(output.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, {"code": "extract_failed", "pages": 0, "chars": 0}, None
    finally:
        for path in (script, output):
            try:
                path.unlink()
            except OSError:
                pass
        try:
            scratch.rmdir()
        except OSError:
            pass
    info = {"code": payload.get("code") or "", "pages": int(payload.get("page_count") or 0),
            "chars": int(payload.get("chars") or 0),
            "backend": str(payload.get("backend") or ""),
            "message": str(payload.get("message") or "")[:120]}
    if payload.get("code") != "ok" or payload.get("cache") is None:
        return None, info, None
    return payload["cache"], info, None
