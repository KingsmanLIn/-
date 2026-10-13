"""Paper text input layer: extraction, cleaning, chunking and the on-disk cache.

Public interface
----------------
    extractor = PdfTextExtractor(files=DataStore(data_dir))     # files optional
    result = extractor.extract(pdf_path)                        # -> ExtractionResult
    chunks = extractor.chunks(result)                           # -> list[Chunk]

`result.pages[i]` is ``{"pdf_page": 1, "text": "...", "char_count": 12, ...}`` with
physical, 1-based PDF page numbers. Extraction results are cached under
``data/extracted/<paper_id>.json`` (or ``<sha256>.json`` without a paper id) and a
cache hit is only accepted after checking the source hash, the extractor version and
the page count.

Nothing here calls a model, and a failure never blocks manual reading: every failure
comes back as a code plus an actionable message.
"""
from __future__ import annotations

import time
from pathlib import Path

import chunker as chunker_module
import pdftext
import textclean
from pdftext import (BACKEND_ORDER, EXTRACTOR_VERSION, ExtractionError, ExtractionResult,
                     Page, file_sha256)
from storage import DataStore

CACHE_SCHEMA = 1
DEFAULT_MAX_CHARS = 6000
DEFAULT_OVERLAP_RATIO = 0.1


class PdfTextExtractor:
    """Extract PDF text with backend selection, caching and conservative cleaning."""

    def __init__(self, files=None, prefer=None, allow_cache=True, timeout_note=True):
        self.files = files or DataStore(Path("data"))
        self.prefer = [name for name in (prefer or BACKEND_ORDER)]
        self.allow_cache = allow_cache
        self._backends = {
            "pdftotext": pdftext.PdftotextBackend(),
            "builtin": pdftext.BuiltinBackend(),
            "pypdf": pdftext.PypdfBackend(),
        }
        self.last_diagnostics = {}

    # -- backends ----------------------------------------------------------
    def available(self):
        return [name for name in self.prefer if self._backends[name].available()]

    def backend_name(self):
        available = self.available()
        return available[0] if available else ""

    def backend_version(self, name):
        backend = self._backends.get(name)
        return backend.version() if backend else ""

    # -- cache -------------------------------------------------------------
    def cache_key(self, pdf_path, paper_id=None):
        if paper_id:
            return paper_id
        return file_sha256(pdf_path)[:32]

    def _read_cache(self, key, digest, page_count, backend, version):
        payload, error = self.files.read_extracted(key)
        if payload is None:
            return None, error
        if payload.get("schema") != CACHE_SCHEMA:
            return None, f"缓存 schema 不符（{payload.get('schema')}）"
        if payload.get("source_sha256") != digest:
            return None, "缓存的 PDF 指纹与当前文件不一致"
        extractor = payload.get("extractor") or {}
        if extractor.get("version") != version:
            return None, f"抽取器版本已变化（{extractor.get('version')} → {version}）"
        cached_pages = payload.get("pages") or []
        if page_count is not None and len(cached_pages) != page_count:
            return None, f"缓存页数不符（{len(cached_pages)} ≠ {page_count}）"
        if int(payload.get("page_count", -1)) != len(cached_pages):
            return None, f"缓存自述页数不符（{payload.get('page_count')} ≠ {len(cached_pages)}）"
        for position, page in enumerate(cached_pages, start=1):
            if int(page.get("pdf_page", -1)) != position:
                return None, f"缓存第 {position} 条记录的物理页码不是 {position}"
        return payload, None

    def _write_cache(self, key, result):
        payload = result.as_cache_payload(time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z")
        try:
            self.files.write_extracted(key, payload)
            return None
        except (OSError, ValueError) as exc:
            return str(exc)

    def drop_cache(self, pdf_path, paper_id=None):
        key = self.cache_key(pdf_path, paper_id)
        try:
            path = self.files.extracted_path(key)
        except ValueError:
            return False
        if path.is_file():
            path.unlink()
            return True
        return False

    # -- extraction --------------------------------------------------------
    def extract(self, path, paper_id=None, use_cache=None):
        """Extract text. Returns an ExtractionResult; never raises for PDF problems."""
        path = Path(path)
        use_cache = self.allow_cache if use_cache is None else use_cache
        if not path.is_file():
            return self._failure(path, "missing_file", "PDF 文件不存在",
                                 "确认 data/papers 中的文件是否被移动或删除")
        try:
            digest = file_sha256(path)
        except OSError as exc:
            return self._failure(path, "unreadable", f"无法读取 PDF：{exc}", "检查文件权限")

        key = self.cache_key(path, paper_id)
        backend_name = self.backend_name()
        version = self.backend_version(backend_name) or f"none-{EXTRACTOR_VERSION}"
        # pdfinfo gives a cheap authoritative page count, so a PDF whose page count
        # changed can never be served from an older cache entry.
        expected_pages = pdftext.PdftotextBackend.page_count(path)
        cache_note = ""
        if use_cache:
            payload, note = self._read_cache(key, digest, expected_pages, backend_name, version)
            if payload is not None:
                result = ExtractionResult.from_cache_payload(payload, path)
                self.last_diagnostics = {"cache": "hit", "key": key, "backend": result.backend}
                return result
            cache_note = note or ""
        if not backend_name:
            return self._failure(path, "no_backend", "本机没有可用的 PDF 文本抽取后端",
                                 "安装 Poppler(pdftotext) 或 Python 包 pypdf，仍可人工阅读")
        try:
            pages, backend_used, warnings = self._run_backends(path)
        except ExtractionError as exc:
            result = self._failure(path, exc.code, exc.message, exc.action)
            result.source_sha256 = digest
            return result
        except Exception as exc:                                  # noqa: BLE001 - reported, not raised
            return self._failure(path, "error", f"抽取失败：{type(exc).__name__}: {exc}",
                                 "保留原文，改用人工阅读；必要时把 PDF 另存为新文件后重试")

        code, message = self._diagnose(pages)
        result = ExtractionResult(path=str(path), source_sha256=digest, backend=backend_used,
                                  extractor_version=self.backend_version(backend_used) or EXTRACTOR_VERSION,
                                  page_count=len(pages), pages=pages, code=code, message=message,
                                  warnings=list(warnings))
        if code == "no_text_layer":
            # Do not hand unreadable text to later stages: a pseudo text layer must not
            # become evidence. The PDF itself is untouched and manual reading continues.
            for page in result.pages:
                if page.text:
                    page.warnings = list(page.warnings) + ["文本层不可读，已按无文本处理"]
                    page.text = ""
                    page.char_count = 0
                    page.has_text = False
        write_note = self._write_cache(key, result) if use_cache else None
        self.last_diagnostics = {"cache": "miss", "key": key, "backend": backend_used,
                                 "note": cache_note, "write": write_note or "ok"}
        return result

    def _run_backends(self, path):
        """Try the preferred backends; keep the first that yields usable text."""
        warnings = []
        best = None
        for name in self.available():
            backend = self._backends[name]
            try:
                if name == "pdftotext":
                    raw, stderr = backend.extract(path)
                    pages = self._pages_from_formfeed(raw)
                    if stderr:
                        warnings.append(f"pdftotext 提示：{stderr[:160]}")
                else:
                    pages = backend.extract(path)
            except ExtractionError as exc:
                if exc.code in ("encrypted_pdf", "broken_pdf"):
                    raise
                warnings.append(f"{name} 不可用：{exc.message}")
                continue
            except Exception as exc:                              # noqa: BLE001 - try the next backend
                warnings.append(f"{name} 失败：{type(exc).__name__}: {exc}")
                continue
            usable = sum(1 for page in pages if page.text.strip())
            score = (usable, sum(page.char_count for page in pages))
            if best is None or score > best[0]:
                best = (score, pages, name)
            if usable == len(pages) and pages:
                break
        if best is None:
            raise ExtractionError("no_backend", "所有文本抽取后端都失败了",
                                  "安装 pdftotext 或 pypdf 后重试，或只做人工阅读")
        return best[1], best[2], warnings

    @staticmethod
    def _pages_from_formfeed(raw):
        """pdftotext separates pages with a form feed; one feed per page boundary."""
        parts = raw.split("\f")
        if parts and not parts[-1].strip():
            parts = parts[:-1]
        pages = []
        for index, text in enumerate(parts, start=1):
            text = text.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
            pages.append(Page(pdf_page=index, text=text, char_count=len(text),
                              has_text=len(text.strip()) >= pdftext.MIN_USEFUL_CHARS))
        return pages

    @staticmethod
    def _diagnose(pages):
        """ok / no_text_layer plus an actionable message."""
        if not pages:
            return "no_text_layer", "这份 PDF 没有可读取的页面"
        usable = [page for page in pages if not pdftext.is_junk_text(page.text)]
        if not usable:
            images = sum(1 for page in pages if page.has_images)
            punctuation = sum(1 for page in pages if page.text.strip())
            if images:
                return ("no_text_layer",
                        f"{len(pages)} 页都是图像，没有可用文本层（扫描件）。可人工阅读，本工具不含 OCR。")
            if punctuation:
                return ("no_text_layer",
                        f"{len(pages)} 页只有稀疏的标点或空白，没有可用文本层（扫描件的伪文本层）。"
                        f"可人工阅读，本工具不含 OCR。")
            return "no_text_layer", f"{len(pages)} 页都没有可用文本层，无法抽取文字。"
        thin = [page.pdf_page for page in pages
                if page.text.strip() and pdftext.is_junk_text(page.text)]
        if thin:
            return ("ok", f"已抽取 {len(usable)}/{len(pages)} 页可用文本；第 "
                          f"{'、'.join(str(n) for n in thin[:5])} 页文字过少，可能没有文本层。")
        return "ok", f"已抽取 {len(pages)} 页文本。"

    def _failure(self, path, code, message, action=""):
        self.last_diagnostics = {"cache": "skip", "backend": None, "code": code}
        return ExtractionResult(path=str(path), source_sha256="", backend="",
                                extractor_version=EXTRACTOR_VERSION, page_count=0, pages=[],
                                code=code, message=message,
                                warnings=[action] if action else [])

    # -- cleaning ----------------------------------------------------------
    @staticmethod
    def clean(result, join_hyphens=False, drop_headers=True):
        """Return (pages, stats). Raw text stays available on `result.pages`."""
        pages = [{"pdf_page": page.pdf_page, "text": page.text, "char_count": page.char_count}
                 for page in result.pages]
        return textclean.clean_pages(pages, join_hyphens=join_hyphens, drop_headers=drop_headers)

    # -- chunking ----------------------------------------------------------
    @staticmethod
    def chunks(result, max_chars=DEFAULT_MAX_CHARS, overlap_ratio=DEFAULT_OVERLAP_RATIO,
               cleaned=False, document_id=""):
        pages = result.pages
        if cleaned:
            pages, _stats = PdfTextExtractor.clean(result)
        return chunker_module.Chunker(max_chars=max_chars,
                                      overlap_ratio=overlap_ratio).chunk(pages, document_id)


def extract_paper(files, paper_id, pdf_path, **kwargs):
    """Convenience wrapper for paper ids: extraction + cache keyed by the paper id."""
    extractor = IsolatedPdfTextExtractor(files=files, **kwargs)
    return extractor.extract(Path(pdf_path), paper_id=paper_id)


class IsolatedPdfTextExtractor(PdfTextExtractor):
    """All untrusted parsing, backend probes and cache verification run in a child.

    Cache is regenerated after resource isolation, so a corrupt local cache cannot
    supply fabricated text to model evidence verification.
    """
    def extract(self, path, paper_id=None, use_cache=None):
        import pdf_worker
        payload, info, _ = pdf_worker.extract_in_subprocess(path, paper_id or '')
        if payload is None:
            return self._failure(path, info.get('code', 'extract_failed'),
                                 'PDF text extraction unavailable; manual reading is available')
        result = ExtractionResult.from_cache_payload(payload, path)
        result.cached = False
        if self.allow_cache if use_cache is None else use_cache:
            self._write_cache(self.cache_key(path, paper_id), result)
        return result
