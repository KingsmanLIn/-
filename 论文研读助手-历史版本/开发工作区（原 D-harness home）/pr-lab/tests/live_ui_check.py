"""Check the live install's served UI: new controls, versioned assets, cache policy.

Read-only: fetches the page and its assets from the running instance and reports what is there.
"""
import json
import re
import sys
import urllib.request
from pathlib import Path

LAB = Path(r"C:\Users\ASUS\OneDrive\Desktop\论文研读助手\论文研读助手-历史版本\开发工作区（原 D-harness home）\pr-lab")
LIVE = Path(r"C:\Users\ASUS\OneDrive\Desktop\论文研读助手\1.3.8版本\论文研读助手-本地版-1.3.8-完整便携版")
sys.path.insert(0, str(LAB))

instance = json.loads((LIVE / "run" / "server.json").read_text(encoding="utf-8"))
base = f"http://127.0.0.1:{instance['port']}"


def fetch(path):
    request = urllib.request.Request(base + path)
    with urllib.request.urlopen(request, timeout=8) as response:
        return response.read().decode("utf-8", "replace"), dict(response.headers)


report = {"port": instance["port"]}
page, headers = fetch("/")
report["page_cache"] = headers.get("Cache-Control")
report["assets"] = sorted(set(re.findall(r'(?:src|href)="(/static/[^"]+)"', page)))
report["placeholders_left"] = [name for name in ("__VERSION__", "__BOOTSTRAP__") if name in page]
detail, script = fetch("/static/app.js")
report["script_cache"] = script.get("Cache-Control")
report["style_cache"] = fetch("/static/styles.css")[1].get("Cache-Control")

checks = {
    "分隔条元素": ("splitter", None),
    "读图开关": ("ai-vision", None),
    "图表页上限": ("ai-max-figures", None),
    "删除确认框": ("delete-dialog", None),
    "AI 进度条": ("ai-progress", None),
}
report["page_controls"] = {label: needle in page for label, (needle, _) in checks.items()}
report["progress_fill_removed"] = ("ai-progress-fill" not in page
                                   and "ai-progress-fill" not in detail)
report["script_features"] = {
    label: needle in detail for label, needle in (
        ("分隔条逻辑", "initSplitter"),
        ("分隔条记忆键", "paper-reader:pdf-split"),
        ("读图设置读写", "ai-vision"),
        ("图表页上限读写", "ai-max-figures"),
        ("行内删除", "data-delete-paper"),
        ("原文缺失判据", "source_missing"),
    )}
layout = fetch("/static/reader-layout.css")[0]
styles = fetch("/static/styles.css")[0]
report["css"] = {
    "分隔条网格列": "var(--pdf-split" in layout,
    "字号记号": "--fs-body" in styles,
    "字体栈记号": "--font-ui" in styles,
}
print(json.dumps(report, ensure_ascii=False, indent=2))
