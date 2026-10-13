"""Offline paper reading workbench. Python 3.10+, standard library only."""
from __future__ import annotations

import argparse
import copy
import hashlib
import io
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse
import uuid
import webbrowser
import zipfile

import job_policy
import analysis
import backup_guard
import credentials
import endpoint_policy
import extraction
import extract_subprocess
import launcher_pipe
import metadata
import pdftext
import providers
import reading_card
import schema
import secret_guard
import session
import storage
from storage import DataStore

ROOT = Path(__file__).resolve().parent
APP_VERSION = "1.3.6"          # library schema version 2 (see schema.py)
BACKUP_MANIFEST = "manifest.json"
FIELDS = {
    "question": "研究问题", "motivation": "问题的重要性与文献缺口",
    "claim": "主要回答与贡献", "data": "数据与样本",
    "variables": "关键概念与变量", "method": "研究方法与比较逻辑",
    "evidence": "核心结果及原文位置", "robustness": "稳健性与替代解释",
    "boundaries": "结论的适用边界", "assessment": "我的判断与疑问",
    "summary": "合上论文后的复述", "connections": "与其他文献的联系",
    "next": "下一步阅读或研究行动",
}
TYPES = {"framework": "框架 / 综述", "empirical": "实证：机制与因果评估", "descriptive": "实证：描述与趋势检验", "theory": "理论模型"}
ANNOTATIONS = {"argument": "论点", "evidence": "证据", "question": "疑问", "term": "术语"}
START = "<!-- paper-reader:generated:start -->"
END = "<!-- paper-reader:generated:end -->"
ID_RE = re.compile(r"^[a-z0-9-]{8,64}$")
MAX_PDF = 80 * 1024 * 1024
# Paper record keys a browser session is allowed to change. `analysis` is deliberately
# absent: AI metadata is written by the service, never by /api/save.
EDITABLE_KEYS = ("title", "authors", "year", "venue", "doi", "tags", "citation", "type", "language",
                 "fields", "steps", "annotations", "pageOffset", "pageLabels", "lastPage", "archived")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def clean_name(text, limit=72):
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f\[\]#]', "_", str(text)).strip(" ._")
    return (text[:limit].rstrip(" .") or "未命名论文")


def atomic_write(path, content):
    """Kept for callers inside this module; delegates to the storage layer."""
    if isinstance(content, bytes):
        storage.atomic_write_bytes(path, content)
    else:
        storage.atomic_write_text(path, content)


def validate_paper(paper):
    if not isinstance(paper, dict) or not ID_RE.fullmatch(str(paper.get("id", ""))):
        raise ValueError("论文记录的编号无效")
    if paper.get("type") not in TYPES or paper.get("language") not in ("zh", "en"):
        raise ValueError("论文类型或语言无效")
    if not isinstance(paper.get("fields", {}), dict) or not isinstance(paper.get("annotations", []), list):
        raise ValueError("阅读记录格式无效")
    if len(paper.get("annotations", [])) > 10000:
        raise ValueError("单篇批注数量超过限制")
    for item in paper.get("annotations", []):
        if not isinstance(item, dict) or item.get("type") not in ANNOTATIONS:
            raise ValueError("批注类型无效")
        if not isinstance(item.get("page"), int) or not 1 <= item["page"] <= 100000:
            raise ValueError("批注页码无效")
        for rect in item.get("rects", []):
            if len(rect) != 4 or any(not isinstance(v, (float, int)) or not 0 <= v <= 1.01 for v in rect):
                raise ValueError("高亮位置无效")
    for name in ("title", "authors", "year", "venue", "doi", "tags", "citation"):
        if not isinstance(paper.get(name, ""), str):
            raise ValueError("文献信息格式无效")
    if len(paper.get("title", "")) > 1000:
        raise ValueError("论文标题过长")
    for k, v in paper.get("fields", {}).items():
        if k not in FIELDS or not isinstance(v, str) or len(v) > 200000:
            raise ValueError("阅读卡字段无效")
    if not isinstance(paper.get("steps", [False] * 4), list) or len(paper.get("steps", [])) != 4:
        raise ValueError("阅读步骤格式无效")
    if not isinstance(paper.get("pageOffset", 0), (float, int)):
        raise ValueError("页码偏移必须是数字")
    if "analysis" in paper:
        # Version 2 metadata. Normalised here so an old or hand-edited value cannot
        # travel further; AI answers themselves live in data/analyses/<id>.json.
        paper["analysis"] = schema.validate_analysis(paper["analysis"])


def page_label(paper, page):
    labels = paper.get("pageLabels", [])
    if page <= len(labels) and labels[page - 1]:
        return str(labels[page - 1])
    n = page + int(paper.get("pageOffset", 0))
    return str(n) if n > 0 else "封面"


def generated_body(paper, pdf_link):
    title = paper["title"].replace("\n", " ")
    complete = sum(bool(x) for x in paper.get("steps", []))
    lines = [f"# {title}", "", f"- 作者：{paper.get('authors') or '待核对'}",
             f"- 年份：{paper.get('year') or '待核对'}", f"- 来源：{paper.get('venue') or '待核对'}",
             f"- 类型：{TYPES[paper['type']]}", f"- 语言：{'中文' if paper['language'] == 'zh' else '英文'}",
             f"- 主题：{paper.get('tags') or '未分类'}", f"- 阅读进度：{complete}/4 步",
             f"- 最近保存：{paper.get('updated', '')}", f"- 原文：{pdf_link}"]
    if paper.get("doi"):
        lines.append(f"- DOI：{paper['doi']}")
    if paper.get("citation"):
        lines.extend(["", "## 引用条目", "", paper["citation"]])
    lines.extend(["", "## 阅读步骤", ""])
    for n, label in enumerate(["定位问题", "拆解证据", "形成判断", "复述与应用"]):
        lines.append(f"- [{'x' if paper['steps'][n] else ' '}] {n + 1}. {label}")
    lines.extend(["", "## 阅读卡"])
    for key, label in FIELDS.items():
        if paper["type"] == "theory":
            label = {"data": "分析对象与模型环境", "variables": "符号、参数与关键假设", "method": "推导逻辑与经济直觉", "evidence": "核心命题与可检验预测"}.get(key, label)
        lines.extend(["", f"### {label}", "", paper.get("fields", {}).get(key, "").strip() or "待记录。"])
    lines.extend(["", "## 批注与术语"])
    if not paper.get("annotations"):
        lines.extend(["", "暂无批注。"])
    for item in sorted(paper.get("annotations", []), key=lambda a: (a["page"], a.get("created", ""))):
        label = page_label(paper, item["page"])
        lines.extend(["", f"### {ANNOTATIONS[item['type']]} · PDF {item['page']} / 文内 {label}", ""])
        if item.get("quote"):
            lines.extend("> " + line for line in item["quote"].splitlines())
            lines.append("")
        lines.append(item.get("note", "").strip() or "待补充理解。")
        if pdf_link.startswith("[["):
            target = pdf_link[2:].split("|")[0].removesuffix("]]" )
            lines.extend(["", f"[[{target}#page={item['page']}|回到原文第 {item['page']} 页]]"])
    lines.extend(["", "## 待办", "", f"- [ ] {paper.get('fields', {}).get('next', '').strip() or '补齐阅读卡并回看不理解的位置。'}"])
    return "\n".join(lines) + "\n"


def markdown(paper, pdf_link):
    front = f"---\npaper_id: {json.dumps(paper['id'])}\ntags: [论文研读]\n---\n\n"
    return front + START + "\n" + generated_body(paper, pdf_link) + END + "\n\n## 我的补充\n\n在这里补充你的自由笔记。本节及自动区域以外的内容会在同步时保留。\n"


#: Bibliographic fields a parsed front matter can fill in. `title` is handled separately: the
#: import path owns the title it derived from the file name, so only a caller that knows the
#: paper is still untouched may replace it.
METADATA_FIELDS = ("authors", "year", "venue", "doi", "citation")


def merge_metadata(paper, parsed, allow_title=False):
    """Fill still-empty bibliographic fields of a paper record. Returns the changed keys.

    Only empty fields are written, so a value the reader typed is never replaced by a
    heuristic, and a second parse cannot rewrite what the first one already filled. With
    `allow_title` (the import path) a parsed title replaces the one taken from the file name.
    """
    parsed = parsed or {}
    changed = []
    for key in METADATA_FIELDS:
        value = str(parsed.get(key, "") or "").strip()
        if value and not str(paper.get(key, "") or "").strip():
            paper[key] = value
            changed.append(key)
    title = str(parsed.get("title", "") or "").strip()
    if allow_title and title and title != str(paper.get("title", "") or "").strip():
        paper["title"] = title
        changed.append("title")
    return changed


class Store:
    def __init__(self, directory, extractor_factory=None, prompt_version=None):
        self.directory = Path(directory).resolve()
        self.files = DataStore(self.directory).ensure_directories()
        self.path = self.files.library
        self.pdf_dir = self.files.papers
        self.lock = threading.RLock()
        self.migrated = False
        # Used by the verified restore: a backup's own cache is never trusted, so the PDF is
        # extracted again here. A test can inject its own extractor.
        # None means "use the time-bounded child process" (see Store.restore); a test may
        # inject an in-process extractor instead.
        self.extractor_factory = extractor_factory
        self.prompt_version = prompt_version or reading_card.PROMPT_VERSION
        # Holds the exact bytes of the library as it was found, so library.previous.json
        # always keeps a pre-migration copy even if a later start-up write happens first.
        self.migration_source = None
        if self.path.exists():
            try:
                original = self.path.read_bytes()
                self.state = json.loads(original.decode("utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("数据库文件无法读取，请先用备份恢复：" + str(exc)) from exc
            # version 1 -> 2 (and any later step) runs here, before anything is served.
            self.migration_source = original
            self.state, self.migrated = schema.migrate_state(self.state)
            if self.migrated:
                self.persist(keep_previous=False)
        else:
            self.state = {"version": schema.SCHEMA_VERSION,
                          "settings": {"vault_path": "", "sync_enabled": False, "last_sync": "", "sync_error": "",
                                       "ai": schema.default_ai_settings()},
                          "papers": []}
            self.persist()
        self.state["settings"]["ai"] = schema.validate_ai_settings(self.state["settings"].get("ai", {}))
        for p in self.state["papers"]:
            validate_paper(p)
            p.setdefault("analysis", schema.default_analysis())
        self.mark_missing_sources()
        if self.migration_source is not None:
            # A start-up write may already have moved the library forward; make sure the
            # pre-migration file is still the one a user can roll back to.
            storage.atomic_write_bytes(self.files.previous, self.migration_source)
            self.migration_source = None

    def persist(self, keep_previous=True):
        """Write the library. `library.previous.json` holds one earlier state."""
        if keep_previous:
            if self.migration_source is not None:
                storage.atomic_write_bytes(self.files.previous, self.migration_source)
                self.migration_source = None
            else:
                self.files.backup_library()
        self.files.write_library(self.state)

    # --- version 2 helpers ------------------------------------------------
    def mark_missing_sources(self):
        """Fill in source_sha256 for papers that already have a PDF on disk.

        One-time per paper: once a hash is recorded the method is a no-op, so it can
        run at every start-up without producing new writes.
        """
        changed = False
        for p in self.state["papers"]:
            meta = p.setdefault("analysis", schema.default_analysis())
            if not meta.get("source_sha256") and self.files.has_pdf(p["id"]):
                meta["source_sha256"] = hashlib.sha256(self.files.pdf_path(p["id"]).read_bytes()).hexdigest()
                changed = True
        if changed:
            self.persist()
        return changed

    def ai_settings(self):
        with self.lock:
            return copy.deepcopy(self.state["settings"]["ai"])

    def endpoint_identity(self, ai_settings=None):
        """The approved target a credential binding belongs to. '' when nothing is set."""
        settings = ai_settings if ai_settings is not None else self.state["settings"]["ai"]
        mode = (settings or {}).get("mode", "") or endpoint_policy.DEFAULT_MODE
        base_url = (settings or {}).get("base_url", "") or (
            providers.DEFAULT_OLLAMA_BASE if settings.get('provider') == 'ollama'
            else providers.DEFAULT_OPENAI_BASE)
        try:
            target = endpoint_policy.EndpointPolicy.for_settings(base_url, mode).target
        except endpoint_policy.PolicyError:
            # an unusable address has no identity: nothing can be bound to it
            return ""
        return f"{mode}|{target.identity}"

    # --- outbound approvals (kept out of the library on purpose) -----------
    def outbound_state(self):
        """Approved endpoint identities and the credential reference used for each.

        Stored in `data/outbound.json`, not in the library: the library shape is a published
        contract, and this is application state. It holds no credential value - only the
        endpoint identity, the reference name and a timestamp.
        """
        path = Path(self.directory) / "outbound.json"
        payload, error = storage.read_json(path, limit=64 * 1024)
        if error is not None or not isinstance(payload, dict):
            return {"schema": 1, "approvals": {}}
        payload.setdefault("schema", 1)
        if not isinstance(payload.get("approvals"), dict):
            payload["approvals"] = {}
        return payload

    def _write_outbound(self, payload):
        path = Path(self.directory) / "outbound.json"
        payload["updated_at"] = schema._utc_now()
        storage.atomic_write_json(path, payload)

    def approve_target(self, ai_settings=None):
        """Record that this endpoint identity may receive the bound credential."""
        settings = ai_settings if ai_settings is not None else self.state["settings"]["ai"]
        identity = self.endpoint_identity(settings)
        if not identity:
            return {}
        payload = self.outbound_state()
        payload["approvals"][identity] = {
            "credential_ref": credentials.normalise_ref((settings or {}).get("api_key_env", "")),
            "model": (settings or {}).get("model", ""),
            "approved_at": schema._utc_now(),
        }
        self._write_outbound(payload)
        return payload["approvals"][identity]

    def target_approved(self, ai_settings=None):
        settings = ai_settings if ai_settings is not None else self.state["settings"]["ai"]
        identity = self.endpoint_identity(settings)
        if not identity:
            return False
        record = self.outbound_state()["approvals"].get(identity)
        if not record:
            return False
        return record.get("credential_ref") == credentials.normalise_ref(
            (settings or {}).get("api_key_env", ""))

    def update_ai_settings(self, patch):
        """Merge a partial settings.ai patch. Never stores a key, only the env name.

        When the target changes - mode, origin, port or base path - queued jobs are parked
        and the new target is *not* approved, so the previous credential is never offered to
        a different server. Approving the new target is a separate user action.
        """
        with self.lock:
            before_ai = copy.deepcopy(self.state["settings"]["ai"])
            before_identity = self.endpoint_identity(before_ai)
            merged = copy.deepcopy(before_ai)
            for key, value in (patch or {}).items():
                if key not in schema.AI_SETTINGS_DEFAULTS:
                    raise ValueError("AI 设置含有未知字段：" + str(key))
                merged[key] = value
            merged = schema.validate_ai_settings(merged)
            reference = credentials.normalise_ref(merged.get('api_key_env', ''))
            if reference and reference.startswith('env:') and not credentials.is_registered(
                    reference[4:]):
                raise ValueError('Credential environment reference is not registered')
            sensitive = ("provider", "base_url", "mode", "model", "api_key_env",
                         "max_output_tokens")
            changed = any(before_ai.get(k) != merged.get(k) for k in sensitive)
            merged["config_revision"] = before_ai.get("config_revision", 1) + int(changed)
            merged["credential_revision"] = before_ai.get("credential_revision", 1) + int(
                before_ai.get("api_key_env") != merged.get("api_key_env"))
            self.state["settings"]["ai"] = merged
            if changed:
                manager = getattr(self, "_analysis_manager", None)
                ledger = manager.ledger if manager is not None else job_policy.JobLedger(self.directory)
                ledger.revoke_all("configuration_changed")
                ledger.revoke_targets()
                for paper_id in list(ledger.state["jobs"]):
                    ledger.clear_job(paper_id)
                self._write_outbound({"schema": 1, "approvals": {}})
                self.state["settings"]["last_rebind_parked"] = self._park_for_rebinding()
                if manager is not None:
                    for paper in self.state.get("papers", []):
                        manager._cancel_running(paper["id"])
            self.persist()
            return copy.deepcopy(merged)

    def _park_for_rebinding(self):
        """Move queued work back to waiting_for_user: no request without a fresh approval.

        Called with `self.lock` held, so it mutates the papers in place rather than going
        through the locking helper (which would deadlock on the same lock).
        """
        parked = []
        for paper in self.state.get("papers", []):
            meta = paper.get("analysis") or {}
            if meta.get("status") not in ("queued", "running"):
                continue
            meta.update({"status": "waiting_for_user", "stage": "", "progress": 0,
                         "message": "接口目标已变更，需要重新确认后再生成",
                         "error_code": "", "error_message": "",
                         "updated_at": schema._utc_now()})
            paper["analysis"] = schema.validate_analysis(meta)
            parked.append(paper["id"])
        return parked

    def analysis_meta(self, paper_id):
        return copy.deepcopy(self.paper(paper_id).get("analysis", schema.default_analysis()))

    def analysis_answer(self, paper_id):
        """Read the stored answer file. Returns (payload, error)."""
        self.paper(paper_id)
        return self.files.read_analysis(paper_id)

    def write_analysis_answer(self, paper_id, payload):
        """Write data/analyses/<id>.json atomically and point the metadata at it."""
        with self.lock:
            p = self.paper(paper_id)
            name = self.files.write_analysis(paper_id, payload)
            meta = p.setdefault("analysis", schema.default_analysis())
            meta["answer_file"] = name
            meta["status"] = "ready"
            meta["completed_at"] = now()
            meta["error_code"] = ""
            meta["error_message"] = ""
            validate_paper(p)
            self.persist()
            return {"paper": copy.deepcopy(p), "answer_file": name}

    def write_failed_answer(self, paper_id, payload):
        """Log an unvalidated model answer next to the paper; never an answer file."""
        with self.lock:
            self.paper(paper_id)
            return self.files.write_failed_answer(paper_id, payload)

    def drop_analysis(self, paper_id):
        """Remove the answer file and reset the metadata; reader data is untouched."""
        with self.lock:
            p = self.paper(paper_id)
            removed = self.files.delete_analysis(paper_id)
            p["analysis"] = schema.default_analysis()
            validate_paper(p)
            self.persist()
            return removed

    def delete_paper(self, paper_id):
        """Delete one paper: its files, its library record and its job ledger rows.

        The **Obsidian note in the vault is never touched**: nothing outside `data/` is
        removed, so a synced note stays where the user put it and is theirs to keep.

        `library.previous.json` is refreshed first, so the state before the deletion - with
        the paper still in it - is the one a user can roll back to. Returns the deleted
        paper's title and what was removed from disk; raises ValueError for an id that is
        empty, malformed or not in the library.
        """
        with self.lock:
            paper = self.paper(paper_id)                     # validates the id and the range
            title = str(paper.get("title", ""))
            # Keep the state *before* the deletion as the rollback copy: it still lists this
            # paper, which is the whole point of a previous library after a delete.
            if self.migration_source is not None:
                storage.atomic_write_bytes(self.files.previous, self.migration_source)
                self.migration_source = None
            else:
                self.files.backup_library()
            manager = getattr(self, "_analysis_manager", None)
            if manager is not None:
                # Same pattern as /api/analysis-cancel: ask every in-flight call for this
                # paper to stop. Done before the files go, so a running job does not race
                # a deleted PDF.
                try:
                    manager.cancel(paper_id)
                except (ValueError, KeyError):
                    # The paper is already leaving the library; a cancel that cannot find
                    # it must not stop the deletion.
                    pass
            # The ledger keeps approvals and frozen jobs next to the library. Both rows for
            # this paper go, so a later re-import of the same source cannot inherit an old
            # go-ahead to send.
            ledger = getattr(self, "_job_ledger", None)
            if ledger is None and manager is not None:
                ledger = getattr(manager, "ledger", None)
            if ledger is not None:
                # A deleted paper must not leave an approval behind: re-importing the same id
                # would otherwise arrive pre-approved. `revoke_approval` only flags a row, so
                # the ledger exposes `clear_approval` for exactly this case.
                ledger.clear_approval(paper_id)
                ledger.clear_job(paper_id)
            removed = self.files.delete_paper_files(paper_id)
            state = self.state["papers"]
            for index, candidate in enumerate(state):
                if candidate.get("id") == paper_id:
                    del state[index]
                    break
            # Atomic write, exactly like every other mutation of the library: write_library
            # is the one place that replaces the file.
            self.files.write_library(self.state)
            return {"paper_id": paper_id, "title": title, "deleted": removed}

    # --- analysis metadata (written by the AnalysisManager only) ----------
    def set_analysis_status(self, paper_id, status, **changes):
        """Single writer helper for the worker: validate, then persist atomically."""
        with self.lock:
            paper = self.paper(paper_id)
            meta = paper.setdefault("analysis", schema.default_analysis())
            if changes.get("progress") is None and changes.get("stage") in analysis.STAGE_PROGRESS:
                changes["progress"] = analysis.STAGE_PROGRESS[changes["stage"]]
            meta.update(changes)
            meta["status"] = status
            meta["updated_at"] = schema._utc_now()
            paper["analysis"] = schema.validate_analysis(meta)
            self.persist()
            return copy.deepcopy(paper["analysis"])

    def paper(self, paper_id):
        if not ID_RE.fullmatch(str(paper_id)):
            raise ValueError("无效的论文编号")
        for p in self.state["papers"]:
            if p["id"] == paper_id:
                return p
        raise ValueError("找不到这篇论文")

    def snapshot(self):
        """The library as the page sees it.

        `source_missing` is computed here and never stored: a paper whose PDF was deleted by
        hand (from `data/papers/` in Explorer, say) still needs to be listed - otherwise its
        record can never be cleaned up - and the page has no other way to tell that the file is
        gone. `has_pdf` validates the id itself and answers False for a malformed one.
        """
        with self.lock:
            state = copy.deepcopy(self.state)
        for paper in state.get("papers", []):
            paper["source_missing"] = not self.files.has_pdf(paper.get("id", ""))
        return state

    def save_paper(self, body):
        with self.lock:
            p = self.paper(body["id"])
            if body.get("revision") != p.get("revision", 0):
                raise Conflict("这篇论文在另一个窗口有更新，请刷新后合并记录。当前草稿仍保存在本浏览器。")
            candidate = copy.deepcopy(p)
            for key, value in body.get("patch", {}).items():
                if key in EDITABLE_KEYS:
                    candidate[key] = value
            validate_paper(candidate)
            candidate["revision"] = p.get("revision", 0) + 1
            candidate["updated"] = now()
            self.state["papers"][self.state["papers"].index(p)] = candidate
            self.sync_paper(candidate)
            self.persist()
            return {"paper": copy.deepcopy(candidate), "settings": copy.deepcopy(self.state["settings"])}

    def apply_metadata(self, paper_id, parsed, allow_title=False):
        """Fill still-empty metadata fields on an existing paper. Returns what changed."""
        with self.lock:
            paper = self.paper(paper_id)
            changed = merge_metadata(paper, parsed, allow_title=allow_title)
            if changed:
                paper["updated"] = now()
                self.persist()
            return changed

    def _parse_front_matter(self, paper_id, pdf_path, filename=""):
        """(fields, error) read from the PDF's first pages by the bounded child process.

        The extraction cache is kept, so an analysis of the same paper later reuses it instead
        of extracting the document twice.
        """
        factory = self.extractor_factory
        extractor = factory() if factory else extraction.IsolatedPdfTextExtractor(files=self.files)
        result = extractor.extract(pdf_path, paper_id=paper_id)
        if not getattr(result, "ok", False):
            return {}, f"{getattr(result, 'code', '')}:{getattr(result, 'message', '')}"
        return metadata.parse_metadata(getattr(result, "pages", []) or [],
                                       filename=filename), ""

    def import_pdf(self, data, filename):
        if b"%PDF-" not in data[:1024]:
            raise ValueError("这个文件不是可识别的 PDF")
        if len(data) > MAX_PDF:
            raise ValueError("PDF 超过 80 MB，请先压缩后导入")
        paper_id = str(uuid.uuid4())
        title = Path(filename.replace("\\", "/")).stem or "未命名论文"
        language = "zh" if re.search(r"[\u4e00-\u9fff]", title) else "en"
        ai = self.state["settings"]["ai"]
        provider_configured = bool(ai.get("provider")) and ai.get("provider") != "none"
        auto = bool(ai.get("auto_analyze"))
        # A new import records its job state and returns immediately. Without a provider the
        # paper waits for one; with a provider it waits for the user unless they explicitly
        # asked for automatic analysis - the AI reference is only ever fetched on request.
        if not provider_configured:
            initial_status = "waiting_for_provider"
            initial_message = "尚未配置本地模型；配置后会在你点击“查看 AI 参考”时开始分析"
        elif auto:
            initial_status = "queued"
            initial_message = "已加入分析队列"
        else:
            initial_status = "waiting_for_user"
            initial_message = "点击“查看 AI 参考”后开始分析"
        analysis_meta = schema.analysis_metadata(
            hashlib.sha256(data).hexdigest(), status=initial_status,
            language=language, provider=ai.get("provider", "none") or "none",
            model=ai.get("model", ""))
        analysis_meta.update(stage="", message=initial_message)
        paper = {"id": paper_id, "title": title, "authors": "", "year": "", "venue": "", "doi": "", "citation": "", "tags": "",
                 "language": language, "type": "empirical", "fields": {k: "" for k in FIELDS},
                 "steps": [False] * 4, "annotations": [], "pageOffset": 0, "pageLabels": [], "lastPage": 1, "revision": 0,
                 "created": now(), "updated": now(), "archived": False, "note_name": "",
                 "analysis": schema.validate_analysis(analysis_meta)}
        # The PDF is written first: the metadata pass reads it from disk, and a paper whose
        # front matter cannot be parsed must still be importable. The extraction is a bounded
        # child process (well under two seconds for a 58-page paper) and its cache is kept for
        # the analysis that may follow, so the file is not read twice.
        with self.lock:
            storage.atomic_write_bytes(self.files.pdf_path(paper_id), data)
        parsed, parse_error = {}, ""
        try:
            parsed, parse_error = self._parse_front_matter(paper_id, self.files.pdf_path(paper_id),
                                                           filename)
        except Exception as exc:                                  # noqa: BLE001 - import wins
            parse_error = type(exc).__name__
        changed = merge_metadata(paper, parsed, allow_title=True)
        if changed:
            # The language decides which input budget the paper gets, so it follows the parsed
            # title rather than the file name.
            language = "zh" if re.search(r"[\u4e00-\u9fff]", paper["title"]) else "en"
            paper["language"] = language
            paper["analysis"]["language"] = language
        paper["note_name"] = clean_name(paper["title"]) + "--" + paper_id[:8] + ".md"
        with self.lock:
            self.state["papers"].append(paper)
            self.sync_paper(paper)
            self.persist()
        if parse_error:
            log_event(secret_guard.log_record(event="metadata_skipped", paper_id=paper_id,
                                              reason=str(parse_error)[:60]))
        elif changed:
            log_event(secret_guard.log_record(event="metadata_filled", paper_id=paper_id,
                                              count=len(changed)))
        return copy.deepcopy(paper)

    def sync_paper(self, paper, with_index=True):
        s = self.state["settings"]
        if not s.get("sync_enabled"):
            return
        try:
            vault = Path(s["vault_path"]).expanduser().resolve()
            if not vault.is_dir():
                raise ValueError("笔记库文件夹不存在，请重新选择")
            folder = vault / "论文研读助手"
            attach = folder / "附件"
            attach.mkdir(parents=True, exist_ok=True)
            source = self.pdf_dir / f"{paper['id']}.pdf"
            target = attach / source.name
            if not target.exists() or target.stat().st_size != source.stat().st_size:
                atomic_write(target, source.read_bytes())
            # Filenames are derived only from locally created titles and stable ids.
            name = Path(paper.get("note_name", "")).name
            if not name or name != paper.get("note_name") or not name.endswith(".md"):
                name = clean_name(paper["title"]) + "--" + paper["id"][:8] + ".md"
                paper["note_name"] = name
            note = folder / name
            link = f"[[附件/{paper['id']}.pdf|论文原文]]"
            fresh = markdown(paper, link)
            if note.exists():
                old = note.read_text(encoding="utf-8")
                if old.count(START) != 1 or old.count(END) != 1 or old.index(START) >= old.index(END):
                    raise ValueError(f"{name} 的自动区域标记被修改，已保留原笔记。请恢复标记后重试")
                a = old.index(START) + len(START)
                b = old.index(END)
                fresh = old[:a] + "\n" + generated_body(paper, link) + old[b:]
                if fresh != old:
                    backup = folder / ".同步备份" / (name + ".previous")
                    atomic_write(backup, old)
            atomic_write(note, fresh)
            if with_index:
                self.sync_index(folder)
            s["last_sync"] = now()
            s["sync_error"] = ""
        except (OSError, ValueError) as exc:
            s["sync_error"] = str(exc)

    def sync_index(self, folder):
        def cell(value):
            return str(value or "待记录").replace("|", "\\|").replace("\n", "<br>")
        lines = ["# 文献索引与比较", "", "从共同研究问题出发，连接不同文献的机制、证据与边界。", "",
                 "| 文献 | 进度 | 研究问题 | 数据与方法 | 核心结果 | 结论边界 |", "| --- | --- | --- | --- | --- | --- |"]
        for p in self.state["papers"]:
            if p.get("archived"):
                continue
            note = Path(p["note_name"]).stem
            f = p["fields"]
            row = [f"[[{note}\\|{p['title'].replace('|', ' / ').replace(']', '')}]]", f"{sum(bool(s) for s in p['steps'])}/4",
                   cell(f.get("question")), cell("\n".join(filter(None, [f.get("data"), f.get("method")]))),
                   cell(f.get("evidence") or f.get("claim")), cell(f.get("boundaries"))]
            lines.append("| " + " | ".join(row) + " |")
        body = "\n".join(lines) + "\n"
        path = folder / "00 文献索引.md"
        if path.exists():
            old = path.read_text(encoding="utf-8")
            if old.count(START) != 1 or old.count(END) != 1 or old.index(START) >= old.index(END):
                raise ValueError("文献索引的自动区域标记已修改，请恢复标记后重试")
            a = old.index(START) + len(START)
            b = old.index(END)
            fresh = old[:a] + "\n" + body + old[b:]
            if fresh != old:
                atomic_write(folder / ".同步备份" / "00 文献索引.md.previous", old)
        else:
            fresh = START + "\n" + body + END + "\n\n## 我的综述线索\n\n"
        atomic_write(path, fresh)

    def sync_all(self):
        with self.lock:
            errors = []
            for p in self.state["papers"]:
                if not p.get("archived"):
                    self.sync_paper(p, with_index=False)
                    if self.state["settings"].get("sync_error"):
                        errors.append(self.state["settings"]["sync_error"])
            if self.state["settings"].get("sync_enabled"):
                try:
                    self.sync_index(Path(self.state["settings"]["vault_path"]) / "论文研读助手")
                except (OSError, ValueError) as exc:
                    errors.append(str(exc))
            self.state["settings"]["sync_error"] = "；".join(dict.fromkeys(errors))
            self.persist()
            return copy.deepcopy(self.state["settings"])

    def configure(self, body):
        enabled = bool(body.get("sync_enabled"))
        raw = str(body.get("vault_path", "")).strip().strip('"')
        if enabled:
            if not raw or not Path(raw).expanduser().is_dir():
                raise ValueError("请先选择一个已经存在的本地笔记库文件夹")
            raw = str(Path(raw).expanduser().resolve())
        with self.lock:
            self.state["settings"].update({"vault_path": raw, "sync_enabled": enabled, "sync_error": ""})
            self.persist()
            return self.sync_all() if enabled else copy.deepcopy(self.state["settings"])

    def backup(self):
        with self.lock:
            content = io.BytesIO()
            with zipfile.ZipFile(content, "w", zipfile.ZIP_DEFLATED) as z:
                state = copy.deepcopy(self.state)
                # Backup restores local records; it never re-enables writes to an old vault,
                # and it never re-arms the AI: incoming settings keep their configured values.
                state["settings"].update(sync_enabled=False, vault_path="", last_sync="", sync_error="")
                z.writestr("library.json", json.dumps(state, ensure_ascii=False, indent=2))
                analyses = extracted = 0
                for p in state["papers"]:
                    z.write(self.pdf_dir / f"{p['id']}.pdf", f"papers/{p['id']}.pdf")
                    z.writestr(f"notes/{p['note_name']}", markdown(p, f"[[../papers/{p['id']}.pdf|论文原文]]"))
                    # AI answers belong to the paper, so they travel with it. The extracted
                    # text cache is optional and can always be rebuilt from the PDF.
                    if self.files.has_analysis(p["id"]):
                        z.write(self.files.analysis_path(p["id"]), f"analyses/{p['id']}.json")
                        analyses += 1
                    if self.files.has_extracted(p["id"]):
                        z.write(self.files.extracted_path(p["id"]), f"extracted/{p['id']}.json")
                        extracted += 1
                z.writestr(BACKUP_MANIFEST, json.dumps({
                    "app": "paper-reader", "app_version": APP_VERSION,
                    "schema_version": schema.SCHEMA_VERSION,
                    "created_at": now(), "papers": len(state["papers"]),
                    "analyses": analyses, "extracted": extracted,
                }, ensure_ascii=False, indent=2))
            return content.getvalue()

    def restore(self, data):
        """Add papers this library does not have yet, after verifying the archive.

        A backup is an untrusted input. Nothing is written until the archive has been
        validated as a whole and every PDF has been re-extracted; imported answers are
        re-verified against that extraction and marked `source=imported`, and no credential,
        approval or queued job is ever restored.
        """
        names, warnings = backup_guard.inspect_archive(data)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            incoming = json.loads(archive.read("library.json").decode("utf-8"))
            if not isinstance(incoming, dict) or not isinstance(incoming.get("papers"), list):
                raise ValueError("这个文件不是当前版本的备份")
            if len(archive.read("library.json")) > backup_guard.MAX_LIBRARY_BYTES:
                raise ValueError("备份中的 library.json 过大")
            incoming, migrated = schema.migrate_state(incoming)   # accept version 1 backups

            def member(name, limit):
                """Read one optional member; the name is a literal, never user text."""
                if name not in names:
                    return None
                try:
                    blob = archive.read(name)
                except (KeyError, zipfile.BadZipFile, OSError):
                    return None
                if len(blob) > limit:
                    warnings.append({"code": "member_too_large", "member": name[:60]})
                    return None
                return blob

            # ---- stage 1: read and validate, touching no library file ----
            staged = []
            for paper in incoming["papers"]:
                validate_paper(paper)
                blob = archive.read(f"papers/{paper['id']}.pdf")
                if len(blob) > MAX_PDF or b"%PDF-" not in blob[:1024]:
                    raise ValueError("备份中的 PDF 无效或过大")
                staged.append({"paper": copy.deepcopy(paper), "pdf": blob,
                               "answer": member(f"analyses/{paper['id']}.json",
                                                backup_guard.MAX_ANSWER_BYTES)})
        with self.lock:
            existing = {p["id"] for p in self.state["papers"]}
            fresh = [item for item in staged if item["paper"]["id"] not in existing]
        if not fresh:
            self.last_restore_report = {"imported": 0, "verified": 0, "rejected": 0,
                                        "reports": [], "warnings": warnings,
                                        "migrated": migrated}
            return 0

        # ---- stage 2: extract every PDF afresh, outside the library lock ----
        reports, accepted = [], []
        scratch = Path(tempfile.mkdtemp(prefix="restore-"))
        try:
            for item in fresh:
                paper = item["paper"]
                pdf_path = scratch / f"{paper['id']}.pdf"
                pdf_path.write_bytes(item["pdf"])
                digest = pdftext.file_sha256(pdf_path)
                if self.extractor_factory is None:
                    # A backup is untrusted input, so the PDF is parsed in a short-lived child
                    # process with a scrubbed environment and a hard time limit. The cache
                    # payload it returns carries the pages, which is what the answer is then
                    # checked against - the archive's own cache is still never read.
                    cache, info, _ = extract_subprocess.extract_in_subprocess(
                        pdf_path, paper["id"], project_root=ROOT)
                    extracted = _CacheView(cache)
                else:
                    cache, info, extracted = backup_guard.rebuild_cache(
                        pdf_path, paper["id"], extractor_factory=self.extractor_factory)
                report = {"paper_id": paper["id"], "text": info,
                          "answer": "not_present" if item["answer"] is None else ""}
                record = None
                if item["answer"] is not None:
                    payload, error = storage.read_json_from_bytes(item["answer"])
                    if error is not None or payload is None:
                        report["answer"] = "unreadable"
                    else:
                        index, page_count = backup_guard.page_index_from(extracted)
                        record, answer_report = backup_guard.verify_analysis(
                            payload, paper_id=paper["id"], source_sha256=digest,
                            page_index=index, page_count=page_count or report["text"]["pages"],
                            prompt_version=self.prompt_version,
                            extracted_ok=(report["text"]["code"] in ("", "ok")
                                          and report["text"]["pages"] > 0))
                        report["answer"] = ("verified" if record is not None else "rejected")
                        report["codes"] = answer_report.get("codes", [])
                        report["verified_quotes"] = answer_report.get("verified_quotes", 0)
                        report["claimed_quotes"] = answer_report.get("claimed_quotes", 0)
                accepted.append({"paper": paper, "pdf": item["pdf"], "digest": digest,
                                 "cache": cache, "record": record,
                                 "had_answer": item["answer"] is not None})
                reports.append(report)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

        # ---- stage 3: commit only what was verified ----
        verified = rejected = 0
        with self.lock:
            existing = {p["id"] for p in self.state["papers"]}
            for item in accepted:
                paper = item["paper"]
                if paper["id"] in existing:
                    continue
                paper["note_name"] = clean_name(paper["title"]) + "--" + paper["id"][:8] + ".md"
                paper.setdefault("analysis", schema.default_analysis())
                paper["analysis"]["answer_file"] = ""
                # a restored row is never an authorisation to send anything
                if paper["analysis"]["status"] in ("queued", "running"):
                    paper["analysis"]["status"] = "waiting_for_user"
                    paper["analysis"]["stage"] = ""
                    paper["analysis"]["message"] = "这是导入的记录，需要重新确认才会请求模型"
                    paper["analysis"]["error_code"] = ""
                storage.atomic_write_bytes(self.files.pdf_path(paper["id"]), item["pdf"])
                if item["cache"] is not None:
                    self.files.write_extracted(paper["id"], item["cache"])
                if item["record"] is not None:
                    payload = dict(item["record"])
                    payload["source_sha256"] = item["digest"]
                    self.files.write_analysis(paper["id"], payload)
                    paper["analysis"]["answer_file"] = f"{paper['id']}.json"
                    paper["analysis"]["status"] = "ready"
                    if payload.get("verified") is False:
                        # Kept, but never presented as re-checked.
                        paper["analysis"]["message"] = ("已从备份恢复（旧版本参考，未按当前规则重新核对，"
                                                        "仅供参考）")
                        paper["analysis"]["error_code"] = "import_unverified"
                    else:
                        paper["analysis"]["message"] = "已从备份恢复，引用已按当前原文重新核对"
                        paper["analysis"]["error_code"] = ""
                        verified += 1
                    paper["analysis"]["source_sha256"] = item["digest"]
                elif item["had_answer"]:
                    # An answer came with the backup but could not be substantiated against
                    # the PDF here. The paper is intact; the reference is not shown as
                    # checked, and the reader is told plainly.
                    paper["analysis"]["status"] = "waiting_for_user"
                    paper["analysis"]["stage"] = ""
                    paper["analysis"]["message"] = ("备份中的 AI 参考没有通过按当前原文的重新核对，"
                                                    "需要重新生成")
                    paper["analysis"]["error_code"] = "import_unverified"
                validate_paper(paper)
                self.state["papers"].append(paper)
                existing.add(paper["id"])
            self.persist()
        rejected = len([report for report in reports if report["answer"] == "rejected"])
        # The published contract of restore() is the number of papers added. The detail -
        # which answers were re-verified, which were refused, which members were skipped -
        # is kept for the caller that wants to show it, and is never written to a log.
        self.last_restore_report = {
            "imported": len(accepted), "verified": verified, "rejected": rejected,
            "reports": reports, "warnings": warnings, "migrated": migrated}
        return len(accepted)


class Conflict(ValueError):
    pass


class _CacheView:
    """A cache payload wearing the extraction-result interface.

    The child process returns a cache payload; the answer verifier wants `.ok`, `.pages` and
    `.page_count`. This adapter keeps both sides honest instead of inventing a second format.
    """

    def __init__(self, payload):
        self._payload = payload or {}

    @property
    def ok(self):
        return bool(self._payload) and self._payload.get("code", "ok") == "ok"

    @property
    def pages(self):
        return self._payload.get("pages") or []

    @property
    def page_count(self):
        return int(self._payload.get("page_count") or 0)


def choose_directory():
    if os.name != "nt":
        raise ValueError("请在输入框中填写笔记库路径")
    script = """[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new(); Add-Type -AssemblyName System.Windows.Forms; $dialog = New-Object System.Windows.Forms.FolderBrowserDialog; $dialog.Description = 'Choose your Obsidian vault folder'; $dialog.ShowNewFolderButton = $false; if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { [Console]::Write($dialog.SelectedPath) }"""
    result = subprocess.run(["powershell.exe", "-NoProfile", "-STA", "-Command", script], capture_output=True, encoding="utf-8", timeout=300, creationflags=subprocess.CREATE_NO_WINDOW)
    return result.stdout.strip()


def analysis_status_payload(store, manager, paper_id):
    """Status, stage, progress, timestamps and a short error summary - never the answer."""
    meta = store.analysis_meta(paper_id)          # validates the id, raises for unknown papers
    queue = manager.queue_depth() if manager else {"queued": [], "running": []}
    return {
        "paper_id": paper_id,
        "status": meta.get("status", "none"),
        "stage": meta.get("stage", ""),
        "progress": meta.get("progress", 0),
        "message": meta.get("message", ""),
        "requested_at": meta.get("requested_at", ""),
        "started_at": meta.get("started_at", ""),
        "completed_at": meta.get("completed_at", ""),
        "updated_at": meta.get("updated_at", ""),
        "duration_ms": meta.get("duration_ms", 0),
        "attempt": meta.get("attempt", 0),
        "provider": meta.get("provider", ""),
        "adapter": meta.get("adapter", ""),
        "model": meta.get("model", ""),
        "prompt_version": meta.get("prompt_version", ""),
        "language": meta.get("language", ""),
        "error_code": meta.get("error_code", ""),
        "error_summary": (meta.get("error_message", "") or "")[:200],
        "answer_file": meta.get("answer_file", ""),
        "has_answer": bool(meta.get("answer_file")) and store.files.has_analysis(paper_id),
        "in_queue": paper_id in queue.get("queued", []),
        "running_now": paper_id in queue.get("running", []),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
    }


def analysis_answer_payload(store, paper_id):
    """Full analysis, and only when the job is `ready`."""
    meta = store.analysis_meta(paper_id)
    status = meta.get("status", "none")
    payload, error = store.analysis_answer(paper_id)
    answer = payload if status == "ready" and error is None else None
    reason = ""
    if answer is None:
        reason = ("参考分析尚未就绪" if status in ("none", "queued", "running", "waiting_for_user",
                                              "waiting_for_provider")
                  else (meta.get("message") or f"当前状态是 {status}"))
    return {"paper_id": paper_id, "analysis": meta, "status": status,
            "answer": answer,
            "answer_error": error or "",
            "answer_available": answer is not None,
            "reason": reason,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds")}


READ_TIMEOUT_SECONDS = 30           # per-connection socket timeout
DRAIN_LIMIT = 4 * 1024 * 1024        # bytes of a rejected body we will still absorb
DRAIN_TIMEOUT_SECONDS = 2.0          # and how long we wait for them
DEFAULT_MAX_CONCURRENCY = 8          # concurrent connections before refusing
DEFAULT_JSON_LIMIT = 16 * 1024          # settings, credentials, session: small on purpose
MANAGEMENT_JSON_LIMIT = 64 * 1024       # ordinary management JSON (save a reading card)
BEACON_JSON_LIMIT = 256 * 1024          # sendBeacon compatibility route, body token allowed
IMPORT_LIMIT = 80 * 1024 * 1024         # PDF import keeps its existing ceiling
RESTORE_LIMIT = 200 * 1024 * 1024       # backup restore keeps its dedicated ceiling
HTML_ENTRY_PATHS = ("/", "/index.html")
# Route-prefix -> body limit. Checked before a single byte of the body is read.
ROUTE_LIMITS = (
    ("/api/import", IMPORT_LIMIT),
    ("/api/restore", RESTORE_LIMIT),
    ("/api/beacon/", BEACON_JSON_LIMIT),
    ("/api/save", MANAGEMENT_JSON_LIMIT),
    ("/api/settings", DEFAULT_JSON_LIMIT),
    ("/api/session/bootstrap", DEFAULT_JSON_LIMIT),
    ("/api/", DEFAULT_JSON_LIMIT),
)


def limit_for(path):
    """Body ceiling for a path. Unknown /api/ routes get the smallest limit."""
    for prefix, limit in ROUTE_LIMITS:
        if path.startswith(prefix):
            return limit
    return MANAGEMENT_JSON_LIMIT


def cache_control_for(path):
    """`no-store` for anything that can carry session state; assets must revalidate.

    The previous rule gave `/static/` a one-hour cache. After an upgrade the browser then kept
    the OLD `app.js` while `index.html` was served fresh (`no-store`), so the page showed a new
    button whose handler did not exist in the cached script - the button did nothing. Static
    files are a few kilobytes on a loopback server, so revalidating them every load costs
    nothing and removes the whole class of "the update did not take effect" bugs.
    `/vendor/` (PDF.js and its resources, a few megabytes) stays cacheable: it is third-party
    and only changes with a release of its own.
    """
    clean = path.split("?", 1)[0]
    if clean in HTML_ENTRY_PATHS or clean.startswith("/api/"):
        return "no-store"
    if clean.startswith("/static/"):
        return "no-cache"
    if clean.startswith("/vendor/"):
        return "max-age=3600"
    return "no-store"


def handler_for(store, token, manager=None, sessions=None, limits=None):
    sessions = sessions if sessions is not None else session.SessionStore()
    limits = limits or {}
    concurrency = limits.get("concurrency")

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        timeout = limits.get("read_timeout", 30)

        def log_message(self, fmt, *args):
            pass

        def log_enqueue(self, outcome):
            if manager is not None:
                try:
                    manager.log({"event": "import_enqueue", **outcome})
                except (TypeError, ValueError):
                    pass

        def send(self, data, content_type="application/json; charset=utf-8", status=200,
                 filename=None, extra_headers=None, close=False):
            if isinstance(data, (dict, list)):
                if secret_guard.contains_secret(data):
                    data = {"ok": False, "code": secret_guard.E_SECRET_ECHO_BLOCKED,
                            "error": secret_guard.message_for(secret_guard.E_SECRET_ECHO_BLOCKED)}
                    status = 502
                data = json.dumps(data, ensure_ascii=False).encode("utf-8")
            elif isinstance(data, str):
                data = data.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", cache_control_for(self.path))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; style-src 'self' 'unsafe-inline'; worker-src 'self' blob:; img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self' blob:; object-src 'none'; base-uri 'self'; frame-ancestors 'none'")
            for name, value in (extra_headers or {}).items():
                self.send_header(name, value)
            if close:
                # A refusal that did not read the body must end the connection: unread bytes
                # would otherwise be parsed as the next request on a keep-alive socket, and
                # the client would see an aborted connection instead of this reply.
                self.send_header("Connection", "close")
                self.close_connection = True
            if filename:
                from urllib.parse import quote
                self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(filename))
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def check_host(self):
            host = self.headers.get("Host", "")
            if host not in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}:
                self.send({"error": "仅支持本机访问"}, status=403)
                return False
            origin = self.headers.get("Origin")
            if origin and origin not in {f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}:
                self.send({"error": "请求来源不受支持"}, status=403)
                return False
            if self.headers.get("Sec-Fetch-Site") == "cross-site":
                self.send({"error": "请求来源不受支持"}, status=403)
                return False
            return True

        # -- session ---------------------------------------------------------
        def cookie_session(self):
            """The session named by the cookie, or None."""
            jar = session.parse_cookies(self.headers.get("Cookie"))
            candidate = jar.get(session.COOKIE_NAME, "")
            if not candidate:
                return None
            found, _code = sessions.lookup(candidate)
            return found

        def authorized(self, body=None, csrf_required=False):
            """Cookie session first (the shipped path), then the legacy header token.

            The page no longer receives a management token, so a normal browser session
            authenticates with the cookie. `X-Reader-Token` remains accepted so an existing
            installation and a scripted client keep working; it is not a capability a page can
            obtain, and the body token is only honoured on the beacon compatibility route.
            """
            current = self.cookie_session()
            if current is not None:
                if csrf_required:
                    supplied = self.headers.get("X-CSRF-Token", "")
                    code = sessions.check_csrf(current, supplied)
                    if code != session.BOOTSTRAP_OK:
                        self.send({"error": "请求校验未通过，请重新打开页面",
                                   "code": code}, status=403)
                        return False
                return True
            supplied = self.headers.get("X-Reader-Token", "")
            if supplied and secrets.compare_digest(str(supplied), token):
                return True
            if body is not None and self.allow_body_token():
                candidate = str(body.get("token", "") or "")
                if candidate and secrets.compare_digest(candidate, token):
                    return True
            self.send({"error": "页面已失效，请重新打开软件"}, status=403)
            return False

        def allow_body_token(self):
            """Only the sendBeacon compatibility route may carry a token in the body."""
            return self.path.split("?", 1)[0].startswith("/api/beacon/")

        def handle_bootstrap(self, raw):
            """Exchange a one-time capability for a cookie session.

            The capability comes from the launcher pipe and reaches the page through the URL
            fragment, so it never appears in a query string, a log or storage. The response
            body carries the CSRF token once; it is not stored server-side in any file.
            """
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self.send({"error": "引导信息格式不正确", "code": session.BOOTSTRAP_MALFORMED},
                          status=400)
                return
            capability = str((payload or {}).get("capability", "") or "")
            found, code = sessions.exchange_bootstrap(
                capability, self.headers.get("User-Agent", ""))
            if found is None:
                self.log_bootstrap_failure(code)
                self.send({"error": "无法建立会话，请重新运行启动文件", "code": code}, status=403)
                return
            self.send({"ok": True, "csrf": found.csrf, "version": APP_VERSION,
                       "session": found.as_dict()},
                      extra_headers={"Set-Cookie": session.cookie_header(found.session_id)})

        def log_bootstrap_failure(self, code):
            if manager is not None:
                try:
                    manager.log(secret_guard.log_record(event="bootstrap_rejected", code=code))
                except (TypeError, ValueError):
                    pass

        def handle_beacon(self, path, body):
            """The sendBeacon compatibility route.

            `navigator.sendBeacon` cannot set request headers, so the CSRF token arrives in
            the body. This is the only route that accepts it there, it is limited to
            `BEACON_JSON_LIMIT`, and it performs exactly the same store call as `/api/save` -
            no extra authority, no bypass of the reading-card rules.
            """
            supplied = str(body.get("csrf", "") or "")
            current = self.cookie_session()
            if current is None or sessions.check_csrf(current, supplied) != session.BOOTSTRAP_OK:
                self.send({"error": "页面已失效，请重新打开软件"}, status=403)
                return
            action = path[len("/api/beacon/"):]
            if action == "save":
                payload = {key: value for key, value in body.items() if key != "csrf"}
                self.send(store.save_paper(payload))
            else:
                self.send({"error": "操作不存在"}, status=404)

        # -- body ------------------------------------------------------------
        def read_body(self, path):
            """Read at most the route's limit, after the headers have been checked.

            Returns bytes, or None when the request was already answered (too large, empty,
            malformed length). Nothing is read from the socket before the limit is known, so
            an unauthenticated 100 MiB body cannot occupy a worker.
            """
            raw_length = self.headers.get("Content-Length", "")
            if raw_length is None or str(raw_length).strip() == "":
                # Refusing without reading the body means the connection must end here: the
                # unread bytes would otherwise be parsed as the next request on a keep-alive
                # socket, and the client would see an aborted connection instead of this reply.
                self.send({"error": "缺少 Content-Length"}, status=411, close=True)
                return None
            try:
                length = int(str(raw_length).strip())
            except (TypeError, ValueError):
                self.send({"error": "Content-Length 无效"}, status=400, close=True)
                return None
            if length < 0:
                self.send({"error": "Content-Length 无效"}, status=400, close=True)
                return None
            limit = limit_for(path)
            if length == 0:
                self.send({"error": "请求为空"}, status=413, close=True)
                return None
            if length > limit:
                # Ask for no more of it, but do take what the client has already sent: an
                # upload cut off mid-body surfaces as an aborted connection instead of this
                # 413, which is confusing and hard to test.
                self.drain(length)
                self.send({"error": f"请求过大（上限 {limit // 1024} KiB）"}, status=413,
                          close=True)
                return None
            try:
                body = self.rfile.read(length)
            except (OSError, ValueError):
                self.send({"error": "读取请求失败"}, status=400)
                return None
            if len(body) != length:
                self.send({"error": "读取请求不完整"}, status=400)
                return None
            return body

        def drain(self, declared_length):
            """Consume a rejected body without storing it.

            Bounded on purpose: at most `DRAIN_LIMIT` bytes and with a short timeout, so a
            client that announces a huge body cannot keep a worker busy by trickling it.
            """
            remaining = min(int(declared_length), DRAIN_LIMIT)
            previous = None
            try:
                previous = self.connection.gettimeout()
                self.connection.settimeout(DRAIN_TIMEOUT_SECONDS)
            except OSError:
                previous = None
            try:
                while remaining > 0:
                    chunk = self.rfile.read(min(remaining, 64 * 1024))
                    if not chunk:
                        break
                    remaining -= len(chunk)
            except (OSError, ValueError):
                pass
            finally:
                if previous is not None:
                    try:
                        self.connection.settimeout(previous)
                    except OSError:
                        pass

        def analysis_route(self, path):
            """Resolve /api/analysis/... to (paper_id, action).

            Path traversal is detected on the *raw* path before any normalisation, so a
            crafted id can never be silently rewritten into a different lookup.
            """
            raw = self.path.split("?", 1)[0].replace("%2e", ".", 1).replace("%2E", ".")
            unquoted_raw = unquote(raw)
            suffix = unquoted_raw[len("/api/analysis/"):] if unquoted_raw.startswith("/api/analysis/") else ""
            if ".." in suffix or "\\" in suffix:
                raise ValueError("无效的论文编号")
            parts = [part for part in suffix.split("/") if part != ""]
            return (parts[0] if parts else ""), (parts[1] if len(parts) > 1 else "")

        def do_GET(self):
            if not self.check_host():
                return
            path = unquote(urlparse(self.path).path)
            try:
                if path == "/api/health":
                    # Public: version and readiness only. No path, no credential, no model.
                    self.send({"app": "paper-reader", "version": APP_VERSION,
                               "schema_version": schema.SCHEMA_VERSION,
                               "root": hashlib.sha256(str(ROOT).encode()).hexdigest()[:16],
                               "data": hashlib.sha256(str(store.directory).encode()).hexdigest()[:16]})
                    return
                if path == "/api/session":
                    # Identity for a page that already holds a session; never mints one.
                    current = self.cookie_session()
                    if current is None:
                        self.send({"error": "会话不存在，请重新运行启动文件", "code": "session_missing"},
                                  status=401)
                        return
                    self.send({"ok": True, "csrf": current.csrf,
                               "session": current.as_dict(), "version": APP_VERSION})
                    return
                if path.startswith("/api/") and not self.authorized():
                    return
                if path == "/api/state":
                    self.send(store.snapshot())
                elif path == "/api/backup":
                    self.send(store.backup(), "application/zip", filename="论文研读备份-" + datetime.now().strftime("%Y%m%d") + ".zip")
                elif path == "/api/ai-settings":
                    # Credential status is a reference and a state, never a value: no key,
                    # no mask, no leading/trailing fragment.
                    self.send({"ai": store.ai_settings(), "providers": list(schema.PROVIDERS),
                               "credential": providers.credential_report(store.ai_settings(),
                                                                        store.files),
                               "registry": credentials.registry_hint(),
                               "budget": manager.budget_status() if manager else {},
                               "pause": {"paused": manager.paused()} if manager else
                                        {"paused": False}})
                elif path == "/api/analysis":
                    health = None
                    blocked_code, blocked_message = ("", "") if manager is None \
                        else manager.outbound_blocked()
                    if manager is not None and not blocked_code:
                        ok, adapter, checked = manager.provider_ready()
                        health = dict(checked.as_dict(), provider=adapter.name, ready=ok)
                    elif blocked_code:
                        # paused or disabled: do not even probe the provider, and say why
                        health = {"ok": False, "code": blocked_code, "ready": False,
                                  "detail": blocked_message}
                    self.send({"statuses": manager.statuses() if manager else {},
                               "queue": manager.queue_depth() if manager else {},
                               "budget": manager.budget_status() if manager else {},
                               "outbound": {"blocked": bool(blocked_code),
                                            "code": blocked_code,
                                            "message": blocked_message},
                               "health": health})
                elif path.startswith("/api/analysis/"):
                    paper_id, action = self.analysis_route(path)
                    if action == "status":
                        # Status only: never the answer body. Safe for polling.
                        self.send(analysis_status_payload(store, manager, paper_id))
                    elif action in ("", "answer"):
                        # Full answer. The UI only calls this after the user clicks
                        # "查看 AI 参考"; anything not ready returns no content.
                        self.send(analysis_answer_payload(store, paper_id))
                    else:
                        self.send({"error": "操作不存在"}, status=404)
                elif path.startswith("/api/pdf/"):
                    p = store.paper(path.removeprefix("/api/pdf/"))
                    self.send((store.pdf_dir / f"{p['id']}.pdf").read_bytes(), "application/pdf", filename=clean_name(p['title'], 120) + ".pdf")
                elif path.startswith("/api/export/"):
                    p = copy.deepcopy(store.paper(path.removeprefix("/api/export/")))
                    self.send(markdown(p, f"[[{clean_name(p['title'], 120)}.pdf|论文原文]]"), "text/markdown; charset=utf-8", filename=p["note_name"])
                elif path in ("/", "/index.html"):
                    # The page no longer receives a management token: it gets a hint that a
                    # launcher-mediated session is expected, and the fragment carries the
                    # one-time capability the launcher already placed in the address bar.
                    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
                    # `__VERSION__` version-stamps every local asset reference, so a browser
                    # that still holds an old copy of a script asks for a different URL after an
                    # upgrade instead of silently running the old code.
                    self.send(html.replace("__VERSION__", APP_VERSION).replace(
                        "__BOOTSTRAP__", json.dumps(
                            {"version": APP_VERSION, "session_required": True})),
                        "text/html; charset=utf-8")
                elif path.startswith(("/static/", "/vendor/")):
                    target = (ROOT / path.lstrip("/")).resolve()
                    allowed = (ROOT / path.split("/")[1]).resolve()
                    if not target.is_relative_to(allowed) or not target.is_file():
                        self.send({"error": "文件不存在"}, status=404)
                        return
                    mime = "text/javascript" if target.suffix in (".js", ".mjs") else mimetypes.guess_type(str(target))[0] or "application/octet-stream"
                    self.send(target.read_bytes(), mime)
                else:
                    self.send({"error": "页面不存在"}, status=404)
            except (ValueError, OSError, KeyError) as exc:
                self.send({"error": str(exc)}, status=400)

        def do_POST(self):
            if not self.check_host():
                return
            path = urlparse(self.path).path
            try:
                # Session bootstrap is the one route that runs before a session exists; it is
                # small, single-use and bounded by the capability itself.
                if path == "/api/session/bootstrap":
                    payload = self.read_body(path)
                    if payload is None:
                        return
                    self.handle_bootstrap(payload)
                    return
                if path in ("/api/import", "/api/restore"):
                    # Authentication happens on headers, before the (large) body is read.
                    if not self.authorized(csrf_required=True):
                        return
                    data = self.read_body(path)
                    if data is None:
                        return
                    if path == "/api/import":
                        filename = unquote(self.headers.get("X-File-Name", "paper.pdf"))
                        paper = store.import_pdf(data, filename)
                        # Record the job and return: the worker picks it up, the request
                        # never waits for a model. Only a user who switched automatic
                        # analysis on has authorised the send itself; otherwise the paper
                        # stays waiting for an explicit click.
                        auto_analyze = bool(store.ai_settings().get("auto_analyze"))
                        if manager is not None:
                            try:
                                outcome = manager.enqueue(
                                    paper["id"],
                                    reason="import" if auto_analyze else "prefetch")
                                self.log_enqueue(outcome)
                            except Exception as exc:                  # noqa: BLE001 - import must succeed
                                self.log_enqueue({"error": f"{type(exc).__name__}: {exc}"})
                        self.send({"paper": copy.deepcopy(store.paper(paper["id"]))})
                    else:
                        self.send({"imported": store.restore(data), "state": store.snapshot()})
                    return
                # Ordinary JSON: the ceiling is enforced before authentication and before a
                # single byte of the body is read.
                raw = self.read_body(path)
                if raw is None:
                    return
                try:
                    body = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self.send({"error": "请求内容不是合法 JSON"}, status=400)
                    return
                if not isinstance(body, dict):
                    self.send({"error": "请求内容必须是对象"}, status=400)
                    return
                if path.startswith("/api/beacon/"):
                    # sendBeacon cannot set headers, so this route is the one exception: the
                    # CSRF token arrives in the body and is checked by handle_beacon against
                    # the cookie session. No other route may do this.
                    self.handle_beacon(path, body)
                    return
                if not self.authorized(body, csrf_required=True):
                    return
                if path == "/api/session/revoke":
                    current = self.cookie_session()
                    if current is not None:
                        sessions.revoke(current.session_id)
                    self.send({"ok": True})
                elif path == "/api/save":
                    self.send(store.save_paper(body))
                elif path == "/api/settings":
                    # Obsidian sync plus the AI connection settings, in one save.
                    # A key value is never accepted and never echoed: only the *name* of
                    # the environment variable that holds it.
                    if "api_key" in body or "api_key_value" in body:
                        raise ValueError("不要把密钥写进设置；请填写保存密钥的环境变量名")
                    forbidden = [name for name in ("raw_key", "secret", "api_secret",
                                                   "password", "access_token", "token")
                                 if name in body]
                    if forbidden:
                        raise ValueError("设置里不能提交凭据字段：" + "、".join(sorted(forbidden)))
                    if "api_key_env" in body:
                        # Reject an unregistered name at save time, so the user learns it here
                        # instead of reading a refusal later during a model call.
                        requested = credentials.normalise_ref(body.get("api_key_env", ""))
                        if requested and not credentials.is_registered(credentials.name_of(requested)):
                            raise ValueError(
                                "这个环境变量名没有登记为应用凭据，已拒绝保存。"
                                "请使用 PAPER_READER_ 开头，或常见服务商名称。")
                    settings = store.configure(body)
                    ai_patch = {k: body[k] for k in schema.AI_SETTINGS_DEFAULTS if k in body}
                    if ai_patch:
                        store.update_ai_settings(ai_patch)
                        settings = copy.deepcopy(store.state["settings"])
                    result = {"settings": settings, "ai": settings["ai"],
                              "providers": list(schema.PROVIDERS),
                              "credential": providers.credential_report(settings["ai"],
                                                                       store.files)}
                    if manager is not None:
                        # Saving a target here is the user naming and confirming a destination,
                        # which is what target approval records. It does not authorise any
                        # paper: sending still needs a per-paper approval.
                        manager.approve_target(result["ai"])
                        result["resumed"] = manager.resume_waiting()
                        ok, adapter, checked = manager.provider_ready(result["ai"])
                        result["health"] = dict(checked.as_dict(), provider=adapter.name, ready=ok)
                    self.send(result)
                elif path == "/api/ai-settings":
                    # Settings are written first. Contacting the provider is a separate step
                    # and only happens for a target that is approved, unpaused and enabled.
                    updated = store.update_ai_settings(body.get("ai", body))
                    result = {"ai": updated}
                    if manager is not None:
                        manager.approve_target(updated)
                        # Papers that were waiting for a provider get queued as soon as one
                        # is configured; recovery is idempotent.
                        result["resumed"] = manager.resume_waiting()
                        ok, adapter, checked = manager.provider_ready(updated)
                        result["health"] = dict(checked.as_dict(), provider=adapter.name, ready=ok)
                    self.send(result)
                elif path.startswith("/api/analysis/") and path.endswith("/generate"):
                    # This POST *is* the user's approval for this paper: it records an
                    # approval for the current target and freezes a job. `force` means an
                    # explicit re-run of an existing answer, which is a different action.
                    if manager is None:
                        raise ValueError("后台分析未启用")
                    paper_id = path[len("/api/analysis/"):-len("/generate")].strip("/")
                    store.paper(paper_id)
                    force = bool(body.get("force", False))
                    # Both a first request and a forced re-run are explicit user actions on
                    # this paper, so either one records the approval for the current target.
                    manager.grant_approval(paper_id,
                                           model=store.ai_settings().get("model", ""),
                                           scope="manual")
                    outcome = manager.enqueue(paper_id, reason="force" if force else "manual",
                                              force=force)
                    self.send({"run": outcome,
                               "status": analysis_status_payload(store, manager, paper_id)})
                elif path == "/api/ai/pause":
                    if manager is None:
                        raise ValueError("后台分析未启用")
                    paused = bool(body.get("paused", True))
                    manager.pause(paused, reason=str(body.get("reason", "user"))[:40])
                    if paused:
                        # pausing must stop queued work, not only future requests
                        for paper in list(store.state.get("papers", [])):
                            if (paper.get("analysis") or {}).get("status") == "queued":
                                manager.cancel(paper.get("id", ""))
                    self.send({"paused": manager.paused(), "budget": manager.budget_status()})
                elif path == "/api/ai/profile":
                    if manager is None:
                        raise ValueError("后台分析未启用")
                    settings = store.ai_settings()
                    profile_id = str(body.get("profile_id") or "") or \
                        manager.current_profile_id(settings)
                    disabled = bool(body.get("disabled", True))
                    manager.set_profile_disabled(profile_id, disabled)
                    self.send({"profile_id": profile_id,
                               "disabled_profiles": manager.ledger.disabled_profiles()})
                elif path.startswith("/api/analysis/") and path.endswith("/cancel"):
                    if manager is None:
                        raise ValueError("后台分析未启用")
                    paper_id = path[len("/api/analysis/"):-len("/cancel")].strip("/")
                    store.paper(paper_id)
                    self.send({"cancelled": manager.cancel(paper_id),
                               "status": analysis_status_payload(store, manager, paper_id)})
                elif path == "/api/analysis-run":
                    # Kept for compatibility with the earlier wiring.
                    if manager is None:
                        raise ValueError("后台分析未启用")
                    paper_id = str(body.get("paper_id", ""))
                    store.paper(paper_id)
                    # no default force: a compatibility call must not become a re-run
                    outcome = manager.enqueue(paper_id, reason="manual",
                                              force=bool(body.get("force", False)))
                    self.send({"run": outcome, "analysis": store.analysis_meta(paper_id)})
                elif path == "/api/analysis-cancel":
                    if manager is None:
                        raise ValueError("后台分析未启用")
                    paper_id = str(body.get("paper_id", ""))
                    store.paper(paper_id)
                    self.send({"cancelled": manager.cancel(paper_id),
                               "analysis": store.analysis_meta(paper_id)})
                elif path == "/api/delete":
                    # Destructive and irreversible from inside the app: the PDF, the AI answer,
                    # the failed-answer log and the extraction cache go, and the record leaves
                    # the library. An empty id would otherwise read as "the first paper", so it
                    # is refused with the same wording as a malformed one. Nothing outside
                    # `data/` is touched - the Obsidian note in the vault is never deleted.
                    paper_id = str(body.get("paper_id", "") or "").strip()
                    if not paper_id:
                        raise ValueError("请选择要删除的论文")
                    result = store.delete_paper(paper_id)     # validates the id, raises 400
                    # Audit line from the fixed field whitelist only: event, the id, the
                    # outcome and how many files were removed. No payload, no path, no title.
                    try:
                        log_event(secret_guard.log_record(
                            event="paper_deleted", paper_id=paper_id, outcome="ok",
                            count=sum(1 for value in result["deleted"].values() if value)))
                    except (TypeError, ValueError):
                        pass
                    self.send({"deleted": result["deleted"], "title": result["title"],
                               "state": store.snapshot()})
                elif path == "/api/sync":
                    if not store.state["settings"].get("sync_enabled"):
                        raise ValueError("请先选择笔记库并开启同步")
                    self.send({"settings": store.sync_all()})
                elif path == "/api/choose-vault":
                    self.send({"path": choose_directory()})
                elif path == "/api/shutdown":
                    self.send({"ok": True})
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                else:
                    self.send({"error": "操作不存在"}, status=404)
            except Conflict as exc:
                self.send({"error": str(exc)}, status=409)
            except (ValueError, OSError, KeyError, TypeError, zipfile.BadZipFile, subprocess.SubprocessError) as exc:
                self.send({"error": str(exc)}, status=400)
    return Handler


def log_event(record):
    """One JSON line per event, built from a fixed field whitelist.

    The whitelist is the primary control: a caller cannot add a field that carries a
    credential, a prompt or an upstream body. `secret_guard.redact` still runs over the
    line as a second layer.
    """
    safe = secret_guard.pick(record, secret_guard.LOG_FIELDS)
    if not safe:
        safe = {"event": "log"}
    line = json.dumps(safe, ensure_ascii=False)
    sys.stderr.write(secret_guard.redact(line) + "\n")
    sys.stderr.flush()


def acquire_instance_lock(directory):
    """Take the single-writer lock for this library.

    Returns the open file object, or None when another process already holds it. On
    Windows an exclusive `open()` is not required for locking, and a *sharing violation*
    (WinError 32 / PermissionError) simply means somebody else has it open - that is the
    "already running" case, not a crash.
    """
    path = directory / "instance.lock"
    try:
        handle = os.open(str(path), os.O_RDWR | os.O_CREAT)
    except (PermissionError, OSError):
        return None
    lock_file = os.fdopen(handle, "r+b", buffering=0)
    try:
        if lock_file.seek(0, os.SEEK_END) == 0:
            lock_file.write(b"0")
        lock_file.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_file.close()
        return None
    return lock_file


def probe_service(port, timeout=1.0):
    """Return /api/health for a local paper-reader service, or None."""
    try:
        from urllib.request import urlopen
        with urlopen(f"http://127.0.0.1:{int(port)}/api/health", timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return payload if payload.get("app") == "paper-reader" else None
    except Exception:                                        # noqa: BLE001 - probe only
        return None


def find_running_service(start_port, fingerprint, data_fingerprint):
    """Find a local service for this folder, else any running instance.

    Returns (url, exact, health). `exact` is True when the service belongs to this very
    folder, which is the only case where reusing it is completely safe.
    """
    recorded = ROOT / "run" / "server.json"
    ports = []
    try:
        ports.append(int(json.loads(recorded.read_text(encoding="utf-8"))["port"]))
    except Exception:                                        # noqa: BLE001 - optional file
        pass
    ports += [port for port in range(start_port, start_port + 30) if port not in ports]
    fallback = None
    for port in ports:
        health = probe_service(port)
        if not health:
            continue
        if health.get("root") == fingerprint and health.get("data") == data_fingerprint:
            return f"http://127.0.0.1:{port}", True, health
        if fallback is None:
            fallback = (f"http://127.0.0.1:{port}", health)
    if fallback:
        return fallback[0], False, fallback[1]
    return None, False, None


DIALOG_CODE = ("import ctypes,sys;"
               "ctypes.windll.user32.MessageBoxW(0, sys.argv[1], sys.argv[2], int(sys.argv[3]))")


def show_error(message, title="论文研读助手"):
    """Surface a startup failure even when started without a console (pythonw).

    The dialog is raised by a separate process: MessageBoxW is modal, so showing it here
    would block the start-up path (and hang scripted runs) until it was dismissed.
    """
    sys.stderr.write(message + "\n")
    sys.stderr.flush()
    if os.name != "nt" or os.environ.get("PAPER_READER_NO_DIALOG") == "1":
        return
    try:
        subprocess.Popen([sys.executable, "-c", DIALOG_CODE, message, title, str(0x10)],
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:                                        # noqa: BLE001 - best effort
        pass


class BoundedHTTPServer(ThreadingHTTPServer):
    """Threading server with a hard cap on concurrent connections.

    `ThreadingHTTPServer` already refuses unbounded growth by spawning a thread per request;
    this adds the ceiling the spec asks for: beyond `max_concurrency` the connection is closed
    immediately instead of being queued, so a slow or hostile client cannot occupy the worker
    budget that manual reading also uses.
    """

    max_concurrency = 8
    # Deliberately NOT `allow_reuse_address`. On Windows that flag lets a *second* process bind
    # a port another instance is already listening on, and the two then split incoming
    # connections unpredictably - observed in the field as "the new version will not start" and
    # as a UI button that silently does nothing because requests reached the other process.
    # Without it the bind fails, `run_server` moves to the next free port, and every instance
    # keeps its own address.
    allow_reuse_address = False

    def __init__(self, *args, **kwargs):
        self.max_concurrency = int(kwargs.pop("max_concurrency", self.max_concurrency) or 8)
        self._active = 0
        self.refused_connections = 0
        self._active_lock = threading.Lock()
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        """Accept only while there is budget; otherwise close immediately.

        The counter is incremented exactly once per *accepted* connection and released in
        `shutdown_request` for the same connection. A refused connection must not touch it:
        an earlier version incremented on refusal paths without a matching release, so the
        service slowly refused everything.
        """
        with self._active_lock:
            if self._active >= self.max_concurrency:
                self.refused_connections += 1
                try:
                    request.close()
                except OSError:
                    pass
                return
            self._active += 1
        try:
            super().process_request(request, client_address)
        except Exception:                                        # noqa: BLE001
            with self._active_lock:
                self._active = max(0, self._active - 1)
            raise

    def shutdown_request(self, request):
        try:
            super().shutdown_request(request)
        finally:
            with self._active_lock:
                self._active = max(0, self._active - 1)

    def active_connections(self):
        with self._active_lock:
            return self._active


def run_server(args):
    """Start the service; returns an exit code. Never raises."""
    directory = Path(args.data_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    fingerprint = hashlib.sha256(str(ROOT).encode()).hexdigest()[:16]
    data_fingerprint = hashlib.sha256(str(ROOT / "data").encode()).hexdigest()[:16]

    instance_lock = acquire_instance_lock(directory)
    if instance_lock is None:
        # Another process owns this library. Point the user at it instead of failing.
        url, exact, health = find_running_service(args.port, fingerprint, data_fingerprint)
        if url and exact:
            if args.open:
                webbrowser.open(url)
            sys.stderr.write(f"already running: {url}\n")
            return 0
        if url:
            show_error("已经有一个“论文研读助手”实例在运行：\n"
                       f"{url}\n\n如果那是旧版本，请先关掉它（在它的设置页点“保存并退出软件”），"
                       "然后再启动本目录。")
            return 3
        show_error("这个文献库正被另一个进程占用（data\\instance.lock）。\n"
                   "请先在任务管理器里结束残留的 pythonw.exe，或重启电脑后重试。")
        return 4

    store = None
    try:
        store = Store(args.data_dir)
        manager = analysis.AnalysisManager(store, log=log_event)
        manager.recover()
        if not args.no_worker:
            manager.start()
        token = secrets.token_urlsafe(32)
        sessions = session.SessionStore()
        limits = {"read_timeout": READ_TIMEOUT_SECONDS,
                  "concurrency": args.max_concurrency}
        httpd = None
        for port in range(args.port, args.port + 30):
            try:
                httpd = BoundedHTTPServer(
                    ("127.0.0.1", port),
                    handler_for(store, token, manager, sessions=sessions, limits=limits),
                    max_concurrency=args.max_concurrency)
                break
            except OSError:
                continue
        if httpd is None:
            show_error("无法启动本机服务：8765 起的 30 个端口都被占用。\n请关闭其它程序后重试。")
            return 5
        httpd.daemon_threads = True

        # The launcher pipe is the only source of bootstrap capabilities. Its name is public
        # (it carries no secret); the capability it hands out is not.
        # The pipe name is derived from the library, not the installation: two instances with
        # different data directories (a second library, or the ones a test suite starts) must
        # not compete for the same pipe, or a bootstrap could be handed to the wrong service.
        fingerprint = hashlib.sha256(str(Path(args.data_dir).resolve()).encode()).hexdigest()[:16]
        pipe_name = session.launcher_pipe_name(fingerprint)
        pipe = launcher_pipe.LauncherPipe(pipe_name, sessions, log=log_event)
        pipe_ready = pipe.start()
        public_state = {"port": httpd.server_port, "pid": os.getpid(),
                        "pipe": pipe_name if pipe_ready else "",
                        "pipe_error": "" if pipe_ready else pipe.error}
        atomic_write(ROOT / "run" / "server.json", json.dumps(public_state))
        # The same public facts are recorded next to the library, so several instances (for
        # example the ones a test suite starts) can each be asked for a bootstrap on their own
        # pipe instead of all pointing at whichever instance wrote run/server.json last.
        atomic_write(Path(args.data_dir) / "run-instance.json", json.dumps(public_state))
        url = f"http://127.0.0.1:{httpd.server_port}"
        if args.open:
            # Opening the page ourselves would give the browser a URL without a capability, so
            # the launcher does the opening after it has read one from the pipe. This path is
            # kept for a manual `python server.py --open`.
            webbrowser.open(url)
        print(url, flush=True)
        try:
            httpd.serve_forever()
        finally:
            httpd.server_close()
            # Sessions and bootstrap capabilities die with the process, so a page left open
            # cannot keep using management endpoints.
            sessions.revoke_all()
            pipe.stop()
        return 0
    finally:
        instance_lock.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", default=str(ROOT / "data"))
    parser.add_argument("--open", action="store_true")
    parser.add_argument("--no-worker", action="store_true",
                        help="不启动后台分析线程（测试或调试用）")
    parser.add_argument("--max-concurrency", type=int, default=DEFAULT_MAX_CONCURRENCY,
                        help="并发连接上限（超出立即拒绝，不排队）")
    args = parser.parse_args()
    try:
        return run_server(args)
    except SystemExit:
        raise
    except BaseException:                                    # noqa: BLE001 - report, never crash
        import traceback
        detail = traceback.format_exc()
        sys.stderr.write(detail)
        sys.stderr.flush()
        last = [line for line in detail.strip().splitlines() if line.strip()][-1]
        show_error("论文研读助手启动失败：\n\n" + last[:400] +
                   "\n\n完整信息见软件目录下 run\\startup.log。")
        return 1


if __name__ == "__main__":
    sys.exit(main())
