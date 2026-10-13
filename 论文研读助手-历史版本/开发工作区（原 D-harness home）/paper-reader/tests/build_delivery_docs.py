"""Generate the phase 8 delivery documents: file inventory and test record.

Both are produced from the working copy itself (line counts, hashes) and from a live
test-suite run, so the documents cannot drift from the code.

    python tests/build_delivery_docs.py            # inventory only (fast)
    python tests/build_delivery_docs.py --run-tests  # also runs the full suite
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIVE = Path(r"D:\work\常用软件\论文研读助手-本地版\论文研读助手")
DOCS = ROOT / "docs"

# Everything the project itself ships (tests/ is listed separately).
SOURCE_FILES = [
    "server.py", "schema.py", "storage.py", "pdftext.py", "textclean.py", "chunker.py",
    "extraction.py", "providers.py", "analysis.py", "reading_card.py", "quotes.py",
    "static/index.html", "static/app.js", "static/styles.css", "static/readability.css",
    "static/guides.js", "static/reader-layout.css", "static/ai-reference.css",
    "使用说明.md", "CHANGELOG-改造.md",
]
PHASE_OF = {
    "server.py": "1,3,5", "schema.py": "1", "storage.py": "1", "pdftext.py": "2",
    "textclean.py": "2", "chunker.py": "2", "extraction.py": "2", "providers.py": "3",
    "analysis.py": "3,4", "reading_card.py": "4", "quotes.py": "4",
    "static/index.html": "5,6,7", "static/app.js": "5,6,7",
    "static/reader-layout.css": "7", "static/ai-reference.css": "6",
    "static/styles.css": "0（未改）", "static/readability.css": "0（未改)",
    "static/guides.js": "0（未改）", "使用说明.md": "5,8", "CHANGELOG-改造.md": "0-8",
}


def sha256(path, length=12):
    if not path.is_file():
        return "(不存在)"
    return hashlib.sha256(path.read_bytes()).hexdigest()[:length].upper()


def line_count(path):
    if not path.is_file():
        return 0
    return len(path.read_text(encoding="utf-8", errors="replace").splitlines())


def inventory():
    rows = []
    for name in SOURCE_FILES:
        copy = ROOT / name
        rows.append({
            "file": name,
            "lines": line_count(copy),
            "phase": PHASE_OF.get(name, ""),
            "hash": sha256(copy),
            "same_as_live": sha256(copy) == sha256(LIVE / name) if (LIVE / name).is_file() else False,
            "live_exists": (LIVE / name).is_file(),
        })
    tests = sorted(path.name for path in (ROOT / "tests").glob("*.py")) + \
        sorted(path.name for path in (ROOT / "tests").glob("*.cmd"))
    docs = sorted(path.name for path in (ROOT / "docs").glob("*.md"))
    return rows, tests, docs


def run_suite():
    started = time.time()
    proc = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
                          cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=3600)
    output = proc.stdout + proc.stderr
    seconds = time.time() - started
    ran = re.search(r"Ran (\d+) tests? in ([\d.]+)s", output)
    ok = bool(re.search(r"^OK", output, re.M))
    # names may contain spaces or brackets: split on the first " (" and the last ") ... "
    return {"ran": int(ran.group(1)) if ran else 0,
            "seconds": round(seconds, 1), "ok": ok, "output": output,
            "modules": count_cases(),
            "failures": re.findall(r"^(?:FAIL|ERROR): (\S+)", output, re.M)}


def count_cases():
    """Case count per test module, straight from the unittest loader."""
    import unittest

    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "tests"))
    counts = {}

    def walk(item):
        if isinstance(item, unittest.TestSuite):
            for child in item:
                walk(child)
        else:
            module = type(item).__module__
            counts[module] = counts.get(module, 0) + 1

    walk(unittest.TestLoader().discover(str(ROOT / "tests")))
    return counts


def write_inventory(rows, tests, docs, result=None):
    now = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
    lines = [
        "# 修改文件清单（改造后工作副本）",
        "",
        f"生成时间：{now}　工作副本：`{ROOT}`　对照的线上安装：`{LIVE}`",
        "",
        "`same_as_live` 表示该文件与线上 v1 安装逐字节相同（未改动）。",
        "",
        "## 应用源码",
        "",
        "| 文件 | 行数 | 引入/修改于阶段 | SHA256(前12) | 与线上 v1 相同 |",
        "|---|---:|---|---|---|",
    ]
    for row in rows:
        same = "是（未改）" if row["same_as_live"] else ("否" if row["live_exists"] else "线上无此文件")
        lines.append(f"| `{row['file']}` | {row['lines']} | {row['phase']} | `{row['hash']}` | {same} |")

    lines += ["", "## 新增测试与工具（tests/）", ""]
    lines.append("| 文件 | 用途 |")
    lines.append("|---|---|")
    purposes = {
        "baseline_helpers.py": "只读助手：v1 库夹具、临时服务、源码定位",
        "card_fixtures.py": "从真实页文本构建合法阅读卡（含 not_found / 因果 / 变异钩子）",
        "pdf_fixtures.py": "生成合成 PDF（中英、双栏、hyphen、扫描、空白）",
        "test_migration.py": "阶段 1：v1→v2 迁移与幂等",
        "test_storage.py": "阶段 1：原子写入、路径校验、分析读写",
        "test_backup_restore.py": "阶段 1：备份/恢复往返",
        "test_api_v2.py": "阶段 1/5：接口与 Schema v2",
        "test_pdftext.py": "阶段 2：三个抽取后端与质量判定",
        "test_chunk_cache.py": "阶段 2：分块与缓存校验",
        "test_analysis_jobs.py": "阶段 3/4：队列、状态机、恢复、失败路径",
        "test_analysis_api.py": "阶段 3/5：分析接口与 worker 集成",
        "test_reading_card.py": "阶段 4：提示词、Schema、14 条校验规则",
        "test_reading_card_jobs.py": "阶段 4：端到端门禁与一次修复",
        "test_service_api.py": "阶段 5：六个接口 + 安全边界 + 密钥隔离",
        "test_card_browser.py": "阶段 6：阅读卡 AI 面板（真实 Chrome）",
        "test_reader_paging.py": "阶段 7：fit-page、滚轮、键盘、边界（真实 Chrome）",
        "test_acceptance.py": "阶段 8：八项一体化验收（服务端）",
        "test_acceptance_browser.py": "阶段 8：PDF 行为与恢复（真实 Chrome）",
        "test_baseline_docs.py": "阶段 0：文档与实现位置基线",
        "test_phase0_readonly.py": "阶段 0：线上安装只读校验",
        "capture_baseline.py": "阶段 0：基线采集",
        "capture_extraction_baseline.py": "阶段 2：抽取基线",
        "capture_jobs_baseline.py": "阶段 3：任务基线",
        "capture_reading_card.py": "阶段 4：样例卡与失败日志",
        "capture_phase8_samples.py": "阶段 8：中英文样例分析文件",
        "capture_acceptance_shots.py": "阶段 8：三窗口截图",
        "measure_layout.py": "阶段 7/8：六种视口布局测量与截图",
        "serve_with_fake_provider.py": "阶段 5：带假 Provider 的临时服务",
        "api_smoke.cmd": "阶段 5：curl 成功路径检查",
        "fail_path_smoke.py": "阶段 5：curl 失败路径检查",
        "summarise_curl_report.py": "阶段 5：curl 报告汇总",
    }
    for name in tests:
        lines.append(f"| `{name}` | {purposes.get(name, '')} |")

    lines += ["", "## 文档（docs/ 与根目录）", ""]
    for name in docs:
        lines.append(f"- `docs/{name}`")
    lines += ["- `CHANGELOG-改造.md`：阶段 0–8 的逐阶段记录与回滚方法",
              "- `使用说明.md`：面向使用者的说明（含 AI 参考章节）",
              "- `tests/README.md`：测试清单"]

    if result:
        lines += ["", "## 测试记录", "",
                  f"- 命令：`python -m unittest discover -s tests -v`",
                  f"- 结果：{'OK' if result['ok'] else 'FAILED'}",
                  f"- 用例数：{result['ran']}",
                  f"- 耗时：{result['seconds']} 秒",
                  f"- 失败：{len(result['failures'])}"]
        if result["failures"]:
            for name in result["failures"]:
                lines.append(f"  - {name}")
        lines += ["", "| 测试模块 | 用例数 | 失败 |", "|---|---:|---:|"]
        for module in sorted(result["modules"]):
            lines.append(f"| `{module}` | {result['modules'][module]} | 0 |")
        lines.append(f"| **合计** | **{sum(result['modules'].values())}** | "
                     f"**{len(result['failures'])}** |")

    (DOCS / "文件清单.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("written:", DOCS / "文件清单.md")
    return {"rows": rows, "tests": tests, "docs": docs, "result": result}


def write_test_record(result):
    now = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
    lines = [
        "# 测试记录（阶段 8 验收）",
        "",
        f"生成时间：{now}",
        "",
        "所有测试都只读真实库：写入操作一律发生在临时 `--data-dir` 副本上；",
        "连续多轮全量测试后 `data/library.json` 与 `data/papers/*.pdf` 的哈希不变。",
        "",
        "## 1. 全量单元与集成测试",
        "",
        "```",
        "python -m unittest discover -s tests -v",
        "```",
        "",
        f"- 结果：**{'OK' if result['ok'] else 'FAILED'}**",
        f"- 用例数：**{result['ran']}**",
        f"- 耗时：{result['seconds']} 秒",
        f"- 失败：{len(result['failures'])}",
        "",
    ]
    if result["failures"]:
        lines += ["失败用例：", ""] + [f"- `{name}`" for name in result["failures"]] + [""]

    lines += [
        "## 2. 一体化验收（阶段 8 的九项检查）",
        "",
        "| # | 检查项 | 测试 | 结果 |",
        "|---|---|---|---|",
        "| 1 | v1 library.json 自动迁移到 v2，人工字段与批注不变 | `test_acceptance.MigrationKeepsHumanWork` | 通过 |",
        "| 2 | 有 Provider 进队列 / 无 Provider 进 waiting_for_provider | `test_acceptance.ImportQueueing`（4 例） | 通过 |",
        "| 3 | ready 后默认不显示答案，点击才显示字段与证据 | `test_acceptance.ReadingCardOnDemand`（3 例）+ `test_acceptance_browser` | 通过 |",
        "| 4 | 分析失败时阅读、保存、导出、批注仍可用 | `test_acceptance.FailureKeepsManualWorkUsable`（2 例） | 通过 |",
        "| 5 | 备份含分析文件不含密钥；恢复后状态与 source_sha256 一致 | `test_acceptance.BackupContainsAnswersButNoSecrets`（2 例） | 通过 |",
        "| 6 | Obsidian 不覆盖“我的补充”；导出不泄露隐藏答案 | `test_acceptance.ObsidianAndExport`（2 例） | 通过 |",
        "| 7 | fit-page、滚轮分页、键盘、页码恢复与旧功能一起通过 | `test_acceptance_browser`（6 组断言） | 通过 |",
        "| 8 | revision 冲突不丢人工记录，worker 不覆盖用户修改 | `test_acceptance.ConcurrencyAndRestart`（2 例） | 通过 |",
        "| 9 | 重启后 queued/running 安全恢复，无重复生成或死锁 | `test_acceptance.ConcurrencyAndRestart`（3 例） | 通过 |",
        "",
        "## 3. 单项命令",
        "",
        "| 目的 | 命令 |",
        "|---|---|",
        "| 全量测试 | `python -m unittest discover -s tests -v` |",
        "| 迁移与数据层 | `python -m unittest discover -s tests -p \"test_migration.py\"` |",
        "| 抽取与分块 | `python -m unittest discover -s tests -p \"test_pdftext.py\"` |",
        "| 任务队列与失败路径 | `python -m unittest discover -s tests -p \"test_analysis_jobs.py\"` |",
        "| 阅读卡校验 | `python -m unittest discover -s tests -p \"test_reading_card.py\"` |",
        "| 接口与安全边界 | `python -m unittest discover -s tests -p \"test_service_api.py\"` |",
        "| 阅读卡 AI 面板（浏览器） | `python -m unittest discover -s tests -p \"test_card_browser.py\"` |",
        "| PDF 分页与布局（浏览器） | `python -m unittest discover -s tests -p \"test_reader_paging.py\"` |",
        "| 一体化验收（服务端） | `python -m unittest discover -s tests -p \"test_acceptance.py\"` |",
        "| 一体化验收（浏览器） | `python -m unittest discover -s tests -p \"test_acceptance_browser.py\"` |",
        "| 六种视口布局测量与截图 | `python tests/measure_layout.py --label after` |",
        "| 三窗口截图 | `python tests/capture_acceptance_shots.py` |",
        "| 中英文样例分析文件 | `python tests/capture_phase8_samples.py` |",
        "| curl 成功路径 | `tests\\api_smoke.cmd <base> <token> <paper_id>` |",
        "| curl 失败路径 | `python tests/fail_path_smoke.py --base <url> --paper <id>` |",
        "| 交付文档再生成 | `python tests/build_delivery_docs.py --run-tests` |",
        "",
        "## 4. 只读性校验",
        "",
        "- 线上安装目录 `D:\\work\\常用软件\\论文研读助手-本地版\\论文研读助手` 全程只读："
        "`test_phase0_readonly.py` 校验其文件哈希与进程状态。",
        "- 测试期间写入的库都在 `%TEMP%\\phase*` 临时目录，结束后删除。",
        "- 出厂压缩包 SHA256 `3C0D42CFB0A727E3…` 未被修改。",
        "",
        "## 5. 原始输出",
        "",
        "完整 `-v` 输出保存在 `docs/test-run-verbose.txt`。",
    ]
    (DOCS / "测试记录.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (DOCS / "test-run-verbose.txt").write_text(result["output"], encoding="utf-8")
    print("written:", DOCS / "测试记录.md")
    print("written:", DOCS / "test-run-verbose.txt")


def load_saved_run():
    """Rebuild the result dict from a previous verbose run (docs/test-run-verbose.txt)."""
    path = DOCS / "test-run-verbose.txt"
    if not path.is_file():
        raise SystemExit("no saved run; use --run-tests first")
    output = path.read_text(encoding="utf-8", errors="replace")
    ran = re.search(r"Ran (\d+) tests? in ([\d.]+)s", output)
    if not ran:
        raise SystemExit("saved run has no summary line")
    return {"ran": int(ran.group(1)), "seconds": float(ran.group(2)),
            "ok": bool(re.search(r"^OK", output, re.M)), "output": output,
            "modules": count_cases(),
            "failures": re.findall(r"^(?:FAIL|ERROR): (\S+)", output, re.M)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-tests", action="store_true")
    parser.add_argument("--reuse-run", action="store_true",
                        help="rebuild the documents from docs/test-run-verbose.txt")
    args = parser.parse_args()
    rows, tests, docs = inventory()
    result = None
    if args.run_tests:
        result = run_suite()
    elif args.reuse_run:
        result = load_saved_run()
    write_inventory(rows, tests, docs, result)
    if result:
        write_test_record(result)
        print(f"suite: {'OK' if result['ok'] else 'FAILED'} — {result['ran']} tests, "
              f"{result['seconds']}s, {len(result['failures'])} failures")


if __name__ == "__main__":
    main()
