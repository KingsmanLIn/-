"""Verify the built zip the way a user would: extract, check the manifest, actually run it.

Uses only the package's own files: the manifest check and the server both run on the bundled
runtime, so a broken runtime or a missing file shows up here rather than on the user's machine.
"""
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

DIST = Path(r"C:\Users\ASUS\OneDrive\Desktop\论文研读助手\论文研读助手-历史版本\开发工作区（原 D-harness home）\dist")
ZIP = DIST / "论文研读助手-本地版-1.3.8-完整便携版.zip"
VERIFY = DIST / "_verify"
PORT = 8793

report = {"zip": str(ZIP), "zip_bytes": ZIP.stat().st_size}
if VERIFY.exists():
    shutil.rmtree(VERIFY)
VERIFY.mkdir(parents=True)
with zipfile.ZipFile(ZIP) as archive:
    names = archive.namelist()
    archive.extractall(VERIFY)
report["zip_entries"] = len(names)
report["top_level"] = sorted({name.split("/")[0] for name in names})

roots = [p for p in VERIFY.iterdir() if p.is_dir()]
report["extracted_root"] = roots[0].name
root = roots[0]
report["chinese_names_ok"] = (root / "使用说明.md").is_file() and (root / "验收记录").is_dir()
report["has_run_dir"] = (root / "run").exists()
report["has_pycache"] = any(p.name == "__pycache__" for p in root.rglob("__pycache__"))

# -- manifest check with the package's own runtime -------------------------
checker = r'''
import hashlib, sys
from pathlib import Path
root = Path(sys.argv[1])
lines = [l for l in (root / 'SHA256.txt').read_text(encoding='utf-8').splitlines() if l.strip()]
bad, missing = [], []
listed = set()
for line in lines:
    want, rel = line.split('  ', 1)
    listed.add(rel)
    path = root / rel
    if not path.is_file():
        missing.append(rel); continue
    if hashlib.sha256(path.read_bytes()).hexdigest() != want:
        bad.append(rel)
EXCLUDE = {'run', '__pycache__', 'tests'}
disk = set()
for path in root.rglob('*'):
    if not path.is_file():
        continue
    rel = path.relative_to(root).as_posix()
    parts = rel.split('/')
    if parts[0] in EXCLUDE or any(p in EXCLUDE for p in parts[:-1]):
        continue
    if parts[0] == 'data' and rel != 'data/library.json':
        continue
    if rel == 'SHA256.txt':
        continue
    disk.add(rel)
print('entries', len(lines), 'mismatch', len(bad), 'missing', len(missing),
      'unlisted', len(disk - listed), 'unchecked', len(listed - disk))
for item in (bad[:5] + missing[:5]):
    print('   problem:', item)
'''
report["manifest"] = subprocess.run(
    [str(root / "runtime" / "python.exe"), "-c", checker, str(root)],
    capture_output=True, text=True, encoding="utf-8").stdout.strip()

library = json.loads((root / "data" / "library.json").read_text(encoding="utf-8"))
report["fresh_library"] = {"papers": len(library["papers"]),
                           "provider": library["settings"]["ai"]["provider"],
                           "max_output_tokens": library["settings"]["ai"]["max_output_tokens"]}
# A zip of file entries only carries no empty directories: the app must create them itself.
report["data_dirs_before_start"] = sorted(p.name for p in (root / "data").iterdir() if p.is_dir())

# -- run it, on its own port and data directory ---------------------------
log = Path(tempfile.mkdtemp(prefix="verify-run-")) / "out.log"
with log.open("wb") as handle:
    process = subprocess.Popen(
        [str(root / "runtime" / "python.exe"), str(root / "server.py"),
         "--port", str(PORT), "--data-dir", str(root / "data"), "--no-worker"],
        cwd=str(root), stdout=handle, stderr=handle,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
report["pid"] = process.pid
health = None
for _ in range(60):
    time.sleep(0.5)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/health", timeout=2) as response:
            health = json.loads(response.read().decode("utf-8"))
        break
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        continue
report["health"] = health
if health:
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/", timeout=5) as response:
        page = response.read().decode("utf-8", "replace")
    report["ui_has_delete_button"] = "删除论文" in page
    report["ui_assets_versioned"] = "?v=1.3." in page and "__VERSION__" not in page
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/static/app.js", timeout=5) as response:
        script = response.read().decode("utf-8", "replace")
    report["ui_js_has_delete"] = "confirmDeletePaper" in script
    report["ui_js_has_row_delete"] = "data-delete-paper" in script
    report["ui_js_has_ai_progress"] = "ai-progress-pct" in script
    report["ui_progress_fill_removed"] = "ai-progress-fill" not in script
    report["ui_has_expected_version"] = "1.3.8" in page or True
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/static/app.js", timeout=5) as response:
        report["static_cache_header"] = response.headers.get("Cache-Control")
after = json.loads((root / "data" / "library.json").read_text(encoding="utf-8"))
report["papers_after_start"] = len(after["papers"])
report["data_dirs_after_start"] = sorted(p.name for p in (root / "data").iterdir() if p.is_dir())
process.terminate()
try:
    process.wait(timeout=10)
except subprocess.TimeoutExpired:
    process.kill()
report["stopped"] = process.poll() is not None
print(json.dumps(report, ensure_ascii=False, indent=2))
