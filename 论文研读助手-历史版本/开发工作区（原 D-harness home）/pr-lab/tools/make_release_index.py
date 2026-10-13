"""Generate the release-archive index (发布索引.md) with sizes and SHA256 for every version.

Usage:  runtime\\python.exe tools\\make_release_index.py [archive_dir]
"""
import hashlib
import sys
from pathlib import Path

ARCHIVE = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(r"C:\Users\ASUS\OneDrive\Desktop\论文研读助手\论文研读助手-历史版本\D盘发布归档（原 D-论文研读助手-发布）")

NOTES = {
    "1.3.3": "原始发行版（用户提供的完整便携版，本次工作的起点）",
    "1.3.4": "删除论文、元数据自动填充、多段续读、失败诊断码、输出上限 16384",
    "1.3.5": "期刊封面页标题误判修复；读图与图表分析上线（多模态）；界面字号统一与可拖拽分栏",
    "1.3.6": "记录栏掉到第二行的网格轨道缺陷修复；设置区数字框排版修复；默认分栏 58%；修掉分栏比例棘轮",
    "1.3.7": "AI 分析可靠性：只修问题字段、解析失败重发一次、最多两次定向修复；引文策略一致化；左侧控件台可收起",
    "1.3.8": "收起控件移到侧栏并加常驻把手；删除 AI 进度填充块；新增 EXE 安装程序（可选目录、卸载保留文献库）；版本归档与统一说明书",
}


def kind(name):
    if name.endswith(".zip"):
        return "便携 zip"
    if name.endswith(".exe"):
        return "安装程序"
    if name.endswith(".md"):
        return "文档"
    if name.endswith(".sha256"):
        return "校验值"
    if name.endswith((".json", ".txt")):
        return "验收记录"
    return "其它"


def main():
    if not ARCHIVE.is_dir():
        raise SystemExit(f"archive dir not found: {ARCHIVE}")
    versions = [name for name in sorted(NOTES) if (ARCHIVE / name).is_dir()]
    lines = [
        "# 论文研读助手 · 发布归档索引", "",
        "本目录收集全部历史版本与当前版本：每个版本一个子目录，含该版本的便携压缩包、使用说明，",
        "以及（1.3.8 起）安装程序与验收记录。表中哈希为 SHA256，可用下面命令核对：", "",
        "```powershell",
        'Get-FileHash -LiteralPath "<文件路径>" -Algorithm SHA256',
        "```", "", "## 版本一览", "",
        "| 版本 | 发布形态 | 说明 |", "|---|---|---|",
    ]
    for version in versions:
        kinds = sorted({kind(path.name) for path in (ARCHIVE / version).rglob("*") if path.is_file()})
        lines.append("| " + version + " | " + " + ".join(kinds) + " | " + NOTES[version] + " |")
    lines += ["", "## 文件清单与校验值", ""]
    for version in versions:
        files = [path for path in sorted((ARCHIVE / version).rglob("*")) if path.is_file()]
        if not files:
            continue
        lines += ["### " + version, "", "| 文件 | 大小 | SHA256 |", "|---|---:|---|"]
        for path in files:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            relative = path.relative_to(ARCHIVE / version).as_posix()
            size = f"{path.stat().st_size / 1024 / 1024:.2f} MB"
            lines.append("| `" + relative + "` | " + size + " | `" + digest + "` |")
        lines.append("")
    lines += [
        "## 建议使用哪个", "",
        "- **新装 / 换机**：用最新版本的安装程序 `论文研读助手-<版本>-安装程序.exe`——可选安装目录、自带卸载器，",
        "  卸载时默认保留文献库（`data\\` 文件夹），重装后继续可用。",
        "- **便携 / U 盘**：用最新版本的 `论文研读助手-本地版-<版本>-完整便携版.zip`，解压后双击 `启动论文研读助手.vbs`。",
        "- **历史版本**：仅用于复现旧行为或对照问题；同一台机器上多个副本可以并存（端口自动错开 8765、8766…）。", "",
        "## 这些产物是怎么来的", "",
        "- `tests/build_package.py` 产出便携 zip（自带空文献库、`SHA256.txt` 覆盖除自身外全部文件）；",
        "- `tools/build_installer.py` 产出安装程序（Inno Setup 6；图标由 `tools/make_icon.py` 生成）；",
        "- `tests/verify_package.py` 做「解压 → 包自带 Python 核对清单 → 真实启动 → 停止」验证；",
        "- `tests/verify_installer.py` 做「静默安装 → 启动并检查版本与端口 → 优雅停止 → 静默卸载 → 确认文献库保留」验证；",
        "- 统一说明书 `docs/使用说明.md`、`README.md`、`CHANGELOG.md` 与各版验收记录一并保存在归档中（1.3.8 起）。",
        "",
    ]
    target = ARCHIVE / "发布索引.md"
    target.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {target} ({target.stat().st_size} bytes, {len(versions)} versions)")


if __name__ == "__main__":
    main()
