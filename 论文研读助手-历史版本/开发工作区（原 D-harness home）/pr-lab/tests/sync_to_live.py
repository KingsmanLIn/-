"""Sync the patched files from the lab copy to the live package (and the workspace tree).

Only production files are copied: user data (data/), logs (run/) and tests are never touched
in the live package. SHA256.txt is regenerated in place with the same coverage rule the
release used, so the package's integrity manifest stays meaningful.

Usage:
  python sync_to_live.py            # copy to the live package only
  python sync_to_live.py --workspace  # also copy code+tests into the workspace source tree
"""
import hashlib
import shutil
import sys
from pathlib import Path

LAB = Path(r"C:\Users\ASUS\OneDrive\Desktop\论文研读助手\论文研读助手-历史版本\开发工作区（原 D-harness home）\pr-lab")
LIVE = Path(r"C:\Users\ASUS\OneDrive\Desktop\论文研读助手\1.3.8版本\论文研读助手-本地版-1.3.8-完整便携版")
WORKSPACE = Path(r"C:\Users\ASUS\OneDrive\Desktop\论文研读助手\论文研读助手-历史版本\开发工作区（原 D-harness home）\paper-reader")

PRODUCTION = ("providers.py", "analysis.py", "reading_card.py", "secret_guard.py",
              "job_policy.py", "storage.py", "server.py", "metadata.py", "figures.py",
              "pdftext.py", "pdf_worker.py", "extraction.py", "schema.py", "launch.py",
              "static/app.js", "static/index.html", "static/styles.css",
              "release.json", "版本说明.md", "使用说明.md",
              "验收记录/1.3.4-改动与验证.md", "验收记录/1.3.5-改动与验证.md", "验收记录/1.3.6-改动与验证.md", "验收记录/1.3.7-改动与验证.md", "验收记录/1.3.8-改动与验证.md")

#: Upgrading the workspace tree means replacing the whole production set, not only the files
#: this change touched: 1.3.2 -> 1.3.4 also moved things between modules (for example
#: `secure_transport.OutboundDenied`), and a mixed tree cannot import at all.
WORKSPACE_PRODUCTION = tuple(sorted(path.name for path in LAB.glob("*.py"))) + (
    "release.json", "版本说明.md", "使用说明.md")

EXCLUDE_DIRS = {"run", "__pycache__", "tests"}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def copy_production(destination, names=PRODUCTION):
    copied = []
    for name in names:
        source = LAB / name
        if not source.is_file():
            print(f"missing in lab, skipped: {name}")
            continue
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        before = digest(target) if target.is_file() else ""
        shutil.copy2(source, target)
        after = digest(target)
        copied.append((name, before != after, after))
    return copied


def regenerate_manifest(root):
    lines = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        parts = rel.split("/")
        if parts[0] in EXCLUDE_DIRS or any(part in EXCLUDE_DIRS for part in parts[:-1]):
            continue
        if parts[0] == "data" and rel != "data/library.json":
            continue
        if rel == "SHA256.txt":
            continue
        lines.append(f"{digest(path)}  {rel}")
    (root / "SHA256.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(lines)


def main():
    copied = copy_production(LIVE)
    # The whole static/ folder travels together with the version-stamped index.html.
    for path in sorted((LAB / "static").glob("*")):
        if path.is_file():
            shutil.copy2(path, LIVE / "static" / path.name)
    for name, changed, _hash in copied:
        print(f"{'updated' if changed else 'same   '}  {name}")
    count = regenerate_manifest(LIVE)
    print(f"live manifest regenerated: {count} entries")

    if "--workspace" in sys.argv:
        ws_copied = copy_production(WORKSPACE, WORKSPACE_PRODUCTION)
        # The whole static/ folder goes too: the UI and its styles are one unit in this app.
        for path in sorted((LAB / "static").glob("*")):
            if path.is_file():
                shutil.copy2(path, WORKSPACE / "static" / path.name)
        cache = WORKSPACE / "__pycache__"
        if cache.is_dir():
            shutil.rmtree(cache, ignore_errors=True)
        print("workspace:")
        for name, changed, _hash in ws_copied:
            print(f"  {'updated' if changed else 'same   '}  {name}")
        target = WORKSPACE / "tests"
        target.mkdir(exist_ok=True)
        for path in sorted((LAB / "tests").glob("test_*.py")):
            shutil.copy2(path, target / path.name)
            print(f"  tests/{path.name}")
        shutil.copy2(LAB / "tests" / "live_control.py", target / "live_control.py")
        print("  tests/live_control.py")


if __name__ == "__main__":
    main()
