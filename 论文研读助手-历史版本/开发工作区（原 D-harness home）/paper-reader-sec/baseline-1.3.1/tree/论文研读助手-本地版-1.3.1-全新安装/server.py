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
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse
import uuid
import webbrowser
import zipfile

import analysis
import providers
import schema
import storage
from storage import DataStore

ROOT = Path(__file__).resolve().parent
APP_VERSION = "1.3.1"          # library schema version 2 (see schema.py)
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


class Store:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.files = DataStore(self.directory).ensure_directories()
        self.path = self.files.library
        self.pdf_dir = self.files.papers
        self.lock = threading.RLock()
        self.migrated = False
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

    def update_ai_settings(self, patch):
        """Merge a partial settings.ai patch. Never stores a key, only the env name."""
        with self.lock:
            merged = copy.deepcopy(self.state["settings"]["ai"])
            for key, value in (patch or {}).items():
                if key not in schema.AI_SETTINGS_DEFAULTS:
                    raise ValueError("AI 设置含有未知字段：" + str(key))
                merged[key] = value
            merged = schema.validate_ai_settings(merged)
            self.state["settings"]["ai"] = merged
            self.persist()
            return copy.deepcopy(merged)

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
        with self.lock:
            return copy.deepcopy(self.state)

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
                 "created": now(), "updated": now(), "archived": False, "note_name": clean_name(title) + "--" + paper_id[:8] + ".md",
                 "analysis": schema.validate_analysis(analysis_meta)}
        with self.lock:
            storage.atomic_write_bytes(self.files.pdf_path(paper_id), data)
            self.state["papers"].append(paper)
            self.sync_paper(paper)
            self.persist()
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
        """Add papers that this library does not have yet.

        Backups from schema version 1 (no analyses/, no extracted/, no manifest) restore
        their manual records normally; the reader stays fully usable afterwards.
        """
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            if sum(x.file_size for x in z.infolist()) > 500 * 1024 * 1024:
                raise ValueError("备份解压大小超过 500 MB")
            incoming = json.loads(z.read("library.json").decode("utf-8"))
            if not isinstance(incoming, dict) or not isinstance(incoming.get("papers"), list):
                raise ValueError("这个文件不是当前版本的备份")
            incoming, _ = schema.migrate_state(incoming)   # accept version 1 backups
            names = set(z.namelist())

            def optional(member):
                """Read an optional member; a crafted name can never become a path."""
                if member not in names:
                    return None
                try:
                    return z.read(member)
                except (KeyError, zipfile.BadZipFile, OSError):
                    return None

            staged = []
            for p in incoming["papers"]:
                validate_paper(p)
                blob = z.read(f"papers/{p['id']}.pdf")
                if len(blob) > MAX_PDF or b"%PDF-" not in blob[:1024]:
                    raise ValueError("备份中的 PDF 无效或过大")
                staged.append((copy.deepcopy(p), blob,
                               optional(f"analyses/{p['id']}.json"),
                               optional(f"extracted/{p['id']}.json")))
        with self.lock:
            existing = {p["id"] for p in self.state["papers"]}
            count = 0
            for p, blob, answer, extracted in staged:
                if p["id"] in existing:
                    continue
                p["note_name"] = clean_name(p["title"]) + "--" + p["id"][:8] + ".md"
                p.setdefault("analysis", schema.default_analysis())
                p["analysis"]["answer_file"] = ""
                storage.atomic_write_bytes(self.files.pdf_path(p["id"]), blob)
                if answer is not None:
                    payload, error = storage.read_json_from_bytes(answer)
                    if error is None:
                        name = self.files.write_analysis(p["id"], payload)
                        p["analysis"]["answer_file"] = name
                        if p["analysis"]["status"] == "none":
                            p["analysis"]["status"] = "ready"
                if extracted is not None:
                    payload, error = storage.read_json_from_bytes(extracted)
                    if error is None:
                        self.files.write_extracted(p["id"], payload)
                validate_paper(p)
                self.state["papers"].append(p)
                existing.add(p["id"])
                count += 1
            self.persist()
            return count


class Conflict(ValueError):
    pass


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


def handler_for(store, token, manager=None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def log_enqueue(self, outcome):
            if manager is not None:
                try:
                    manager.log({"event": "import_enqueue", **outcome})
                except (TypeError, ValueError):
                    pass

        def send(self, data, content_type="application/json; charset=utf-8", status=200, filename=None):
            if isinstance(data, (dict, list)):
                data = json.dumps(data, ensure_ascii=False).encode("utf-8")
            elif isinstance(data, str):
                data = data.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store" if "api/" in self.path or self.path == "/" else "max-age=3600")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; style-src 'self' 'unsafe-inline'; worker-src 'self' blob:; img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self' blob:; object-src 'none'; base-uri 'self'; frame-ancestors 'none'")
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

        def authorized(self, body=None):
            supplied = self.headers.get("X-Reader-Token", "") or (body or {}).get("token", "")
            if not secrets.compare_digest(str(supplied), token):
                self.send({"error": "页面已失效，请重新打开软件"}, status=403)
                return False
            return True

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
                    self.send({"app": "paper-reader", "version": APP_VERSION, "schema_version": schema.SCHEMA_VERSION, "root": hashlib.sha256(str(ROOT).encode()).hexdigest()[:16], "data": hashlib.sha256(str(store.directory).encode()).hexdigest()[:16]})
                    return
                if path.startswith("/api/") and not self.authorized():
                    return
                if path == "/api/state":
                    self.send(store.snapshot())
                elif path == "/api/backup":
                    self.send(store.backup(), "application/zip", filename="论文研读备份-" + datetime.now().strftime("%Y%m%d") + ".zip")
                elif path == "/api/ai-settings":
                    self.send({"ai": store.ai_settings(), "providers": list(schema.PROVIDERS)})
                elif path == "/api/analysis":
                    health = None
                    if manager is not None:
                        ok, adapter, checked = manager.provider_ready()
                        health = dict(checked.as_dict(), provider=adapter.name, ready=ok)
                    self.send({"statuses": manager.statuses() if manager else {},
                               "queue": manager.queue_depth() if manager else {},
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
                    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
                    self.send(html.replace("__BOOTSTRAP__", json.dumps({"token": token, "version": APP_VERSION})), "text/html; charset=utf-8")
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
                length = int(self.headers.get("Content-Length", "0"))
                limit = 200 * 1024 * 1024 if path == "/api/restore" else MAX_PDF
                if length <= 0 or length > limit:
                    self.send({"error": "请求为空或文件过大"}, status=413)
                    return
                if path in ("/api/import", "/api/restore"):
                    if not self.authorized():
                        return
                    data = self.rfile.read(length)
                    if path == "/api/import":
                        filename = unquote(self.headers.get("X-File-Name", "paper.pdf"))
                        paper = store.import_pdf(data, filename)
                        # Record the job and return: the worker picks it up, the request
                        # never waits for a model.
                        if manager is not None:
                            try:
                                outcome = manager.enqueue(paper["id"], reason="import")
                                self.log_enqueue(outcome)
                            except Exception as exc:                  # noqa: BLE001 - import must succeed
                                self.log_enqueue({"error": f"{type(exc).__name__}: {exc}"})
                        self.send({"paper": copy.deepcopy(store.paper(paper["id"]))})
                    else:
                        self.send({"imported": store.restore(data), "state": store.snapshot()})
                    return
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                if not self.authorized(body):
                    return
                if path == "/api/save":
                    self.send(store.save_paper(body))
                elif path == "/api/settings":
                    # Obsidian sync plus the AI connection settings, in one save.
                    # A key value is never accepted and never echoed: only the *name* of
                    # the environment variable that holds it.
                    if "api_key" in body or "api_key_value" in body:
                        raise ValueError("不要把密钥写进设置；请填写保存密钥的环境变量名")
                    settings = store.configure(body)
                    if any(field in body for field in ("provider", "base_url", "model",
                                                       "api_key_env", "auto_analyze",
                                                       "max_concurrent_jobs")):
                        store.update_ai_settings({field: body[field] for field in
                                                  ("provider", "base_url", "model", "api_key_env",
                                                   "auto_analyze", "max_concurrent_jobs")
                                                  if field in body})
                        # configure() ran before the AI block was written, so read the
                        # settings again to return what is actually stored now.
                        settings = copy.deepcopy(store.state["settings"])
                    result = {"settings": settings, "ai": settings["ai"],
                              "providers": list(schema.PROVIDERS)}
                    if manager is not None:
                        result["resumed"] = manager.resume_waiting()
                        ok, adapter, checked = manager.provider_ready(result["ai"])
                        result["health"] = dict(checked.as_dict(), provider=adapter.name, ready=ok)
                    self.send(result)
                elif path == "/api/ai-settings":
                    # Data-layer settings only; no provider is contacted here.
                    updated = store.update_ai_settings(body.get("ai", body))
                    result = {"ai": updated}
                    if manager is not None:
                        # Papers that were waiting for a provider get queued as soon as one
                        # is configured; recovery is idempotent.
                        result["resumed"] = manager.resume_waiting()
                        ok, adapter, checked = manager.provider_ready(updated)
                        result["health"] = dict(checked.as_dict(), provider=adapter.name, ready=ok)
                    self.send(result)
                elif path.startswith("/api/analysis/") and path.endswith("/generate"):
                    # Retry (status failed/stale/...) or an explicit force re-run.
                    if manager is None:
                        raise ValueError("后台分析未启用")
                    paper_id = path[len("/api/analysis/"):-len("/generate")].strip("/")
                    store.paper(paper_id)
                    force = bool(body.get("force", False))
                    outcome = manager.enqueue(paper_id, reason="force" if force else "retry",
                                              force=force)
                    self.send({"run": outcome,
                               "status": analysis_status_payload(store, manager, paper_id)})
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
                    outcome = manager.enqueue(paper_id, reason="manual",
                                              force=bool(body.get("force", True)))
                    self.send({"run": outcome, "analysis": store.analysis_meta(paper_id)})
                elif path == "/api/analysis-cancel":
                    if manager is None:
                        raise ValueError("后台分析未启用")
                    paper_id = str(body.get("paper_id", ""))
                    store.paper(paper_id)
                    self.send({"cancelled": manager.cancel(paper_id),
                               "analysis": store.analysis_meta(paper_id)})
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
    """One JSON line per event. Only provider, model, timing and error category."""
    line = json.dumps(record, ensure_ascii=False)
    sys.stderr.write(providers.redact(line) + "\n")
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
        httpd = None
        for port in range(args.port, args.port + 30):
            try:
                httpd = ThreadingHTTPServer(("127.0.0.1", port), handler_for(store, token, manager))
                break
            except OSError:
                continue
        if httpd is None:
            show_error("无法启动本机服务：8765 起的 30 个端口都被占用。\n请关闭其它程序后重试。")
            return 5
        httpd.daemon_threads = True
        atomic_write(ROOT / "run" / "server.json",
                     json.dumps({"port": httpd.server_port, "pid": os.getpid()}))
        url = f"http://127.0.0.1:{httpd.server_port}"
        if args.open:
            webbrowser.open(url)
        print(url, flush=True)
        try:
            httpd.serve_forever()
        finally:
            httpd.server_close()
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
