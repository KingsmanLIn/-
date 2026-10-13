"""Resource isolation for PDF parsing; this is not a native-code execution sandbox."""
from __future__ import annotations
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

EXTRACT_TIMEOUT_SECONDS = 120
MAX_CHILD_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_MEMORY_BYTES = 512 * 1024 * 1024
MAX_CPU_SECONDS = 60
MAX_PDF_BYTES = 100 * 1024 * 1024
CHILD_FLAG = '--extract-for-verify'

CHILD_SCRIPT = r'''
import os, sys, time
from pathlib import Path
gate = Path(sys.argv[5])
deadline = time.monotonic() + 10
while not gate.exists():
    if time.monotonic() >= deadline:
        sys.exit(3)
    time.sleep(.01)
if os.name != 'nt':
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (512*1024*1024, 512*1024*1024))
    resource.setrlimit(resource.RLIMIT_CPU, (60, 60))
    resource.setrlimit(resource.RLIMIT_FSIZE, (16*1024*1024, 16*1024*1024))
sys.path.insert(0, sys.argv[1])
import extraction, json
result = extraction.PdfTextExtractor(allow_cache=False).extract(
    sys.argv[2], paper_id=sys.argv[3], use_cache=False)
if len(result.pages) > 3000 or result.total_chars > 2000000:
    sys.exit(4)
payload = {'code': result.code, 'page_count': len(result.pages),
           'chars': result.total_chars, 'backend': result.backend,
           'cache': result.as_cache_payload('') if result.ok else None}
with Path(sys.argv[4]).open('wb') as f:
    size = 0
    for piece in json.JSONEncoder(ensure_ascii=False).iterencode(payload):
        block = piece.encode('utf-8')
        size += len(block)
        if size > 16*1024*1024:
            sys.exit(4)
        f.write(block)
'''


def scrub_environment(extra=None):
    system = os.environ.get('SystemRoot', '')
    environment = {
        'PATH': os.pathsep.join(filter(None, [str(Path(sys.executable).parent),
                               str(Path(system) / 'System32') if system else '/usr/bin'])),
        'SystemRoot': system,
        'TEMP': tempfile.gettempdir(), 'TMP': tempfile.gettempdir(),
        'PYTHONIOENCODING': 'utf-8', 'PYTHONDONTWRITEBYTECODE': '1',
        'PAPER_READER_NO_DIALOG': '1', 'NO_PROXY': '*',
    }
    for key in ('TEMP', 'TMP'):
        if extra and key in extra:
            environment[key] = str(extra[key])
    return {key: value for key, value in environment.items() if value}


def _windows_job(process):
    """Assign the waiting child before giving it permission to parse the PDF."""
    import ctypes
    from ctypes import wintypes
    class IO(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in
                    ('read_ops', 'write_ops', 'other_ops', 'read_bytes', 'write_bytes', 'other_bytes')]
    class BASIC(ctypes.Structure):
        _fields_ = [('ProcessTime', ctypes.c_longlong), ('JobTime', ctypes.c_longlong),
                    ('Flags', wintypes.DWORD), ('MinWorkingSet', ctypes.c_size_t),
                    ('MaxWorkingSet', ctypes.c_size_t), ('ActiveProcesses', wintypes.DWORD),
                    ('Affinity', ctypes.c_size_t), ('Priority', wintypes.DWORD),
                    ('Scheduling', wintypes.DWORD)]
    class EXTENDED(ctypes.Structure):
        _fields_ = [('Basic', BASIC), ('Io', IO), ('ProcessMemory', ctypes.c_size_t),
                    ('JobMemory', ctypes.c_size_t), ('PeakProcess', ctypes.c_size_t),
                    ('PeakJob', ctypes.c_size_t)]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                              ctypes.c_void_p, wintypes.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateJobObjectW(None, None)
    if not handle:
        raise OSError('resource limit unavailable')
    limits = EXTENDED()
    limits.Basic.Flags = 0x4 | 0x100 | 0x200 | 0x2000
    limits.Basic.JobTime = MAX_CPU_SECONDS * 10000000
    limits.ProcessMemory = limits.JobMemory = MAX_MEMORY_BYTES
    if not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)) \
            or not kernel.AssignProcessToJobObject(handle, wintypes.HANDLE(int(process._handle))):
        kernel.CloseHandle(handle)
        raise OSError('resource limit unavailable')
    return lambda: kernel.CloseHandle(handle)


def extract_in_subprocess(pdf_path, paper_id, project_root=None, timeout=EXTRACT_TIMEOUT_SECONDS,
                          python_executable=None):
    root = Path(project_root or Path(__file__).resolve().parent).resolve()
    scratch = process = close_job = None
    def failure(code):
        return None, {'code': code, 'pages': 0, 'chars': 0}, None
    try:
        source = Path(pdf_path).resolve()
        if not source.is_file():
            return failure('missing_file')
        if source.stat().st_size > MAX_PDF_BYTES:
            return failure('extract_too_large')
        scratch = Path(tempfile.mkdtemp(prefix='extract-'))
        script, output, gate = scratch / 'child.py', scratch / 'result.json', scratch / 'ready'
        script.write_text(CHILD_SCRIPT, encoding='utf-8')
        command = [python_executable or sys.executable, '-I', str(script), str(root),
                   str(source), str(paper_id), str(output), str(gate)]
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL,
                                   env=scrub_environment({'TEMP': scratch, 'TMP': scratch}),
                                   cwd=str(scratch), start_new_session=os.name != 'nt',
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if os.name == 'nt':
            close_job = _windows_job(process)
        gate.touch()
        deadline = time.monotonic() + max(.001, float(timeout))
        while process.poll() is None:
            if time.monotonic() >= deadline:
                return failure('extract_timeout')
            if output.is_file() and output.stat().st_size > MAX_CHILD_OUTPUT_BYTES:
                return failure('extract_too_large')
            try:
                process.wait(timeout=min(.05, max(.001, deadline-time.monotonic())))
            except subprocess.TimeoutExpired:
                pass
        if process.returncode != 0:
            return failure('extract_too_large' if process.returncode == 4 else 'extract_failed')
        if not output.is_file() or output.stat().st_size > MAX_CHILD_OUTPUT_BYTES:
            return failure('extract_failed')
        payload = json.loads(output.read_text(encoding='utf-8'))
        info = {'code': payload.get('code') or 'extract_failed',
                'pages': int(payload.get('page_count') or 0),
                'chars': int(payload.get('chars') or 0),
                'backend': str(payload.get('backend') or '')}
        if info['pages'] > 3000 or info['chars'] > 2000000:
            return failure('extract_too_large')
        return payload.get('cache') if info['code'] == 'ok' else None, info, None
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        return failure('extract_failed')
    finally:
        if process is not None:
            if os.name != 'nt':
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            elif process.poll() is None:
                process.kill()
            if close_job is not None:
                close_job()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)
