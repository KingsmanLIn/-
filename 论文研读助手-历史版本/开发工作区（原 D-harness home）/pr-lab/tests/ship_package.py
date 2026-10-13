"""Ship the fixed 1.3.8 release: sync, build, and place three deliverables on the Desktop.

  1. the extracted package folder (correctly structured, no nested folder)
  2. the zip
  3. the user guide as a standalone document

Everything is hash-compared against the staged tree, and the zip is verified separately by
`verify_package.py` (extract -> manifest check with the bundled runtime -> really run it).
"""
import hashlib
import shutil
import sys
from pathlib import Path

LAB = Path(r"C:\Users\ASUS\OneDrive\Desktop\论文研读助手\论文研读助手-历史版本\开发工作区（原 D-harness home）\pr-lab")
DIST = Path(r"C:\Users\ASUS\OneDrive\Desktop\论文研读助手\论文研读助手-历史版本\开发工作区（原 D-harness home）\dist")
STAGE = DIST / "论文研读助手-本地版-1.3.8-完整便携版"
ZIP = DIST / "论文研读助手-本地版-1.3.8-完整便携版.zip"
INSTALLER = DIST / "论文研读助手-1.3.8-安装程序.exe"
DESK = Path(r"C:\Users\ASUS\OneDrive\Desktop")
DESK_FOLDER = DESK / "论文研读助手-本地版-1.3.8-完整便携版"
DESK_ZIP = DESK / "论文研读助手-本地版-1.3.8-完整便携版.zip"
DESK_INSTALLER = DESK / "论文研读助手-1.3.8-安装程序.exe"
DESK_GUIDE = DESK / "论文研读助手-1.3.8-使用说明.md"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def copy_tree(source, target):
    """Copy the staged tree over the target *without* removing it first.

    The target may be the copy the reader is already using: it can be locked (their app is
    running from it) and it holds their library. So this never deletes the folder, never touches
    anything under `data/` or `run/`, and only overwrites the program files.
    """
    target.mkdir(parents=True, exist_ok=True)
    copied = skipped = 0
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(source)
        if relative.parts and relative.parts[0] in ("data", "run"):
            skipped += 1
            continue
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(path, destination)
            copied += 1
        except PermissionError:
            skipped += 1
    return copied, skipped


def compare(source, target):
    different, missing = [], []
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(source)
        counterpart = target / rel
        if not counterpart.is_file():
            missing.append(str(rel))
        elif digest(counterpart) != digest(path):
            different.append(str(rel))
    return different, missing


if __name__ == "__main__":
    if not STAGE.is_dir() or not ZIP.is_file():
        sys.exit("run build_package.py first")
    copied, skipped = copy_tree(STAGE, DESK_FOLDER)
    shutil.copy2(ZIP, DESK_ZIP)
    shutil.copy2(LAB / "使用说明.md", DESK_GUIDE)
    if INSTALLER.is_file():
        shutil.copy2(INSTALLER, DESK_INSTALLER)
    different, missing = compare(STAGE, DESK_FOLDER)
    print(f"folder copied: {copied} files (skipped {skipped}: data/run or locked) -> {DESK_FOLDER}")
    print(f"zip copied   : {DESK_ZIP} ({ZIP.stat().st_size} bytes)")
    print(f"guide copied : {DESK_GUIDE}")
    if INSTALLER.is_file():
        print(f"installer    : {DESK_INSTALLER} ({INSTALLER.stat().st_size} bytes)")
    print(f"folder matches the staged tree: different={different} missing={missing}")
    print("desktop zip sha256:", digest(DESK_ZIP))
    if INSTALLER.is_file():
        print("installer sha256  :", digest(INSTALLER))
