"""Text extraction from PDFs with verifiable physical page numbers.

There is no PDF library in this environment, so the module ships three backends and
uses the best one that is actually available:

1. ``pdftotext`` - the reliable command shipped with TeX Live / Poppler
   (``-layout`` keeps reading order and table columns, form feeds separate pages);
2. ``builtin`` - a dependency-free extractor over the standard library
   (xref tables, xref streams, object streams, FlateDecode + PNG predictors,
   ToUnicode CMaps, WinAnsi/MacRoman, multi-column reordering);
3. ``pypdf`` - used only when it happens to be installed.

Nothing is invented: a page without a usable text layer produces a ``no_text_layer``
diagnosis instead of guessed characters, and manual reading keeps working.

    extractor = PdfTextExtractor()
    result = extractor.extract(path)
    result.pages[0]      # {"pdf_page": 1, "text": "...", "char_count": 12, ...}

``pdf_page`` is always the physical page, 1-based - never a printed page label.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import unicodedata
import zlib
from dataclasses import dataclass, field
from pathlib import Path

EXTRACTOR_VERSION = "2.1.0"      # bump when extraction behaviour changes
BACKEND_ORDER = ("pdftotext", "builtin", "pypdf")

#: Absolute path of the pdftotext command, handed to the isolated extraction child by its
#: parent. The child's PATH is deliberately trimmed to the interpreter directory and System32,
#: so `shutil.which` cannot find a user-installed pdftotext there; passing the resolved path is
#: what keeps the good backend available without widening the child's PATH.
PDFTOTEXT_ENV = "PAPER_READER_PDFTOTEXT"
MIN_USEFUL_CHARS = 20            # a page below this is treated as having no text layer
MIN_TEXT_QUALITY = 0.55          # below this a page is reported as having no usable layer
MAX_PAGE_OBJECTS = 200000
MAX_STREAM_BYTES = 16 * 1024 * 1024

def bounded_inflate(raw):
    decoder = zlib.decompressobj()
    expanded = decoder.decompress(raw, MAX_STREAM_BYTES + 1)
    if len(expanded) > MAX_STREAM_BYTES or decoder.unconsumed_tail:
        raise ExtractionError('extract_too_large', 'PDF stream exceeds extraction budget')
    return expanded



# ---------------------------------------------------------------------------
# data containers
# ---------------------------------------------------------------------------

@dataclass
class Page:
    pdf_page: int
    text: str
    char_count: int
    width: float | None = None
    height: float | None = None
    has_text: bool = True
    has_images: bool = False
    warnings: list = field(default_factory=list)


@dataclass
class ExtractionResult:
    path: str
    source_sha256: str
    backend: str
    extractor_version: str
    page_count: int
    pages: list
    code: str = "ok"          # ok | no_text_layer | broken_pdf | encrypted_pdf | error
    message: str = ""
    warnings: list = field(default_factory=list)
    cached: bool = False

    @property
    def ok(self):
        return self.code == "ok"

    @property
    def total_chars(self):
        return sum(page.char_count for page in self.pages)

    @property
    def text_pages(self):
        return [page for page in self.pages if page.text.strip()]

    def as_cache_payload(self, created_at):
        return {
            "schema": 1,
            "source_sha256": self.source_sha256,
            "extractor": {"name": self.backend, "version": self.extractor_version},
            "page_count": self.page_count,
            "created_at": created_at,
            "code": self.code,
            "message": self.message,
            "warnings": list(self.warnings),
            "pages": [{"pdf_page": page.pdf_page, "text": page.text,
                       "char_count": page.char_count, "width": page.width,
                       "height": page.height, "has_text": page.has_text,
                       "has_images": page.has_images,
                       "warnings": list(page.warnings)} for page in self.pages],
        }

    @classmethod
    def from_cache_payload(cls, payload, path):
        pages = []
        for item in payload.get("pages", []):
            text = str(item.get("text", ""))
            pages.append(Page(pdf_page=int(item["pdf_page"]), text=text,
                              char_count=int(item.get("char_count", len(text))),
                              width=item.get("width"), height=item.get("height"),
                              has_text=bool(item.get("has_text", True)),
                              has_images=bool(item.get("has_images", False)),
                              warnings=list(item.get("warnings", []))))
        extractor = payload.get("extractor", {}) or {}
        return cls(path=str(path), source_sha256=payload.get("source_sha256", ""),
                   backend=extractor.get("name", ""), extractor_version=extractor.get("version", ""),
                   page_count=int(payload.get("page_count", len(pages))), pages=pages,
                   code=payload.get("code", "ok"), message=payload.get("message", ""),
                   warnings=list(payload.get("warnings", [])), cached=True)


class ExtractionError(RuntimeError):
    """Raised when the file cannot be used at all."""

    def __init__(self, code, message, action=""):
        super().__init__(message)
        self.code, self.message, self.action = code, message, action

    def as_dict(self):
        return {"code": self.code, "message": self.message, "action": self.action}


def file_sha256(path, block_size=1024 * 1024):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(block_size), b""):
            digest.update(block)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# backend 1: pdftotext
# ---------------------------------------------------------------------------

class PdftotextBackend:
    name = "pdftotext"

    def __init__(self, executable=None, layout=True):
        # The isolated child runs with a trimmed PATH, so the parent resolves pdftotext and
        # hands the absolute path over in PDFTOTEXT_ENV (see pdf_worker.scrub_environment).
        # Without it the child never saw pdftotext and silently used the builtin extractor: on
        # a real 58-page paper that is 79 641 characters instead of 189 246, with diacritics
        # and ligatures mangled ("scar Jord", "Befln").
        self.executable = (executable or os.environ.get(PDFTOTEXT_ENV, "")
                           or shutil.which("pdftotext") or "")
        self.layout = layout
        self._version = None

    def available(self):
        return bool(self.executable)

    def version(self):
        if self._version is None:
            self._version = ""
            if self.available():
                try:
                    proc = subprocess.run([self.executable, "-v"], capture_output=True, text=True,
                                          encoding="utf-8", errors="replace", timeout=20)
                    lines = (proc.stderr or proc.stdout or "").strip().splitlines()
                    self._version = lines[0][:80] if lines else "pdftotext"
                except (OSError, subprocess.SubprocessError):
                    self._version = "pdftotext"
        return self._version

    @staticmethod
    def page_count(path):
        """Cheap authoritative page count via pdfinfo, or None when unavailable."""
        executable = shutil.which("pdfinfo")
        if not executable:
            return None
        try:
            proc = subprocess.run([executable, str(path)], capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=30)
        except (OSError, subprocess.SubprocessError):
            return None
        found = re.search(r"^Pages:\s+(\d+)", proc.stdout or "", re.M)
        return int(found.group(1)) if found else None

    def extract(self, path):
        if not self.available():
            raise ExtractionError("no_backend", "没有可用的 pdftotext 命令", "改用内置抽取器")
        args = [self.executable, "-enc", "UTF-8"]
        if self.layout:
            args.append("-layout")
        args += [str(path), "-"]
        try:
            proc = subprocess.run(args, capture_output=True, timeout=600)
        except subprocess.TimeoutExpired as exc:
            raise ExtractionError("timeout", "pdftotext 超时", "文件过大或损坏，可先拆分 PDF") from exc
        except OSError as exc:
            raise ExtractionError("no_backend", f"无法运行 pdftotext：{exc}", "检查命令是否在 PATH 中") from exc
        stderr = (proc.stderr or b"").decode("utf-8", "replace").strip()
        if proc.returncode not in (0, 1):
            lowered = stderr.lower()
            code = "encrypted_pdf" if ("password" in lowered or "encrypted" in lowered) else "broken_pdf"
            raise ExtractionError(code, f"pdftotext 失败：{stderr[:200] or proc.returncode}",
                                  "确认 PDF 未加密且完整，或改用人工阅读")
        return (proc.stdout or b"").decode("utf-8", "replace"), stderr


# ---------------------------------------------------------------------------
# backend 2: builtin (standard library only)
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(
    rb"(?P<num>[+-]?(?:\d+\.\d*|\.\d+|\d+))"
    rb"|(?P<hex><[0-9A-Fa-f\s]*>)"
    rb"|(?P<name>/[^\s/\[\]<>(){}]*)"
    rb"|(?P<lit>\((?:[^()\\]|\\.)*\))"
    rb"|(?P<open>\[|\]|<<|>>)"
    rb"|(?P<op>[A-Za-z'\"*][A-Za-z0-9*'\"]*)"
    rb"|(?P<other>\S)", re.S | re.X)

_TEXT_OPS = {b"Tj", b"TJ", b"'", b'"'}
_LINE_BREAK_OPS = {b"Td", b"TD", b"T*", b"'"}
_WIN_ANSI = {
    **{code: chr(code) for code in range(0x20, 0x7F)},
    0x80: "\u20ac", 0x82: "\u201a", 0x83: "\u0192", 0x84: "\u201e", 0x85: "\u2026",
    0x86: "\u2020", 0x87: "\u2021", 0x88: "\u02c6", 0x89: "\u2030", 0x8a: "\u0160",
    0x8b: "\u2039", 0x8c: "\u0152", 0x8e: "\u017d", 0x91: "\u2018", 0x92: "\u2019",
    0x93: "\u201c", 0x94: "\u201d", 0x95: "\u2022", 0x96: "\u2013", 0x97: "\u2014",
    0x98: "\u02dc", 0x99: "\u2122", 0x9a: "\u0161", 0x9b: "\u203a", 0x9c: "\u0153",
    0x9e: "\u017e", 0x9f: "\u0178", 0xa0: " ", 0xa9: "\u00a9", 0xae: "\u00ae",
    0xb0: "\u00b0", 0xb1: "\u00b1", 0xd7: "\u00d7", 0xf7: "\u00f7",
}
_MAC_ROMAN_HIGH = {
    0x80: "\u00c4", 0x81: "\u00c5", 0x82: "\u00c7", 0x83: "\u00c9", 0x84: "\u00d1",
    0x85: "\u00d6", 0x86: "\u00dc", 0x87: "\u00e1", 0x88: "\u00e0", 0x89: "\u00e2",
    0x8a: "\u00e4", 0x8b: "\u00e3", 0x8c: "\u00e5", 0x8d: "\u00e7", 0x8e: "\u00e9",
    0x8f: "\u00e8", 0x90: "\u00ea", 0x91: "\u00eb", 0x92: "\u00ed", 0x93: "\u00ec",
    0x94: "\u00ee", 0x95: "\u00ef", 0x96: "\u00f1", 0x97: "\u00f3", 0x98: "\u00f2",
    0x99: "\u00f4", 0x9a: "\u00f6", 0x9b: "\u00f5", 0x9c: "\u00fa", 0x9d: "\u00f9",
    0x9e: "\u00fb", 0x9f: "\u00fc", 0xa0: "\u2020", 0xa1: "\u00b0", 0xa2: "\u00a2",
    0xa3: "\u00a3", 0xa4: "\u00a7", 0xa5: "\u2022", 0xa6: "\u00b6", 0xa7: "\u00df",
    0xa8: "\u00ae", 0xa9: "\u00a9", 0xaa: "\u2122", 0xab: "\u00b4", 0xac: "\u00a8",
    0xad: "\u2260", 0xae: "\u00c6", 0xaf: "\u00d8", 0xb0: "\u221e", 0xb1: "\u00b1",
    0xb4: "\u00d7", 0xb5: "\u00f7", 0xbb: "\u00aa", 0xbc: "\u00ba", 0xbe: "\u00e6",
    0xbf: "\u00f8", 0xc0: "\u00bf", 0xc1: "\u00a1", 0xc2: "\u00ac", 0xc4: "\u0192",
    0xc5: "\u2248", 0xc6: "\u2206", 0xc7: "\u00ab", 0xc8: "\u00bb", 0xc9: "\u2026",
    0xca: "\u00a0", 0xcb: "\u00c0", 0xcc: "\u00c3", 0xcd: "\u00d5", 0xce: "\u0152",
    0xcf: "\u0153", 0xd0: "\u2013", 0xd1: "\u2014", 0xd2: "\u201c", 0xd3: "\u201d",
    0xd4: "\u2018", 0xd5: "\u2019", 0xd6: "\u00f7", 0xd8: "\u00ff", 0xd9: "\u0178",
}


GLYPH_NAMES = {
    "space": " ", "exclam": "!", "quotedbl": '"', "numbersign": "#", "dollar": "$",
    "percent": "%", "ampersand": "&", "quotesingle": "'", "quoteright": "\u2019",
    "quoteleft": "\u2018", "quotedblleft": "\u201c", "quotedblright": "\u201d",
    "parenleft": "(", "parenright": ")", "asterisk": "*", "plus": "+", "comma": ",",
    "hyphen": "-", "period": ".", "slash": "/", "zero": "0", "one": "1", "two": "2",
    "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7", "eight": "8",
    "nine": "9", "colon": ":", "semicolon": ";", "less": "<", "equal": "=", "greater": ">",
    "question": "?", "at": "@", "bracketleft": "[", "backslash": "\\", "bracketright": "]",
    "asciicircum": "^", "underscore": "_", "grave": "`", "braceleft": "{", "bar": "|",
    "braceright": "}", "asciitilde": "~", "endash": "\u2013", "emdash": "\u2014",
    "bullet": "\u2022", "dagger": "\u2020", "daggerdbl": "\u2021", "ellipsis": "\u2026",
    "perthousand": "\u2030", "guilsinglleft": "\u2039", "guilsinglright": "\u203a",
    "fi": "fi", "fl": "fl", "ff": "ff", "ffi": "ffi", "ffl": "ffl",
    "minus": "\u2212", "fraction": "/", "degree": "\u00b0", "plusminus": "\u00b1",
    "multiply": "\u00d7", "divide": "\u00f7", "notequal": "\u2260", "lessequal": "\u2264",
    "greaterequal": "\u2265", "infinity": "\u221e", "approxequal": "\u2248",
    "Delta": "\u0394", "delta": "\u03b4", "mu": "\u03bc", "alpha": "\u03b1",
    "beta": "\u03b2", "gamma": "\u03b3", "sigma": "\u03c3", "pi": "\u03c0",
    "Euro": "\u20ac", "sterling": "\u00a3", "yen": "\u00a5", "cent": "\u00a2",
    "section": "\u00a7", "paragraph": "\u00b6", "copyright": "\u00a9",
    "registered": "\u00ae", "trademark": "\u2122", "germandbls": "\u00df",
    "ae": "\u00e6", "oe": "\u0153", "oslash": "\u00f8", "aring": "\u00e5",
}
for _index in range(26):
    GLYPH_NAMES.setdefault(f"_{chr(65 + _index)}".lower(), "")     # placeholders, not used
del _index


def glyph_to_unicode(name):
    """Adobe glyph name -> text, following the AGL conventions."""
    if not name:
        return ""
    if name in GLYPH_NAMES:
        return GLYPH_NAMES[name]
    if len(name) == 1:
        return name
    if name.startswith("uni") and len(name) >= 7:
        try:
            return bytes.fromhex(name[3:7]).decode("utf-16-be", "replace")
        except ValueError:
            return ""
    if name.startswith("u") and 5 <= len(name) <= 7:
        try:
            return chr(int(name[1:], 16))
        except ValueError:
            return ""
    if "." in name:                     # suffixed variants such as "a.sc"
        return glyph_to_unicode(name.split(".", 1)[0])
    return ""


EXPECTED_SYMBOLS = set(
    " \t\n\r.,;:!?'\"()[]{}<>/\\|-–—_=+*&%#@$§¶†‡•…‰°±×÷≤≥≠≈∞€£¥¢©®™№−"
    "，。、；：？！“”‘’（）【】《》〈〉「」『』～·—…￥"
)
MIN_MEANINGFUL_CHARS = 40       # below this a page carries no usable text
MIN_TEXT_DENSITY = 0.02         # useful characters / total characters
MIN_JUNK_QUALITY = 0.60         # quality below this plus low density means junk


def unreadable_chars(text):
    """Meaningful-looking characters that are actually invalid (broken font mapping).

    A PDF whose ToUnicode CMap is wrong produces private-use or surrogate code points
    instead of real glyphs; those must never be treated as content.
    """
    bad = 0
    for char in text:
        category = unicodedata.category(char)
        if category[0] not in ("L", "N"):
            continue
        if category in ("Co", "Cs", "Cn") or unicodedata.name(char, "") == "":
            bad += 1
    return bad


def is_junk_text(text):
    """True when a text layer exists but carries almost no readable information.

    Scans sometimes ship a pseudo text layer: mostly spacing, stray glyphs or private
    use code points, so the raw character count looks large while the page holds
    nothing readable. Two independent signals are combined - the encoding quality
    score and the density of valid characters - and both must be low before a page is
    called unreadable, so ordinary sparse pages (cover pages, figure captions) with
    real words are never discarded.
    """
    if not text:
        return True
    valid = meaningful_chars(text) - unreadable_chars(text)
    if valid < MIN_MEANINGFUL_CHARS:
        return True
    density = valid / len(text)
    return density < MIN_TEXT_DENSITY and text_quality(text) < MIN_JUNK_QUALITY


def meaningful_chars(text):
    """Count characters that actually carry information (letters and digits).

    A scanned page sometimes ships a text layer that is nothing but spaces and
    punctuation; such a page must be reported as having no usable text instead of
    being treated as evidence. Table values, signs and units are preserved by the
    caller - this counter is only used to decide whether a page is worth reading.
    """
    return sum(1 for char in text if unicodedata.category(char)[0] in ("L", "N"))
DIGIT_OR_LETTER = ("L", "N")


def text_quality(text):
    """0..1 score for how much a page's text looks like real text.

    A page decoded through the wrong encoding is mostly unexpected symbols and
    Latin-1 punctuation, which scores far below real prose. The score is used to
    refuse garbled output instead of publishing it as evidence.
    """
    if not text:
        return 0.0
    total = good = symbols = controls = replacements = 0
    for char in text:
        total += 1
        category = unicodedata.category(char)
        if char == "\ufffd":
            replacements += 1
        elif category[0] in ("L", "N") or char.isspace():
            good += 1
        elif category[0] == "C":
            controls += 1
        elif char not in EXPECTED_SYMBOLS:
            symbols += 1
    return max(0.0, (good - symbols * 2 - controls * 5 - replacements * 5) / total)


def _utf16_be(hex_digits):
    digits = hex_digits.decode("latin-1") if isinstance(hex_digits, bytes) else hex_digits
    digits = re.sub(r"\s+", "", digits)
    if len(digits) % 4:
        digits = digits.zfill(len(digits) + (4 - len(digits) % 4))
    try:
        return bytes.fromhex(digits).decode("utf-16-be", "replace")
    except ValueError:
        return ""


def _unescape_literal(raw):
    """Decode a PDF literal string body (without the parentheses)."""
    out = bytearray()
    index = 0
    while index < len(raw):
        char = raw[index]
        if char != 0x5C:                     # backslash
            out.append(char)
            index += 1
            continue
        index += 1
        if index >= len(raw):
            break
        escape = raw[index]
        mapping = {0x6E: 0x0A, 0x72: 0x0D, 0x74: 0x09, 0x62: 0x08, 0x66: 0x0C,
                   0x28: 0x28, 0x29: 0x29, 0x5C: 0x5C}
        if escape in mapping:
            out.append(mapping[escape])
            index += 1
        elif 0x30 <= escape <= 0x37:         # octal, up to three digits
            digits = ""
            while index < len(raw) and len(digits) < 3 and 0x30 <= raw[index] <= 0x37:
                digits += chr(raw[index])
                index += 1
            out.append(int(digits, 8) & 0xFF)
        elif escape == 0x0A:
            index += 1
        else:
            out.append(escape)
            index += 1
    return bytes(out)


class BuiltinBackend:
    """Dependency-free PDF text extractor."""

    name = "builtin"

    def __init__(self):
        self._memo = {}

    def available(self):
        return True

    def version(self):
        return f"builtin-{EXTRACTOR_VERSION}"

    # -- object layer ------------------------------------------------------
    def _objects(self, blob):
        """number -> {"body": bytes, "stream": bytes|None}.

        Keyed by a content hash, not by id(blob): a recycled memory address would
        otherwise return another file's objects.
        """
        key = hashlib.sha256(blob).hexdigest()
        if key in self._memo:
            return self._memo[key]
        objects = {}
        queue = [int(m.group(1)) for m in re.finditer(rb"startxref\s+(\d+)", blob)]
        seen = set()
        while queue:
            offset = queue.pop(0)
            if offset in seen or not 0 <= offset < len(blob):
                continue
            seen.add(offset)
            if blob[offset:offset + 4] == b"xref":
                trailer = self._read_xref_table(blob, offset, objects)
            else:
                header = re.match(rb"\s*(\d+)\s+(\d+)\s+obj\b", blob[offset:offset + 64])
                if not header:
                    continue
                number = int(header.group(1))
                body, stream = self._read_indirect(blob, offset)
                objects.setdefault(number, {"body": body, "stream": stream})
                trailer = body
                head = body[:160]
                if b"/W [" in head or b"/W[" in head or b"/Type /XRef" in head or b"/Index" in head:
                    # cross-reference stream: it carries its own /Index and /Prev
                    previous = self._read_xref_stream(blob, body, stream, objects)
                    if previous:
                        queue.append(previous)
            for name in (b"/XRefStm", b"/Prev"):
                found = re.search(name + rb"\s+(\d+)", trailer)
                if found:
                    queue.append(int(found.group(1)))
        self._expand_object_streams(blob, objects)
        self._scan_missing_objects(blob, objects)
        self._memo[key] = objects
        return objects

    @staticmethod
    def _scan_missing_objects(blob, objects):
        """Fallback scan for indirect objects the xref chain did not reach.

        Only runs when traversal is clearly incomplete (far fewer objects than the
        declared /Size, or a catalog whose /Pages cannot be resolved), and only accepts
        object headers at the start of a line, so stream payloads rarely produce a false
        object. Without this a linearised file can end up with an unreachable page tree.
        """
        declared = 0
        for entry in objects.values():
            found = re.search(rb"/Size\s+(\d+)", entry["body"][:400])
            if found:
                declared = max(declared, int(found.group(1)))
        needs_scan = declared > len(objects) + 25
        if not needs_scan:
            catalogs = [n for n, e in objects.items() if b"/Catalog" in e["body"][:400]]
            needs_scan = bool(catalogs) and not any(
                re.search(rb"/Pages\s+\d+\s+\d+\s+R", objects[n]["body"][:600]) for n in catalogs)
        if not needs_scan:
            return
        for match in re.finditer(rb"(?:^|[\r\n])(\d{1,7})\s+(\d{1,5})\s+obj\b", blob):
            number = int(match.group(1))
            if number in objects:
                continue
            body, stream = BuiltinBackend._read_indirect(blob, match.start(1))
            if body.startswith(b"<<"):
                objects[number] = {"body": body, "stream": stream}
        BuiltinBackend._expand_object_streams_static(blob, objects)

    def _read_xref_stream(self, blob, body, stream, objects):
        """Decode a cross-reference stream (/W widths, /Index ranges).

        Returns the /Prev offset so the caller can follow the xref chain; without it a
        linearised file never reaches the object streams that hold most objects.
        """
        previous = re.search(rb"/Prev\s+(\d+)", body)
        previous = int(previous.group(1)) if previous else None
        widths = re.search(rb"/W\s*\[\s*(\d+)\s+(\d+)\s*(\d*)", body)
        if not widths:
            return previous
        widths = [int(widths.group(1)), int(widths.group(2)),
                  int(widths.group(3)) if widths.group(3) else 0]
        entry_size = sum(widths)
        if entry_size <= 0:
            return
        index = re.search(rb"/Index\s*\[\s*([\d\s]+)\]", body)
        if index:
            values = [int(x) for x in index.group(1).split()]
            pairs = list(zip(values[0::2], values[1::2]))
        else:
            size = re.search(rb"/Size\s+(\d+)", body)
            pairs = [(0, int(size.group(1)) if size else 0)]
        data = BuiltinBackend._decode_stream_static(body, stream)
        position = 0
        for first, count in pairs:
            for offset in range(count):
                if position + entry_size > len(data):
                    return previous
                fields = []
                for width in widths:
                    value = 0
                    for byte in data[position:position + width]:
                        value = (value << 8) | byte
                    fields.append(value)
                    position += width
                kind = fields[0] if widths[0] else 1
                number = first + offset
                if kind == 1 and fields[1] and number not in objects:
                    body_at, stream_at = self._read_indirect(blob, fields[1])
                    objects.setdefault(number, {"body": body_at, "stream": stream_at})
                # kind == 0 is a free entry, kind == 2 lives inside an object stream and is
                # added when that stream is expanded - nothing to record here.
        return previous

    @staticmethod
    def _decode_stream_static(body, raw):
        if raw is None:
            return b""
        if b"/FlateDecode" in body:
            try:
                raw = bounded_inflate(raw)
            except zlib.error:
                try:
                    raw = bounded_inflate(raw)
                except zlib.error:
                    return b""
        if b"/Predictor" in body:
            predictor = re.search(rb"/Predictor\s+(\d+)", body)
            if predictor and int(predictor.group(1)) >= 10:
                columns = re.search(rb"/Columns\s+(\d+)", body)
                colors = re.search(rb"/Colors\s+(\d+)", body)
                bits = re.search(rb"/BitsPerComponent\s+(\d+)", body)
                raw = BuiltinBackend._undo_png_predictor(
                    raw, int(columns.group(1)) if columns else 1,
                    int(colors.group(1)) if colors else 1,
                    int(bits.group(1)) if bits else 8)
        return raw

    @staticmethod
    def _read_indirect(blob, offset):
        dict_start = blob.find(b"<<", offset)
        if dict_start < 0:
            return b"", None
        body = BuiltinBackend._balanced_dict(blob, dict_start)
        marker = re.compile(rb"stream\r?\n").search(blob, dict_start, dict_start + len(body) + 4096)
        if not marker:
            return body, None
        start = marker.end()
        length = re.search(rb"/Length\s+(\d+)(?!\s+\d+\s+R)", body)
        if length:
            return body, blob[start:start + int(length.group(1))]
        end = blob.find(b"endstream", start)
        return body, blob[start:end if end > 0 else len(blob)]

    @staticmethod
    def _read_xref_table(blob, offset, objects):
        end = blob.find(b"trailer", offset)
        head = blob[offset:end if end > 0 else offset + 8192]
        for block in re.finditer(rb"(\d+)\s+(\d+)\s*\n((?:\d{10}\s+\d{5}\s+[nf]\s*(?:\r?\n|$))+)", head):
            first = int(block.group(1))
            for index, entry in enumerate(re.findall(rb"(\d{10})\s+(\d{5})\s+([nf])", block.group(3))):
                if entry[2] == b"n":
                    number = first + index
                    body, stream = BuiltinBackend._read_indirect(blob, int(entry[0]))
                    objects.setdefault(number, {"body": body, "stream": stream})
        trailer = b""
        if end > 0:
            trailer_start = blob.find(b"<<", end)
            trailer = BuiltinBackend._balanced_dict(blob, trailer_start) if trailer_start > 0 else b""
        return trailer

    @staticmethod
    def _balanced_dict(blob, at):
        if at < 0 or blob[at:at + 2] != b"<<":
            return b""
        depth, index = 0, at
        while index < len(blob) - 1:
            pair = blob[index:index + 2]
            if pair == b"<<":
                depth += 1
                index += 2
                continue
            if pair == b">>":
                depth -= 1
                index += 2
                if depth == 0:
                    return blob[at:index]
                continue
            index += 1
        return blob[at:at + 8192]

    def _expand_object_streams(self, blob, objects):
        return self._expand_object_streams_static(blob, objects)

    @staticmethod
    def _expand_object_streams_static(blob, objects):
        for number, entry in list(objects.items()):
            if b"/ObjStm" not in entry["body"][:300]:
                continue
            raw = BuiltinBackend.decode_entry(entry)
            if not raw:
                continue
            count = int((re.search(rb"/N\s+(\d+)", entry["body"]) or [None, b"0"])[1])
            first = int((re.search(rb"/First\s+(\d+)", entry["body"]) or [None, b"0"])[1])
            header = raw[:first].split()
            for index in range(count):
                try:
                    obj_number = int(header[2 * index])
                    start = int(header[2 * index + 1])
                    stop = int(header[2 * index + 3]) if 2 * index + 3 < len(header) else len(raw) - first
                except (IndexError, ValueError):
                    break
                segment = raw[first + start:first + stop]
                if segment.lstrip().startswith(b"<<"):
                    objects.setdefault(obj_number, {"body": BuiltinBackend._balanced_dict(segment, segment.find(b"<<")),
                                                    "stream": None})
                else:
                    objects.setdefault(obj_number, {"body": segment, "stream": None})

    def _decode(self, entry):
        return BuiltinBackend.decode_entry(entry)

    @staticmethod
    def decode_entry(entry):
        raw = entry.get("stream")
        if raw is None:
            return b""
        body = entry["body"]
        if b"/FlateDecode" in body:
            try:
                raw = bounded_inflate(raw)
            except zlib.error:
                try:
                    raw = bounded_inflate(raw)
                except zlib.error:
                    return b""
        if b"/Predictor" in body:
            predictor = re.search(rb"/Predictor\s+(\d+)", body)
            if predictor and int(predictor.group(1)) >= 10:
                columns = re.search(rb"/Columns\s+(\d+)", body)
                colors = re.search(rb"/Colors\s+(\d+)", body)
                bits = re.search(rb"/BitsPerComponent\s+(\d+)", body)
                raw = BuiltinBackend._undo_png_predictor(raw, int(columns.group(1)) if columns else 1,
                                               int(colors.group(1)) if colors else 1,
                                               int(bits.group(1)) if bits else 8)
        return raw

    @staticmethod
    def _undo_png_predictor(data, columns, colors, bits):
        bpp = max(1, colors * bits // 8)
        row_length = max(1, columns * colors * bits // 8)
        out = bytearray()
        previous = bytearray(row_length)
        position = 0
        while position + 1 + row_length <= len(data):
            filter_type = data[position]
            row = bytearray(data[position + 1:position + 1 + row_length])
            position += 1 + row_length
            for index in range(row_length):
                left = row[index - bpp] if index >= bpp else 0
                up = previous[index]
                up_left = previous[index - bpp] if index >= bpp else 0
                if filter_type == 1:
                    row[index] = (row[index] + left) & 0xFF
                elif filter_type == 2:
                    row[index] = (row[index] + up) & 0xFF
                elif filter_type == 3:
                    row[index] = (row[index] + ((left + up) >> 1)) & 0xFF
                elif filter_type == 4:
                    estimate = left + up - up_left
                    pa, pb, pc = abs(estimate - left), abs(estimate - up), abs(estimate - up_left)
                    predictor = left if (pa <= pb and pa <= pc) else (up if pb <= pc else up_left)
                    row[index] = (row[index] + predictor) & 0xFF
            out.extend(row)
            previous = row
        return bytes(out)

    @staticmethod
    def _entry(objects, reference):
        if not reference:
            return {"body": b"", "stream": None}
        return objects.get(reference, {"body": b"", "stream": None})

    @staticmethod
    def _ref(body, key):
        found = re.search(key + rb"\s+(\d+)\s+\d+\s+R", body)
        return int(found.group(1)) if found else None

    # -- page tree ---------------------------------------------------------
    def pages(self, blob):
        objects = self._objects(blob)
        root = None
        for number, entry in objects.items():
            if re.search(rb"/Type\s*/Catalog", entry["body"]):
                root = number
                break
        if root is None:
            raise ExtractionError("broken_pdf", "找不到 PDF 目录对象", "确认文件是否为完整 PDF")
        pages_root = self._ref(objects[root]["body"], rb"/Pages")
        if pages_root is None:
            raise ExtractionError("broken_pdf", "找不到 PDF 页面树", "确认文件是否为完整 PDF")
        order = []
        stack = [pages_root]
        visited = set()
        while stack and len(order) < MAX_PAGE_OBJECTS:
            number = stack.pop(0)
            if number in visited:
                continue
            visited.add(number)
            body = objects.get(number, {"body": b""})["body"]
            if re.search(rb"/Type\s*/Pages", body):
                kids = re.search(rb"/Kids\s*\[(.*?)\]", body, re.S)
                if kids:
                    children = [int(x) for x in re.findall(rb"(\d+)\s+\d+\s+R", kids.group(1))]
                    stack = children + stack
            elif re.search(rb"/Type\s*/Page[^s]", body):
                order.append(number)
        if not order:
            raise ExtractionError("broken_pdf", "页面树里没有页面", "确认文件是否为完整 PDF")
        out = []
        for index, number in enumerate(order, start=1):
            body = objects[number]["body"]
            box = self._media_box(objects, number)
            resources = self._ref(body, rb"/Resources")
            contents = self._ref(body, rb"/Contents")
            if contents is None:
                kids = re.search(rb"/Contents\s*\[(.*?)\]", body, re.S)
                refs = [int(x) for x in re.findall(rb"(\d+)\s+\d+\s+R", kids.group(1))] if kids else []
            else:
                refs = [contents]
            resource_body = self._inherited(objects, number, b"/Resources")
            out.append({"pdf_page": index, "object": number, "body": body, "box": box,
                        "resources": resource_body, "content_refs": refs})
        return objects, out

    def _inherited(self, objects, number, key):
        """Resolve an inheritable page attribute by walking /Parent."""
        body = objects.get(number, {"body": b""})["body"]
        found = re.search(key + rb"\s+(\d+)\s+\d+\s+R", body)
        if found:
            return objects.get(int(found.group(1)), {"body": b""})["body"]
        inline = re.search(key + rb"\s*(<<)", body)
        if inline:
            return BuiltinBackend._balanced_dict(body, inline.start(1))
        parent = self._ref(body, rb"/Parent")
        depth = 0
        while parent is not None and depth < 32:
            parent_body = objects.get(parent, {"body": b""})["body"]
            found = re.search(key + rb"\s+(\d+)\s+\d+\s+R", parent_body)
            if found:
                return objects.get(int(found.group(1)), {"body": b""})["body"]
            inline = re.search(key + rb"\s*(<<)", parent_body)
            if inline:
                return BuiltinBackend._balanced_dict(parent_body, inline.start(1))
            parent = self._ref(parent_body, rb"/Parent")
            depth += 1
        return b""

    def _media_box(self, objects, number):
        body = objects[number]["body"]
        box = re.search(rb"/MediaBox\s*\[\s*([\d.\-]+)\s+([\d.\-]+)\s+([\d.\-]+)\s+([\d.\-]+)", body)
        if not box:
            parent = self._ref(body, rb"/Parent")
            depth = 0
            while parent is not None and depth < 32:
                parent_body = objects.get(parent, {"body": b""})["body"]
                box = re.search(rb"/MediaBox\s*\[\s*([\d.\-]+)\s+([\d.\-]+)\s+([\d.\-]+)\s+([\d.\-]+)", parent_body)
                if box:
                    break
                parent = self._ref(parent_body, rb"/Parent")
                depth += 1
        if not box:
            return None
        try:
            x0, y0, x1, y1 = (float(v) for v in box.groups())
        except ValueError:
            return None
        return round(x1 - x0, 2), round(y1 - y0, 2)

    # -- fonts -------------------------------------------------------------
    def fonts(self, objects, resources):
        table = {}
        font_dict = self._ref(resources, rb"/Font")
        if font_dict is not None:
            body = objects.get(font_dict, {"body": b""})["body"]
        else:
            # /Font is often an inline dictionary; a non-greedy match would stop at the
            # first nested ">>", so walk the braces instead.
            inline = re.search(rb"/Font\s*(<<)", resources)
            body = BuiltinBackend._balanced_dict(resources, inline.start(1)) if inline else b""
        for name, ref in re.findall(rb"/([A-Za-z0-9#_.\-]+)\s+(\d+)\s+\d+\s+R", body):
            table[b"/" + name] = self._font(objects, int(ref))
        return table

    def _font(self, objects, number):
        body = objects.get(number, {"body": b""})["body"]
        entry = {"cmap": {}, "two_byte": b"/Type0" in body, "encoding": b"", "subtype": b""}
        subtype = re.search(rb"/Subtype\s*/([A-Za-z0-9]+)", body)
        if subtype:
            entry["subtype"] = subtype.group(1)
        encoding = re.search(rb"/Encoding\s*/?([A-Za-z0-9\-]+)", body)
        if encoding:
            entry["encoding"] = encoding.group(1)
        candidates = [body]
        descendant = self._ref(body, rb"/DescendantFonts\s*\[")
        if descendant:
            candidates.append(objects.get(descendant, {"body": b""})["body"])
        differences = re.search(rb"/Differences\s*\[(.*?)\]", body, re.S)
        if differences:
            code = 0
            for item in re.findall(rb"(\d+)|/([A-Za-z0-9#_.\-]+)", differences.group(1)):
                if item[0]:
                    code = int(item[0])
                    continue
                entry["cmap"][code] = glyph_to_unicode(item[1].decode("latin-1"))
                code += 1
        for candidate in candidates:
            target = self._ref(candidate, rb"/ToUnicode")
            if target is None or entry["cmap"]:
                continue
            raw = self._decode(objects.get(target, {"body": b"", "stream": None}))
            if raw:
                entry["cmap"].update(self._parse_cmap(raw))
        return entry

    @staticmethod
    def _parse_cmap(raw):
        cmap = {}
        for block in re.findall(rb"beginbfchar(.*?)endbfchar", raw, re.S):
            for src, dst in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block):
                cmap[int(src, 16)] = _utf16_be(dst)
        for block in re.findall(rb"beginbfrange(.*?)endbfrange", raw, re.S):
            for low, high, dst in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block):
                start, stop = int(low, 16), int(high, 16)
                base = _utf16_be(dst)
                if not base or stop < start or stop - start > 65535:
                    continue
                for offset in range(stop - start + 1):
                    cmap[start + offset] = base[:-1] + chr(ord(base[-1]) + offset)
            for low, high, array in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\[(.*?)\]", block, re.S):
                start = int(low, 16)
                for offset, item in enumerate(re.findall(rb"<([0-9A-Fa-f]+)>", array)):
                    cmap[start + offset] = _utf16_be(item)
        return cmap

    # -- text layer --------------------------------------------------------
    def extract(self, path):
        if Path(path).stat().st_size > 100 * 1024 * 1024:
            raise ExtractionError('extract_too_large', 'PDF exceeds extraction budget')
        blob = Path(path).read_bytes()
        if blob[:5] != b"%PDF-":
            raise ExtractionError("broken_pdf", "文件头不是 %PDF-", "确认这是 PDF 文件")
        if re.search(rb"/Encrypt", blob[:4096]) or re.search(rb"/Encrypt", blob[-4096:]):
            raise ExtractionError("encrypted_pdf", "PDF 已加密，无法抽取文本",
                                  "先用阅读器另存为未加密副本，或只做人工阅读")
        objects, pages = self.pages(blob)
        result = []
        for page in pages:
            fonts = self.fonts(objects, page["resources"])
            content = b"".join(self._decode(objects.get(ref, {"body": b"", "stream": None}))
                               for ref in page["content_refs"])
            text, warnings = self._page_text(content, fonts)
            images = self._has_images(objects, page["resources"])
            width, height = page["box"] if page["box"] else (None, None)
            quality = text_quality(text)
            if text.strip() and quality < MIN_TEXT_QUALITY:
                # Refuse to publish unreadable output: a wrong encoding must never become
                # "evidence". The caller sees no_text_layer and can still read manually.
                warnings.append(f"第 {page['pdf_page']} 页文本解码质量过低（{quality:.2f}），"
                                f"已按无可信文本层处理")
                text = ""
            result.append(Page(pdf_page=page["pdf_page"], text=text, char_count=len(text),
                               width=width, height=height,
                               has_text=len(text.strip()) >= MIN_USEFUL_CHARS,
                               has_images=images, warnings=warnings))
        return result

    def _has_images(self, objects, resources):
        """True when the page draws at least one /Subtype /Image XObject."""
        xobjects = self._ref(resources, rb"/XObject")
        body = resources
        if xobjects is not None:
            body = objects.get(xobjects, {"body": b""})["body"]
        else:
            inline = re.search(rb"/XObject\s*(<<)", resources)
            if inline:
                body = BuiltinBackend._balanced_dict(resources, inline.start(1))
        for reference in re.findall(rb"(\d+)\s+\d+\s+R", body):
            target = objects.get(int(reference), {"body": b""})["body"]
            if re.search(rb"/Subtype\s*/Image", target[:400]):
                return True
        return False

    def _tokens(self, content):
        """Yield (kind, value) with kind in {num, str, name, op}."""
        for match in _TOKEN_RE.finditer(content):
            kind = match.lastgroup
            raw = match.group()
            if kind == "num":
                yield "num", float(raw)
            elif kind in ("hex", "lit"):
                yield "str", raw
            elif kind == "name":
                yield "name", raw
            elif kind == "op":
                yield "op", raw

    def _page_text(self, content, fonts):
        """Run the content stream and return (text, warnings)."""
        fragments = []
        warnings = []
        saved = []
        state = {"font": None, "size": 12.0, "ctm": [1, 0, 0, 1, 0, 0],
                 "tm": [1, 0, 0, 1, 0, 0], "tlm": [1, 0, 0, 1, 0, 0], "leading": 0.0,
                 "char_spacing": 0.0, "word_spacing": 0.0, "h_scale": 1.0, "rise": 0.0}
        operands = []
        unmapped = 0
        for kind, value in self._tokens(content):
            if kind != "op":
                operands.append((kind, value))
                continue
            try:
                unmapped += self._apply(value, operands, state, fragments, saved, fonts)
            except (KeyError, ValueError, TypeError, IndexError) as exc:
                warnings.append(f"内容流指令 {value.decode('latin-1', 'replace')} 解析异常：{exc}")
            operands = []
        if unmapped:
            warnings.append(f"{unmapped} 个字形没有 ToUnicode 映射，相关字符被跳过而不是猜测")
        if not fragments:
            return "", warnings
        text = self._layout(fragments)
        rotated = sum(1 for fragment in fragments if fragment["rotated"])
        if rotated >= max(3, len(fragments) // 3):
            warnings.append("页面包含旋转或竖排文本，行顺序可能不准确")
        return text, warnings

    @staticmethod
    def _strings(operands):
        return [value for kind, value in operands if kind == "str"]

    @staticmethod
    def _numbers(operands):
        return [value for kind, value in operands if kind == "num"]

    @staticmethod
    def _names(operands):
        return [value for kind, value in operands if kind == "name"]

    def _codes(self, operand, font):
        """Hex or literal string -> character codes for this font."""
        two_byte = bool((font or {}).get("two_byte"))
        if operand[:1] == b"<":
            digits = re.sub(rb"[^0-9A-Fa-f]", b"", operand)
            padded = digits.decode("latin-1").ljust((len(digits) + 1) // 2 * 2, "0")
            try:
                raw = bytes.fromhex(padded)
            except ValueError:
                return []
        elif operand[:1] == b"(":
            raw = _unescape_literal(operand[1:-1])
        else:
            return []
        if two_byte:
            return [int.from_bytes(raw[index:index + 2], "big") for index in range(0, len(raw) - 1, 2)]
        return list(raw)

    def _decode_operand(self, operand, font):
        codes = self._codes(operand, font)
        cmap = (font or {}).get("cmap") or {}
        if cmap:
            mapping = cmap
        elif (font or {}).get("encoding") == b"MacRomanEncoding":
            mapping = _MAC_ROMAN_HIGH
        else:
            mapping = _WIN_ANSI
        out = []
        for code in codes:
            char = mapping.get(code)
            if char is None:
                if cmap:
                    continue                       # unmapped glyph: skip, never guess
                char = chr(code) if 0x20 <= code < 0x7F else ""
            out.append(char)
        return "".join(out)

    def _apply(self, op, operands, state, fragments, saved, fonts):
        """Apply one operator. Returns the number of unmapped glyphs it hit."""
        numbers = self._numbers(operands)
        if op == b"q":
            saved.append(dict(state))
        elif op == b"Q":
            if saved:
                state.update(saved.pop())
        elif op == b"BT":
            state["tm"] = [1, 0, 0, 1, 0, 0]
            state["tlm"] = [1, 0, 0, 1, 0, 0]
        elif op == b"Tf":
            names = self._names(operands)
            if names:
                state["font"] = names[-1]
            if numbers:
                state["size"] = numbers[-1]
        elif op == b"TL" and numbers:
            state["leading"] = numbers[-1]
        elif op == b"Tc" and numbers:
            state["char_spacing"] = numbers[-1]
        elif op == b"Tw" and numbers:
            state["word_spacing"] = numbers[-1]
        elif op == b"Tz" and numbers:
            state["h_scale"] = numbers[-1] / 100.0
        elif op == b"Ts" and numbers:
            state["rise"] = numbers[-1]
        elif op == b"cm" and len(numbers) >= 6:
            state["ctm"] = self._multiply(numbers[-6:], state["ctm"])
        elif op in (b"Td", b"TD") and len(numbers) >= 2:
            if op == b"TD":
                state["leading"] = -numbers[-1]
            state["tlm"] = self._translate(numbers[-2], numbers[-1], state["tlm"])
            state["tm"] = list(state["tlm"])
        elif op == b"Tm" and len(numbers) >= 6:
            state["tm"] = list(numbers[-6:])
            state["tlm"] = list(numbers[-6:])
        elif op == b"T*":
            state["tlm"] = self._translate(0, -state["leading"], state["tlm"])
            state["tm"] = list(state["tlm"])
        elif op in (b"Tj", b"'", b'"'):
            if op in (b"'", b'"'):
                state["tlm"] = self._translate(0, -state["leading"], state["tlm"])
                state["tm"] = list(state["tlm"])
            strings = self._strings(operands)
            if strings:
                return self._emit(strings[-1], state, fragments, fonts)
        elif op == b"TJ":
            unmapped = 0
            for kind, value in operands:
                if kind == "str":
                    unmapped += self._emit(value, state, fragments, fonts)
                elif kind == "num" and value < -180:
                    self._emit_gap(state, fragments, value)
            return unmapped
        return 0

    def _emit(self, operand, state, fragments, fonts):
        font = fonts.get(state["font"]) if state["font"] else None
        codes = self._codes(operand, font)
        cmap = (font or {}).get("cmap") or {}
        unmapped = sum(1 for code in codes if cmap and code not in cmap) if cmap else 0
        text = self._decode_operand(operand, font)
        if not text:
            return unmapped
        matrix = self._multiply(state["tm"], state["ctm"])
        size = abs(state["size"] * matrix[3]) or abs(state["size"]) or 12.0
        fragments.append({"x": matrix[4], "y": matrix[5], "size": max(size, 1.0), "text": text,
                          "rotated": abs(matrix[1]) > 1e-6 or abs(matrix[2]) > 1e-6})
        return unmapped

    @staticmethod
    def _multiply(left, right):
        a, b, c, d, e, f = left
        a2, b2, c2, d2, e2, f2 = right
        return [a * a2 + b * c2, a * b2 + b * d2, c * a2 + d * c2, c * b2 + d * d2,
                e * a2 + f * c2 + e2, e * b2 + f * d2 + f2]

    @staticmethod
    def _translate(tx, ty, matrix):
        a, b, c, d, e, f = matrix
        return [a, b, c, d, e + tx * a + ty * c, f + tx * b + ty * d]

    def _emit_gap(self, state, fragments, kern):
        """A large negative kern inside TJ is a wide visual gap."""
        size = abs(state["size"]) or 12.0
        if abs(kern) / 1000.0 * size > size * 0.6:
            matrix = self._multiply(state["tm"], state["ctm"])
            fragments.append({"x": matrix[4], "y": matrix[5], "size": size, "text": " ",
                              "rotated": False})

    def _layout(self, fragments):
        """Order fragments into lines, detect columns, and join into page text."""
        columns = self._split_columns(fragments)
        blocks = [[self._join_rows(column) for column in columns]]
        if len(columns) > 1:
            text = "\n".join(block for block in blocks[0] if block)
        else:
            text = blocks[0][0] if blocks[0] else ""
        return re.sub(r"\n{3,}", "\n\n", text).strip()

    @staticmethod
    def _split_columns(fragments):
        """Split fragments into reading-order columns when a gutter is unmistakable.

        Detection happens on fragments, not on assembled lines: once two columns are
        merged into one line their positions are lost. Every fragment start is examined
        and the widest gap that separates two well-populated, non-overlapping groups wins.
        """
        starts = sorted(fragment["x"] for fragment in fragments)
        if len(starts) < 8:
            return [fragments]
        best_gap, best_split = 0.0, None
        for index in range(len(starts) - 1):
            gap = starts[index + 1] - starts[index]
            if gap > best_gap:
                best_gap, best_split = gap, (starts[index] + starts[index + 1]) / 2
        if best_split is None or best_gap < 60:
            return [fragments]
        left = [fragment for fragment in fragments if fragment["x"] < best_split]
        right = [fragment for fragment in fragments if fragment["x"] >= best_split]
        if len(left) < 4 or len(right) < 4:
            return [fragments]
        left_x = [fragment["x"] for fragment in left]
        right_x = [fragment["x"] for fragment in right]
        if max(left_x) >= min(right_x):
            return [fragments]
        left_y = [fragment["y"] for fragment in left]
        right_y = [fragment["y"] for fragment in right]
        overlap = min(max(left_y), max(right_y)) - max(min(left_y), min(right_y))
        if overlap <= 0:
            return [fragments]
        # A wide table inside one column can also create a big gap; require the left group
        # to hold the page's dominant left margin (a third of all fragments or more).
        if len(left) < max(4, len(fragments) // 8):
            return [fragments]
        return [left, right]

    @staticmethod
    def _join_rows(fragments):
        """Group fragments that share a baseline into text lines."""
        rows = []
        for fragment in fragments:
            placed = False
            for row in rows:
                if abs(row["y"] - fragment["y"]) <= max(row["size"], fragment["size"]) * 0.5:
                    row["parts"].append(fragment)
                    row["y"] = (row["y"] + fragment["y"]) / 2
                    row["size"] = max(row["size"], fragment["size"])
                    placed = True
                    break
            if not placed:
                rows.append({"y": fragment["y"], "size": fragment["size"], "parts": [fragment]})
        rows.sort(key=lambda row: -row["y"])
        lines = []
        for row in rows:
            row["parts"].sort(key=lambda part: part["x"])
            text = row["parts"][0]["text"]
            previous = row["parts"][0]
            for part in row["parts"][1:]:
                gap = part["x"] - previous["x"]
                estimate = max(previous["size"], 1.0) * 0.25 * max(1, len(previous["text"].split(" ")[-1]))
                if gap > estimate and not text.endswith(" ") and not part["text"].startswith(" "):
                    text += " "
                text += part["text"]
                previous = part
            lines.append(re.sub(r"[ \t]{2,}", " ", text).rstrip())
        return "\n".join(line for line in lines if line.strip())

# ---------------------------------------------------------------------------
# backend 3: pypdf (optional)
# ---------------------------------------------------------------------------

class PypdfBackend:
    name = "pypdf"

    def __init__(self):
        self._module = None

    def _load(self):
        if self._module is None:
            try:
                import pypdf  # noqa: PLC0415
            except ImportError:
                return None
            self._module = pypdf
        return self._module

    def available(self):
        return self._load() is not None

    def version(self):
        module = self._load()
        return f"pypdf-{getattr(module, '__version__', 'unknown')}" if module else ""

    def extract(self, path):
        module = self._load()
        if module is None:
            raise ExtractionError("no_backend", "pypdf 未安装", "改用 pdftotext 或内置抽取器")
        try:
            reader = module.PdfReader(str(path))
            if reader.is_encrypted:
                raise ExtractionError("encrypted_pdf", "PDF 已加密", "另存为未加密副本后再抽取")
            pages = []
            for index, page in enumerate(reader.pages, start=1):
                try:
                    text = page.extract_text() or ""
                except Exception as exc:                      # noqa: BLE001 - backend specific
                    text = ""
                    pages.append(Page(pdf_page=index, text="", char_count=0, has_text=False,
                                      warnings=[f"第 {index} 页抽取失败：{exc}"]))
                    continue
                box = page.mediabox
                pages.append(Page(pdf_page=index, text=text, char_count=len(text),
                                  width=float(box.width), height=float(box.height),
                                  has_text=len(text.strip()) >= MIN_USEFUL_CHARS))
            return pages
        except ExtractionError:
            raise
        except Exception as exc:                              # noqa: BLE001 - backend specific
            raise ExtractionError("broken_pdf", f"pypdf 无法读取：{exc}", "确认文件完整") from exc


def builtin_backend():
    return BuiltinBackend()


def available_backends():
    backends = {"pdftotext": PdftotextBackend(), "builtin": BuiltinBackend(), "pypdf": PypdfBackend()}
    return {name: (backend, backend.available()) for name, backend in backends.items()}
