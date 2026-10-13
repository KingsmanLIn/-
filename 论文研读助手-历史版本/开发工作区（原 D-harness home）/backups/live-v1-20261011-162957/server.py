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
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse
import uuid
import webbrowser
import zipfile

ROOT = Path(__file__).resolve().parent
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


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def clean_name(text, limit=72):
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f\[\]#]', "_", str(text)).strip(" ._")
    return (text[:limit].rstrip(" .") or "未命名论文")


def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + secrets.token_hex(5) + ".tmp")
    try:
        if isinstance(content, bytes):
            temp.write_bytes(content)
        else:
            temp.write_text(content, encoding="utf-8", newline="\n")
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


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
        self.path = self.directory / "library.json"
        self.pdf_dir = self.directory / "papers"
        self.lock = threading.RLock()
        self.pdf_dir.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self.state = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            self.state = {"version": 1, "settings": {"vault_path": "", "sync_enabled": False, "last_sync": "", "sync_error": ""}, "papers": []}
            self.persist()
        if self.state.get("version") != 1:
            raise ValueError("当前版本无法读取这个数据库，请先保留备份")
        for p in self.state["papers"]:
            validate_paper(p)

    def persist(self):
        if self.path.exists():
            atomic_write(self.directory / "library.previous.json", self.path.read_bytes())
        atomic_write(self.path, json.dumps(self.state, ensure_ascii=False, indent=2))

    def paper(self, paper_id):
        if not ID_RE.fullmatch(paper_id):
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
            editable = {"title", "authors", "year", "venue", "doi", "tags", "citation", "type", "language", "fields", "steps", "annotations", "pageOffset", "pageLabels", "lastPage", "archived"}
            for key, value in body.get("patch", {}).items():
                if key in editable:
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
        paper = {"id": paper_id, "title": title, "authors": "", "year": "", "venue": "", "doi": "", "citation": "", "tags": "",
                 "language": "zh" if re.search(r"[\u4e00-\u9fff]", title) else "en", "type": "empirical", "fields": {k: "" for k in FIELDS},
                 "steps": [False] * 4, "annotations": [], "pageOffset": 0, "pageLabels": [], "lastPage": 1, "revision": 0,
                 "created": now(), "updated": now(), "archived": False, "note_name": clean_name(title) + "--" + paper_id[:8] + ".md"}
        with self.lock:
            atomic_write(self.pdf_dir / f"{paper_id}.pdf", data)
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
                # Backup restores local records; it never re-enables writes to an old vault.
                state["settings"].update(sync_enabled=False, vault_path="", last_sync="", sync_error="")
                z.writestr("library.json", json.dumps(state, ensure_ascii=False, indent=2))
                for p in state["papers"]:
                    z.write(self.pdf_dir / f"{p['id']}.pdf", f"papers/{p['id']}.pdf")
                    z.writestr(f"notes/{p['note_name']}", markdown(p, f"[[../papers/{p['id']}.pdf|论文原文]]"))
            return content.getvalue()

    def restore(self, data):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            if sum(x.file_size for x in z.infolist()) > 500 * 1024 * 1024:
                raise ValueError("备份解压大小超过 500 MB")
            incoming = json.loads(z.read("library.json").decode("utf-8"))
            if incoming.get("version") != 1 or not isinstance(incoming.get("papers"), list):
                raise ValueError("这个文件不是当前版本的备份")
            staged = []
            for p in incoming["papers"]:
                validate_paper(p)
                blob = z.read(f"papers/{p['id']}.pdf")
                if len(blob) > MAX_PDF or b"%PDF-" not in blob[:1024]:
                    raise ValueError("备份中的 PDF 无效或过大")
                staged.append((copy.deepcopy(p), blob))
        with self.lock:
            existing = {p["id"] for p in self.state["papers"]}
            count = 0
            for p, blob in staged:
                if p["id"] in existing:
                    continue
                p["note_name"] = clean_name(p["title"]) + "--" + p["id"][:8] + ".md"
                atomic_write(self.pdf_dir / f"{p['id']}.pdf", blob)
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


def handler_for(store, token):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
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

        def do_GET(self):
            if not self.check_host():
                return
            path = unquote(urlparse(self.path).path)
            try:
                if path == "/api/health":
                    self.send({"app": "paper-reader", "version": "1.0.0", "root": hashlib.sha256(str(ROOT).encode()).hexdigest()[:16], "data": hashlib.sha256(str(store.directory).encode()).hexdigest()[:16]})
                    return
                if path.startswith("/api/") and not self.authorized():
                    return
                if path == "/api/state":
                    self.send(store.snapshot())
                elif path == "/api/backup":
                    self.send(store.backup(), "application/zip", filename="论文研读备份-" + datetime.now().strftime("%Y%m%d") + ".zip")
                elif path.startswith("/api/pdf/"):
                    p = store.paper(path.removeprefix("/api/pdf/"))
                    self.send((store.pdf_dir / f"{p['id']}.pdf").read_bytes(), "application/pdf", filename=clean_name(p['title'], 120) + ".pdf")
                elif path.startswith("/api/export/"):
                    p = copy.deepcopy(store.paper(path.removeprefix("/api/export/")))
                    self.send(markdown(p, f"[[{clean_name(p['title'], 120)}.pdf|论文原文]]"), "text/markdown; charset=utf-8", filename=p["note_name"])
                elif path in ("/", "/index.html"):
                    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
                    self.send(html.replace("__BOOTSTRAP__", json.dumps({"token": token, "version": "1.0.0"})), "text/html; charset=utf-8")
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
                        self.send({"paper": store.import_pdf(data, filename)})
                    else:
                        self.send({"imported": store.restore(data), "state": store.snapshot()})
                    return
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                if not self.authorized(body):
                    return
                if path == "/api/save":
                    self.send(store.save_paper(body))
                elif path == "/api/settings":
                    self.send({"settings": store.configure(body)})
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", default=str(ROOT / "data"))
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args()
    # A second launch must not create a second writer for the same local library.
    directory = Path(args.data_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    instance_lock = (directory / "instance.lock").open("a+b")
    instance_lock.seek(0)
    if not instance_lock.read(1):
        instance_lock.write(b"0")
        instance_lock.flush()
    instance_lock.seek(0)
    if os.name == "nt":
        import msvcrt
        try:
            msvcrt.locking(instance_lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            instance_lock.close()
            raise OSError("此文献库已经打开，请使用启动文件重新进入")
    store = Store(args.data_dir)
    token = secrets.token_urlsafe(32)
    httpd = None
    for port in range(args.port, args.port + 30):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", port), handler_for(store, token))
            break
        except OSError:
            continue
    if httpd is None:
        raise OSError("无法启动本机服务，请关闭旧窗口后重试")
    httpd.daemon_threads = True
    atomic_write(ROOT / "run" / "server.json", json.dumps({"port": httpd.server_port, "pid": os.getpid()}))
    url = f"http://127.0.0.1:{httpd.server_port}"
    if args.open:
        webbrowser.open(url)
    print(url, flush=True)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
        instance_lock.close()


if __name__ == "__main__":
    main()
