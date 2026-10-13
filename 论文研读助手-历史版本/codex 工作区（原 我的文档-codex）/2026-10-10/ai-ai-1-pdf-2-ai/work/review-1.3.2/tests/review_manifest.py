"""Write the review manifest: what changed, where it lives, and how to check it.

The manifest is written for a *different* reviewer AI that has no access to this conversation.
It therefore carries absolute paths, hashes, the baseline tree for diffing, the exact test
commands, and a one-line statement of intent per file - without claiming any of it is secure.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

PROJECT = Path(r"D:\harness home\paper-reader")
BASELINE = Path(r"D:\harness home\backups\sec-phase0-src-20261011-170425")
LEDGER = Path(r"D:\harness home\releases")
def manifest_path():
    return LEDGER / f"{VERSION}-待审清单.json"

#: (path, 阶段, 这个文件负责什么)
FILES = [
    ("credentials.py", "1", "凭据引用（env:NAME）、Secret 包装、旧明文只报告不读取、绑定与轮换"),
    ("secret_guard.py", "1", "固定错误码词表、内存秘密登记、redact/scrub、日志字段白名单"),
    ("endpoint_policy.py", "2", "URL 解析白名单、IDNA、IP 分类（回环/私网/云元数据/CGNAT）、DNS 固定、跳转与代理策略"),
    ("secure_transport.py", "2/6", "唯一出网点：http.client + 固定 socket、无自动跳转、无代理、TLS 全量校验、只匹配固定参数名"),
    ("session.py", "3", "内存会话、一次性 bootstrap、cookie 属性、CSRF、UID 派生"),
    ("launcher_pipe.py", "3", "命名管道（当前用户 ACL）、限长 JSON、禁用 pickle"),
    ("job_policy.py", "4", "任务快照冻结、批准、原子预算 ledger、暂停/停用开关"),
    ("backup_guard.py", "5", "ZIP 全量校验、恢复时重抽取、答案重新核验、旧格式降级、成员白名单"),
    ("extract_subprocess.py", "5", "受限子进程抽取（清洗环境 + 硬超时 + 限长输出）"),
    ("server.py", "3/4/5/6/7", "会话与路由认证、认证前限额、缓存规则、并发上限；恢复流程重写；目标批准；版本号 1.3.2"),
    ("providers.py", "1/2/5/6/7", "Secret 化凭据、payload 构造、工具调用拒绝、能力降级与**重发计费钩子**、无工具字段"),
    ("analysis.py", "4/5/7", "冻结快照驱动、发送闸门与预算、重试/退避/结果未知、**被取代任务不得写答案与状态**、健康检查开关"),
    ("reading_card.py", "5/6/7", "提示词 v2（不可信输入 + 能力边界）、JSON 加固、属性白名单、逐字引文、修复预算"),
    ("quotes.py", "5", "匹配分级 exact/approximate 与 is_verbatim"),
    ("schema.py", "4/6", "mode/max_output_tokens/limits、v1→v2 迁移（清明文键、关自动分析）、limits 校验"),
    ("static/app.js", "3/7", "fragment 换会话、CSRF、beacon、草稿命名空间、**引文核对状态与提醒渲染**"),
    ("static/index.html", "3/7", "无页面令牌、连接方式选择、暂停开关、预算显示位"),
    ("static/ai-reference.css", "7", "逐字/待人工核对标记与提醒的样式"),
    ("launch.py", "3", "经命名管道取 bootstrap，以 fragment 打开页面"),
    ("tests/build_release.py", "6/7", "打包清单改为目录发现；排除 tests/tools/docs/凭据类文件；发布说明含安全章节"),
    ("tests/security/test_phase7_review_fixes.py", "7",
     "独立审查两个必须修复项的回归：被取代代次不得覆盖、健康检查受开关约束、界面标记"),
    ("tests/package_selfcheck.py", "7", "对真实发布包的解压-启动-会话自检"),
    ("tests/review_manifest.py", "7", "生成本待审清单"),
    ("docs/AI安全升级-独立审查与整改.md", "7", "独立审查结论与逐条整改对账"),
]

#: 关键控制点 -> 文件 + 函数/区段，供审查者按点取证
CHECKPOINTS = [
    ("外部 HTTPS 可用 / 首次手动生成", ["providers.py:OpenAICompatibleAdapter.complete_json",
                                        "analysis.py:AnalysisManager.process",
                                        "analysis.py:AnalysisManager._blocked_before_send"]),
    ("无明文回退 / 无 Key 读接口 / 无前端持久化", ["credentials.py:normalise_ref",
                                                  "credentials.py:read_reference",
                                                  "credentials.py:legacy_plaintext_status",
                                                  "storage.py:read_credentials",
                                                  "providers.py:credential_report"]),
    ("Key 绑定目标身份 / 健康检查同限制", ["job_policy.py:JobLedger.approve_target",
                                          "job_policy.py:decide_send",
                                          "analysis.py:AnalysisManager.provider_ready",
                                          "analysis.py:AnalysisManager.target_approved",
                                          "server.py:handler_for"]),
    ("redirect / 代理 / DNS / TLS 连接级证据", ["secure_transport.py:SecureTransport._request",
                                               "secure_transport.py:SecureTransport._connect",
                                               "endpoint_policy.py:EndpointPolicy.authorize",
                                               "endpoint_policy.py:classify_ip"]),
    ("错误与输出不回显 Key", ["secret_guard.py:redact", "secret_guard.py:scrub",
                              "secret_guard.py:LOG_FIELDS", "analysis.py:_public_error",
                              "analysis.py:AnalysisManager._secret_echo"]),
    ("提示注入不能改权限", ["reading_card.py:SYSTEM_PROMPT",
                            "reading_card.py:USER_PROMPT_TEMPLATE",
                            "reading_card.py:CAPABILITIES",
                            "providers.py:_tool_call_denial"]),
    ("被取代代次不得覆盖 / 不重复付费", ["analysis.py:AnalysisManager._superseded",
                                        "analysis.py:AnalysisManager._generation_still_current",
                                        "analysis.py:AnalysisManager._accept_or_repair",
                                        "analysis.py:AnalysisManager.cancel",
                                        "job_policy.py:JobLedger.job_is_current"]),
    ("预算发送前预留 / 重发也计费", ["job_policy.py:JobLedger.reserve", "job_policy.py:JobLedger.settle",
                                    "analysis.py:AnalysisManager._dispatch",
                                    "providers.py:HttpAdapter._post_with_adaptation"]),
    ("恢复用 PDF 重验", ["backup_guard.py:inspect_archive", "backup_guard.py:verify_analysis",
                        "backup_guard.py:rebuild_cache", "server.py:Store.restore"]),
    ("no-store / 会话 / CSRF / 限流 / 草稿", ["server.py:cache_control_for", "server.py:limit_for",
                                             "session.py:SessionStore", "static/app.js:draftKey"]),
    ("用户说明区分三类概念", ["docs/AI安全升级-用户配置与操作说明.md"]),
    ("无虚假承诺", ["docs/AI安全升级-用户配置与操作说明.md",
                    "docs/AI安全升级-迁移与回滚.md",
                    "docs/AI安全升级-独立审查与整改.md"]),
]

COMMANDS = [
    r"cd 'D:\harness home\paper-reader'",
    r"$env:PAPER_READER_NO_DIALOG='1'",
    r"python -m unittest discover -s tests",
    r'python -m unittest discover -s tests/security -p "test_phase*.py"',
    r"python tests/security/phase1_channel_scan.py",
    r"python tests/security/phase2_e2e_https.py",
    r"python tests/release_smoke.py --with-data",
    # NOT build_release: it writes ZIPs and reads the live library
]


import re as _re


def _version():
    source = (PROJECT / "server.py").read_text(encoding="utf-8")
    return _re.search(r'APP_VERSION = "([^"]+)"', source).group(1)


VERSION = _version()


def digest(path, length=16):
    return hashlib.sha256(path.read_bytes()).hexdigest()[:length].upper()


def main():
    entries = []
    for name, phase, purpose in FILES:
        current = PROJECT / name
        original = BASELINE / name
        entries.append({
            "path": str(current),
            "phase": phase,
            "purpose": purpose,
            "exists": current.is_file(),
            "sha256_16": digest(current) if current.is_file() else "",
            "bytes": current.stat().st_size if current.is_file() else 0,
            "baseline_path": str(original),
            "baseline_exists": original.is_file(),
            "baseline_sha256_16": digest(original) if original.is_file() else "",
            "status": ("new" if not original.is_file() else
                       "changed" if digest(current) != digest(original) else "unchanged"),
        })
    release_dir = Path(r"D:\harness home\releases") / VERSION
    artefacts = {}
    for label, name in (("fresh_install_zip", f"论文研读助手-本地版-{VERSION}-全新安装.zip"),
                        ("upgrade_zip", f"论文研读助手-本地版-{VERSION}-升级包.zip")):
        item = release_dir / name
        artefacts[label] = str(item)
        artefacts[label + "_sha256"] = (hashlib.sha256(item.read_bytes()).hexdigest().upper()
                                       if item.is_file() else "MISSING")
    artefacts["release_dir"] = str(release_dir)
    artefacts["warning"] = "发布包含真实 library.json（用户论文），不要公开分发。"
    manifest = {
        "product": "论文研读助手-本地版",
        "version": VERSION,
        "previous_version": "1.3.1",
        "library_schema_version": 2,
        "purpose": ("待独立安全审查：本次改动加固 AI 出站、凭据、任务授权、内容核验与恢复。"
                    "本清单只描述位置与意图，不声称任何一条已经安全。"),
        "project_root": str(PROJECT),
        "baseline_root": str(BASELINE),
        "baseline_note": ("基线是改造前的同一份源码快照，用于逐文件 diff。"
                          "基线里不存在的新模块即本次新增。"),
        "spec_documents": [
            r"C:\Users\ASUS\Documents\Codex\2026-10-10\ai-ai-1-pdf-2-ai\outputs\论文研读助手1.3.1-AI安全架构与改造要求.md",
            r"C:\Users\ASUS\Documents\Codex\2026-10-10\ai-ai-1-pdf-2-ai\outputs\论文研读助手1.3.1-AI安全测试与发布清单.md",
            r"C:\Users\ASUS\Documents\Codex\2026-10-10\ai-ai-1-pdf-2-ai\outputs\论文研读助手1.3.1-AI安全分阶段执行提示词.md",
        ],
        "tests": {
            "product": r"python -m unittest discover -s tests",
            "security": r'python -m unittest discover -s tests/security -p "test_phase*.py"',
            "scanners": ["tests/security/phase1_channel_scan.py",
                         "tests/security/phase2_e2e_https.py"],
            "environment": {"PAPER_READER_NO_DIALOG": "1"},
            "do_not_run": ["tests/build_release.py（会写 ZIP 并读取用户的线上库）",
                           "tests/security/phase0_*.py（会创建快照目录）"],
        },
        "files": entries,
        "checkpoints": [{"question": question, "where": where}
                        for question, where in CHECKPOINTS],
        "artifacts": artefacts,
        "self_check": {
            "script": str(PROJECT / "tests" / "package_selfcheck.py"),
            "what_it_does": ("解压真实发布包到临时目录、启动、取页面、经命名管道领取一次性能力、"
                             "交换会话、读状态、切换暂停开关"),
            "command": "python tests/package_selfcheck.py",
            "not_covered": ["浏览器渲染", "真实服务商调用", "命名管道跨用户拒绝"],
        },
        "reviewer_commands": COMMANDS + [
            "python tests/package_selfcheck.py",
        ],
        "constraints_for_reviewer": [
            "只读：不要修改、新建、删除仓库内文件",
            "不要读取或输出真实密钥；仓库内 tests/secret.json 形似凭据，请只报告其存在，不要打开值",
            "不要连接真实 API；只用 127.0.0.1 回环桩",
            "不要运行 build_release.py",
            "结论必须给出文件:行或函数名 + 触发条件 + 复现步骤 + 实际影响",
            "计划、注释、文档与测试 mock 断言都不能替代真实控制路径证据",
        ],
    }
    out = manifest_path()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print("manifest:", out)
    print("files:", len(entries),
          "| new:", sum(1 for item in entries if item["status"] == "new"),
          "| changed:", sum(1 for item in entries if item["status"] == "changed"))
    for item in entries:
        print(f'  {item["status"]:9} {item["path"]}')


if __name__ == "__main__":
    main()
