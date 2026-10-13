"""Split extracted page text into overlapping chunks with verifiable page ranges.

A chunk always knows the physical PDF pages it came from, so an answer built on a
chunk can be traced back to `pdf_page` in the reader. Chunks are cut on section
headings first, then by size, and consecutive chunks overlap so a sentence spanning a
boundary is still fully present in at least one chunk.

    chunker = Chunker(max_chars=6000, overlap_ratio=0.1)
    chunks = chunker.chunk(pages)         # pages: list[Page] or list[dict]
    chunks[0].chunk_id, chunks[0].start_page, chunks[0].end_page, chunks[0].text
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

DEFAULT_MAX_CHARS = 6000
DEFAULT_OVERLAP_RATIO = 0.1
MAX_SECTION_CHARS = 40000

# A heading is short, has no sentence-ending punctuation, and looks like a title.
_NUMBERED_HEADING = re.compile(
    r"^\s*(?:(?:第\s*[一二三四五六七八九十百零\d]+\s*[章节部分])|"
    r"(?:\d+(?:\.\d+)*[.、]?\s+\S)|"
    r"(?:[IVXLC]+\.\s+\S)|"
    r"(?:[一二三四五六七八九十]+[、.]\s*\S))")
_KEYWORD_HEADING = re.compile(
    r"^\s*(?:Abstract|Introduction|Background|Related\s+work|Literature|Data|"
    r"Data\s+and\s+methods?|Methods?|Methodology|Empirical\s+strategy|Identification|"
    r"Results?|Findings|Discussion|Robustness|Mechanisms?|Conclusion[s]?|"
    r"References|Appendix|摘要|引言|导言|背景|文献综述|数据|数据与方法|研究方法|"
    r"识别策略|实证策略|结果|主要结果|稳健性|稳健性检验|机制|讨论|结论|结语|"
    r"参考文献|附录)\b", re.I)


@dataclass
class Chunk:
    chunk_id: str
    start_page: int
    end_page: int
    text: str
    section: str = ""
    order: int = 0
    page_offsets: list = field(default_factory=list)   # (pdf_page, char offset in text)

    @property
    def char_count(self):
        return len(self.text)

    def page_of_offset(self, offset):
        """Map a character offset inside this chunk back to a physical PDF page."""
        page = self.start_page
        for pdf_page, start in self.page_offsets:
            if offset >= start:
                page = pdf_page
            else:
                break
        return page

    def pages(self):
        return list(range(self.start_page, self.end_page + 1))

    def as_dict(self):
        return {"chunk_id": self.chunk_id, "start_page": self.start_page,
                "end_page": self.end_page, "char_count": self.char_count,
                "section": self.section, "order": self.order,
                "pages": self.pages(),
                "text": self.text}


def page_text(page):
    if isinstance(page, dict):
        return int(page["pdf_page"]), str(page.get("text", ""))
    return int(page.pdf_page), str(page.text)


def find_sections(pages):
    """Locate heading lines; returns [(line_index_in_document, heading_text)]."""
    lines = []
    for pdf_page, text in (page_text(page) for page in pages):
        for line in text.split("\n"):
            lines.append((pdf_page, line))
    found = []
    for index, (pdf_page, line) in enumerate(lines):
        stripped = line.strip()
        if not stripped or len(stripped) > 60:
            continue
        if stripped.endswith((".", "。", "！", "？", "!", "?", ";", "；", ",")):
            continue
        if _KEYWORD_HEADING.match(stripped) or _NUMBERED_HEADING.match(stripped):
            found.append((index, stripped))
    return found, lines


class Chunker:
    """Section-aware, size-bounded, overlapping chunker."""

    def __init__(self, max_chars=DEFAULT_MAX_CHARS, overlap_ratio=DEFAULT_OVERLAP_RATIO,
                 min_chars=200, prefer_sections=True):
        if max_chars < 200:
            raise ValueError("max_chars 至少为 200")
        if not 0 <= overlap_ratio < 0.5:
            raise ValueError("overlap_ratio 必须在 0 到 0.5 之间")
        self.max_chars = max_chars
        self.overlap_ratio = overlap_ratio
        self.min_chars = min_chars
        self.prefer_sections = prefer_sections

    def chunk(self, pages, document_id=""):
        """Return a list of Chunk covering the document in reading order."""
        usable = [(page_text(page)[0], page_text(page)[1]) for page in pages]
        usable = [(number, text) for number, text in usable if text.strip()]
        if not usable:
            return []
        sections = self._section_spans(usable) if self.prefer_sections else []
        chunks = []
        for section_name, blocks in sections:
            chunks.extend(self._chunk_section(section_name, blocks))
        return self._finalise(chunks, document_id)

    # -- sections ----------------------------------------------------------
    def _section_spans(self, usable):
        """Group page text into (section name, [(pdf_page, text)]) blocks."""
        lines, _ = find_sections([{"pdf_page": number, "text": text} for number, text in usable])
        if not lines:
            return [("", usable)]
        # walk the flattened document, cutting at each heading line
        flat = []
        for number, text in usable:
            for line in text.split("\n"):
                flat.append((number, line))
        boundaries = [index for index, _name in lines]
        spans = []
        for position, start in enumerate(boundaries):
            end = boundaries[position + 1] if position + 1 < len(boundaries) else len(flat)
            name = lines[position][1]
            body = flat[start:end]
            if not body:
                continue
            spans.append((name, self._merge_pages(body)))
        head = flat[:boundaries[0]]
        if head and any(line.strip() for _number, line in head):
            spans.insert(0, ("", self._merge_pages(head)))
        return spans

    @staticmethod
    def _merge_pages(lines):
        """[(pdf_page, line)] -> [(pdf_page, text)] keeping page boundaries."""
        blocks = []
        for number, line in lines:
            if blocks and blocks[-1][0] == number:
                blocks[-1][1].append(line)
            else:
                blocks.append((number, [line]))
        return [(number, "\n".join(chunk_lines).strip()) for number, chunk_lines in blocks
                if "\n".join(chunk_lines).strip()]

    # -- size --------------------------------------------------------------
    def _chunk_section(self, section, blocks):
        """Cut one section into chunks no longer than max_chars, with overlap."""
        # flatten to (pdf_page, char, text) so an offset maps back to a page
        full = []
        offsets = []
        position = 0
        for number, text in blocks:
            offsets.append((number, position))
            full.append(text)
            position += len(text) + 1                     # +1 for the join newline
        document = "\n".join(full)
        if not document.strip():
            return []
        chunks = []
        start = 0
        length = len(document)
        while start < length:
            end = min(start + self.max_chars, length)
            if end < length:
                end = self._soft_break(document, start, end)
            piece = document[start:end]
            if not piece.strip():
                break
            start_page, end_page = self._page_range(offsets, start, end)
            chunks.append({"text": piece, "start_page": start_page, "end_page": end_page,
                           "section": section, "offsets": offsets, "start": start})
            if end >= length:
                break
            overlap = int((end - start) * self.overlap_ratio)
            start = max(start + 1, end - overlap)
        return chunks

    def _soft_break(self, document, start, end):
        """Move the cut back to a paragraph or sentence boundary when one is near."""
        window = document[max(start + self.min_chars, end - 600):end]
        for pattern in ("\n\n", "。", ". ", "；", ";", "\n"):
            index = window.rfind(pattern)
            if index > 0:
                return max(start + self.min_chars, end - len(window) + index + len(pattern))
        return end

    @staticmethod
    def _page_range(offsets, start, end):
        """Physical page range covering the character span [start, end)."""
        first = last = offsets[0][0]
        for number, position in offsets:
            if position <= start:
                first = number
            if position < end:
                last = number
        return first, max(first, last)

    # -- ids and page offsets ---------------------------------------------
    def _finalise(self, raw_chunks, document_id):
        chunks = []
        for order, item in enumerate(raw_chunks):
            base = f"{document_id}-" if document_id else ""
            chunk = Chunk(chunk_id=f"{base}c{order + 1:03d}",
                          start_page=item["start_page"], end_page=item["end_page"],
                          text=item["text"], section=item["section"], order=order)
            chunk.page_offsets = [(number, position - item["start"]) for number, position in item["offsets"]
                                  if position - item["start"] <= len(item["text"])]
            chunks.append(chunk)
        return chunks
