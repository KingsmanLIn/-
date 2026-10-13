"""Fix the documentation after review: the screenshots ARE 1.3.8, plus the image name and the
single manual.

Three corrections, all found by looking at the images and the served page:
  * `docs/images/ai-progress.png` actually shows the notes-collapsed reader, so it is renamed;
  * the "known difference: the collapse button is still in the topbar" caveat is wrong - the
    captures were re-taken after the 1.3.8 change (topbar has no toggle, the rail sits at the
    left edge, the control is inside the sidebar brand row);
  * the root `使用说明.md` and `docs/使用说明.md` must hold the same manual, otherwise the
    packaged guide and the repository manual drift apart.

Usage:  runtime\\python.exe tools/fix_docs.py
"""
import hashlib
from pathlib import Path

LAB = Path(__file__).resolve().parent.parent
IMAGES = LAB / "docs" / "images"
OLD_IMAGE = IMAGES / "ai-progress.png"
NEW_IMAGE = IMAGES / "notes-collapsed.png"

SCREENSHOT_NOTE = {
    "README.md": ("> **截图说明**：四张图都取自 1.3.8 版本的界面——收起入口在**侧栏顶部品牌行**，"
                  "阅读区左缘有**常驻把手**（展开时贴在侧栏接缝，收起后停在窗口最左侧），"
                  "顶栏不再有收起按钮，与当前版本一致。"),
    "docs/使用说明.md": ("> **关于截图**：`docs/images/` 里的四张图都取自 1.3.8 版本的界面，"
                        "包括侧栏顶部的收起入口与阅读区左缘的常驻把手，与当前版本一致。"),
    "CHANGELOG.md": ("- 截图说明：`docs/images/` 里的图取自 1.3.8 版本的界面（收起入口在侧栏顶部、"
                     "阅读区左缘有常驻把手），与当前版本一致。"),
}
MARKERS = ("顶栏右侧", "已知差异", "1.3.7 在用实例", "1.3.7 截图")


def main():
    if OLD_IMAGE.is_file():
        OLD_IMAGE.replace(NEW_IMAGE)
        print(f"renamed: {OLD_IMAGE.name} -> {NEW_IMAGE.name}")
    for name, note in SCREENSHOT_NOTE.items():
        path = LAB / name
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        changed = 0
        for index, line in enumerate(lines):
            if "ai-progress.png" in line:
                lines[index] = line.replace("ai-progress.png", "notes-collapsed.png")
                changed += 1
            if any(marker in line for marker in MARKERS):
                lines[index] = note
                changed += 1
        if changed:
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"{name}: {changed} 处修正")

    manual = (LAB / "docs" / "使用说明.md").read_text(encoding="utf-8")
    root = LAB / "使用说明.md"
    root.write_text(manual, encoding="utf-8")
    same = hashlib.sha256(root.read_bytes()).hexdigest() == hashlib.sha256(
        (LAB / "docs" / "使用说明.md").read_bytes()).hexdigest()
    print(f"root manual now identical to docs/使用说明.md: {same} "
          f"({len(manual.splitlines())} 行)")


if __name__ == "__main__":
    main()
