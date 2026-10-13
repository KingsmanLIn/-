from pathlib import Path
import shutil
src = Path(r"D:\harness home\paper-reader\tests")
dst = Path(__file__).resolve().parent / "review-1.3.2" / "tests"
if dst.exists():
    shutil.rmtree(dst)
def ignore(directory, names):
    return [n for n in names if n in {"secret.json", "__pycache__", "build_release.py"} or n.startswith("phase0_") or n.startswith("phase0-")]
shutil.copytree(src, dst, ignore=ignore)
print("copied", sum(1 for _ in dst.rglob("*")))
