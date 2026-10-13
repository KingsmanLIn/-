"""Safe file storage for the reading library.

Everything that touches disk for records, analysis answers and extracted text
goes through here:

* every write is temp-file + fsync + os.replace, so an interrupted write leaves
  the previous file intact;
* every path is checked against its intended directory before it is opened, so a
  crafted id or file name cannot escape `data/`;
* everything written or read is UTF-8 JSON with an explicit size limit.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import time
from pathlib import Path

MAX_JSON_BYTES = 8 * 1024 * 1024          # a single record / answer file
MAX_EXTRACTED_BYTES = 64 * 1024 * 1024    # extracted text cache for one PDF
SAFE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{7,63}$")      # paper ids, mirrors server.ID_RE
SAFE_FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,110}\.json$")
SAFE_DIRNAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_TRAVERSAL = ("..", "/", "\\", "\x00")


def atomic_write_bytes(path, data):
    """Replace `path` with `data` atomically. An interrupted call keeps the old file.

    `os.replace` is retried briefly: on Windows a concurrent reader, a backup tool or
    an editor holding the target open fails the swap with a sharing violation, and a
    retry is enough to keep both writers consistent instead of losing a write.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + secrets.token_hex(5) + ".tmp")
    handle = None
    try:
        handle = open(temp, "wb")
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        handle = None
        last = None
        for attempt in range(6):
            try:
                os.replace(temp, path)
                return
            except PermissionError as exc:      # WinError 5: transient sharing violation
                last = exc
                time.sleep(0.02 * (attempt + 1))
        raise last
    finally:
        if handle is not None:
            handle.close()
        if temp.exists():
            try:
                temp.unlink()
            except OSError:
                pass


def atomic_write_text(path, text):
    atomic_write_bytes(path, str(text).encode("utf-8"))


def atomic_write_json(path, payload, indent=2):
    """Serialise to UTF-8 JSON and replace atomically."""
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=indent))


def read_json(path, limit=MAX_JSON_BYTES, default=None):
    """Read a JSON file.

    Returns (payload, None) on success and (default, message) when the file is
    missing, unreadable, oversized or not valid JSON. A broken file is never
    deleted and never partially applied.
    """
    path = Path(path)
    try:
        if not path.is_file():
            return default, "文件不存在"
        size = path.stat().st_size
        if size > limit:
            return default, f"文件超过大小上限（{size} > {limit} 字节）"
        return json.loads(path.read_text(encoding="utf-8")), None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return default, f"{type(exc).__name__}: {exc}"


def read_json_from_bytes(blob, limit=MAX_JSON_BYTES, default=None):
    """Same contract as read_json, for content coming out of a backup archive."""
    if blob is None:
        return default, "内容不存在"
    if len(blob) > limit:
        return default, f"内容超过大小上限（{len(blob)} > {limit} 字节）"
    try:
        return json.loads(blob.decode("utf-8")), None
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return default, f"{type(exc).__name__}: {exc}"


def check_name(name, pattern, what):
    """Reject empty, traversal-shaped or pattern-violating names."""
    if not isinstance(name, str) or not name:
        raise ValueError(f"{what}不能为空")
    if any(bad in name for bad in _TRAVERSAL):
        raise ValueError(f"{what}含有非法字符")
    if len(name) > 120:
        raise ValueError(f"{what}过长")
    if not pattern.match(name):
        raise ValueError(f"{what}格式无效")
    return name


def check_paper_id(paper_id):
    return check_name(paper_id, SAFE_ID_RE, "论文编号")


def check_file_name(name):
    return check_name(name, SAFE_FILE_RE, "文件名")


def check_dir_name(name):
    return check_name(name, SAFE_DIRNAME_RE, "目录名")


def ensure_within(path, root):
    """Resolve `path` and refuse anything outside `root` (symlinks included)."""
    root = Path(root).resolve()
    target = Path(path).resolve()
    if target != root and root not in target.parents:
        raise ValueError(f"路径越出数据目录：{target}")
    return target


class DataStore:
    """Owns the on-disk layout of one library directory.

    data/
      library.json           main library (schema version 2)
      library.previous.json  previous library (written on every persist)
      papers/<id>.pdf        source PDFs
      analyses/<id>.json     AI answers (schema.ANALYSIS_FILE_SCHEMA)
      analyses/<id>.failed.json  unvalidated model answers, never shown as an answer
      extracted/<id>.json    extracted text cache (schema.EXTRACTED_FILE_SCHEMA)
    """

    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.library = self.directory / "library.json"
        self.previous = self.directory / "library.previous.json"
        self.papers = self.directory / "papers"
        self.analyses = self.directory / "analyses"
        self.extracted = self.directory / "extracted"
        self.credentials = self.directory / "credentials.json"

    def ensure_directories(self):
        for path in (self.directory, self.papers, self.analyses, self.extracted):
            path.mkdir(parents=True, exist_ok=True)
        return self

    # --- record files -----------------------------------------------------
    def pdf_path(self, paper_id):
        return ensure_within(self.papers / f"{check_paper_id(paper_id)}.pdf", self.papers)

    def has_pdf(self, paper_id):
        try:
            return self.pdf_path(paper_id).is_file()
        except ValueError:
            return False

    def analysis_path(self, paper_id):
        return ensure_within(self.analyses / f"{check_paper_id(paper_id)}.json", self.analyses)

    def extracted_path(self, paper_id):
        return ensure_within(self.extracted / f"{check_paper_id(paper_id)}.json", self.extracted)

    def write_library(self, state):
        atomic_write_json(self.library, state)

    def read_library(self):
        return read_json(self.library)

    def backup_library(self):
        """Keep the previous library next to the new one before replacing it."""
        if self.library.is_file():
            atomic_write_bytes(self.previous, self.library.read_bytes())

    # --- analysis answers -------------------------------------------------
    def write_analysis(self, paper_id, payload, limit=MAX_JSON_BYTES):
        target = self.analysis_path(paper_id)
        blob = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        if len(blob) > limit:
            raise ValueError(f"分析文件超过大小上限（{len(blob)} > {limit} 字节）")
        atomic_write_bytes(target, blob)
        return target.name

    def failed_answer_path(self, paper_id):
        """Where an unvalidated model answer is logged (never shown as an answer)."""
        return ensure_within(self.analyses / f"{check_paper_id(paper_id)}.failed.json", self.analyses)

    def write_failed_answer(self, paper_id, payload, limit=MAX_JSON_BYTES):
        target = self.failed_answer_path(paper_id)
        blob = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        if len(blob) > limit:
            raise ValueError(f"失败日志超过大小上限（{len(blob)} > {limit} 字节）")
        atomic_write_bytes(target, blob)
        return target.name

    def has_failed_answer(self, paper_id):
        try:
            return self.failed_answer_path(paper_id).is_file()
        except ValueError:
            return False

    def delete_failed_answer(self, paper_id):
        try:
            path = self.failed_answer_path(paper_id)
        except ValueError:
            return False
        if path.is_file():
            path.unlink()
            return True
        return False

    def read_analysis(self, paper_id):
        return read_json(self.analysis_path(paper_id))

    def has_analysis(self, paper_id):
        try:
            return self.analysis_path(paper_id).is_file()
        except ValueError:
            return False

    def delete_analysis(self, paper_id):
        try:
            path = self.analysis_path(paper_id)
        except ValueError:
            return False
        if path.is_file():
            path.unlink()
            return True
        return False

    # --- extracted text cache --------------------------------------------
    def write_extracted(self, paper_id, payload, limit=MAX_EXTRACTED_BYTES):
        target = self.extracted_path(paper_id)
        blob = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if len(blob) > limit:
            raise ValueError(f"抽取缓存超过大小上限（{len(blob)} > {limit} 字节）")
        atomic_write_bytes(target, blob)
        return target.name

    def read_extracted(self, paper_id):
        return read_json(self.extracted_path(paper_id), limit=MAX_EXTRACTED_BYTES)

    def has_extracted(self, paper_id):
        try:
            return self.extracted_path(paper_id).is_file()
        except ValueError:
            return False

    def delete_extracted(self, paper_id):
        try:
            path = self.extracted_path(paper_id)
        except ValueError:
            return False
        if path.is_file():
            path.unlink()
            return True
        return False

    # --- whole paper ------------------------------------------------------
    def delete_paper_files(self, paper_id):
        """Remove every file that belongs to one paper id.

        Covers the source PDF, the AI answer, the failed-answer log and the extracted text
        cache. The id is validated by the four path helpers first, so nothing outside `data/`
        can be named here; the containment guard inside each helper is kept.

        Idempotent on purpose: a file that is already gone is reported as `False` rather than
        raising, so a repeated delete reports the same shape instead of failing.

        Returns a small dict describing what was removed, e.g.
        {"pdf": True, "analysis": False, "failed_answer": False, "extracted": True}.
        """
        # Validated once up front: the individual helpers repeat the check, and an invalid id
        # must be refused as a whole rather than half-way through the removals.
        check_paper_id(paper_id)
        pdf = self.pdf_path(paper_id)
        pdf_deleted = False
        if pdf.is_file():
            pdf.unlink()
            pdf_deleted = True
        removed = {"pdf": pdf_deleted,
                   "analysis": self.delete_analysis(paper_id),
                   "failed_answer": self.delete_failed_answer(paper_id),
                   "extracted": self.delete_extracted(paper_id)}
        return removed

    # --- credentials ------------------------------------------------------
    def read_credentials(self):
        """Removed in the security release: there is no plaintext credential store.

        The method is kept as a tripwire. Any caller that still asks for a plaintext
        credential gets an error instead of a value, so a forgotten call site cannot
        silently read a key out of a file that travels with the library.

        Returns (None, "plaintext_credentials_removed").
        """
        return None, "plaintext_credentials_removed"

    def legacy_credentials_present(self):
        """Report whether an old plaintext file exists, without reading a single byte."""
        try:
            return bool(self.credentials.is_file())
        except OSError:
            return False

    # --- bulk -------------------------------------------------------------
    def iter_analyses(self):
        """(paper_id, path) for every answer file, ids validated."""
        if not self.analyses.is_dir():
            return
        for path in sorted(self.analyses.glob("*.json")):
            paper_id = path.stem
            try:
                check_paper_id(paper_id)
            except ValueError:
                continue
            yield paper_id, path

    def iter_extracted(self):
        if not self.extracted.is_dir():
            return
        for path in sorted(self.extracted.glob("*.json")):
            paper_id = path.stem
            try:
                check_paper_id(paper_id)
            except ValueError:
                continue
            yield paper_id, path

    def mirror(self, destination):
        """Copy a pristine library + pdfs into `destination` (used by tests/restore drills)."""
        destination = Path(destination)
        (destination / "papers").mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.library, destination / "library.json")
        for pdf in sorted(self.papers.glob("*.pdf")):
            shutil.copy2(pdf, destination / "papers" / pdf.name)
        return destination
