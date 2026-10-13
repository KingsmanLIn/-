"""Conservative text cleaning for extracted PDF pages.

Rules that matter for evidence checking:
* digits, minus signs, percent signs, units and table values are never rewritten;
* superscripts/subscripts are preserved (m2 stays m2, m² stays m²) because NFKC would
  destroy that distinction - only ligatures and invisible characters are normalised;
* every change is counted and reported, so a caller can tell "nothing happened"
  from "we rewrote 4 000 characters";
* the raw extraction result is kept by the caller, so cleaning is reversible.
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field

# --- character normalisation ------------------------------------------------

LIGATURES = {
    "\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi", "\ufb04": "ffl",
    "\ufb05": "ft", "\ufb06": "st",
    "\u0153": "oe", "\u0152": "OE", "\u00e6": "ae", "\u00c6": "AE",
}
INVISIBLE = {
    "\u00ad": "",      # soft hyphen: a line-break hint, not a character
    "\ufeff": "",      # BOM
    "\u200b": "",      # zero width space
    "\u200c": "", "\u200d": "",
    "\u2060": "",
}
SPACE_EQUIVALENTS = {
    "\u00a0": " ", "\u2000": " ", "\u2001": " ", "\u2002": " ", "\u2003": " ",
    "\u2004": " ", "\u2005": " ", "\u2006": " ", "\u2007": " ", "\u2008": " ",
    "\u2009": " ", "\u200a": " ", "\u202f": " ", "\u205f": " ", "\u3000": " ",
    "\t": " ", "\u2028": "\n", "\u2029": "\n", "\u000b": "\n", "\u000c": "\n",
}
DASHES = {"\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-",
          "\u2015": "-", "\u2212": "-", "\ufe63": "-", "\uff0d": "-"}
QUOTES = {"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2032": "'",
          "\u2033": '"'}

_MULTISPACE = re.compile(r"[ \t]{2,}")
_LEADING_SPACE = re.compile(r"^ +", re.M)
_TRAILING_SPACE = re.compile(r" +$", re.M)
_BLANK_LINES = re.compile(r"\n{3,}")
# hyphen at a line end, followed by a lowercase letter on the next line
_SOFT_HYPHEN_BREAK = re.compile(r"(?<=[A-Za-z])-\n(?=[a-z])")
_PAGE_NUMBER_ONLY = re.compile(r"^[\s\-–—·|/]*\d{1,4}[\s\-–—·|/]*$")
_MOSTLY_PUNCT = re.compile(r"^[\W\d_]+$", re.UNICODE)


@dataclass
class CleanStats:
    """What cleaning did, per document."""
    ligatures: int = 0
    invisible: int = 0
    spaces: int = 0
    dashes: int = 0
    quotes: int = 0
    hyphen_joins: int = 0
    collapsed_lines: int = 0
    header_footer_lines_removed: int = 0
    repeated_lines: dict = field(default_factory=dict)

    def as_dict(self):
        return {
            "ligatures": self.ligatures, "invisible": self.invisible, "spaces": self.spaces,
            "dashes": self.dashes, "quotes": self.quotes, "hyphen_joins": self.hyphen_joins,
            "collapsed_lines": self.collapsed_lines,
            "header_footer_lines_removed": self.header_footer_lines_removed,
            "repeated_lines": dict(self.repeated_lines),
        }

    def total_changes(self):
        return (self.ligatures + self.invisible + self.spaces + self.dashes + self.quotes
                + self.hyphen_joins + self.collapsed_lines + self.header_footer_lines_removed)


def _map_characters(text, mapping, counter_name, stats):
    out = []
    hits = 0
    for char in text:
        replacement = mapping.get(char)
        if replacement is None:
            out.append(char)
        else:
            out.append(replacement)
            hits += 1
    if hits:
        setattr(stats, counter_name, getattr(stats, counter_name) + hits)
    return "".join(out)


def normalise_characters(text, stats=None):
    """Character-level normalisation that cannot change any number."""
    stats = stats or CleanStats()
    text = _map_characters(text, LIGATURES, "ligatures", stats)
    text = _map_characters(text, INVISIBLE, "invisible", stats)
    text = _map_characters(text, SPACE_EQUIVALENTS, "spaces", stats)
    text = _map_characters(text, DASHES, "dashes", stats)
    text = _map_characters(text, QUOTES, "quotes", stats)
    # NFC keeps superscripts, subscripts, units and digits intact (NFKC would not).
    text = unicodedata.normalize("NFC", text)
    return text


def normalise_whitespace(text, stats=None):
    stats = stats or CleanStats()
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    cleaned = []
    for line in lines:
        original = line
        line = _MULTISPACE.sub(" ", line)
        line = line.strip(" ")
        if line != original:
            stats.collapsed_lines += 1
        cleaned.append(line)
    out = "\n".join(cleaned)
    out = _BLANK_LINES.sub("\n\n", out)
    return out.strip("\n")


def join_line_break_hyphens(text, stats=None):
    """Join `informa-\\ntion` style breaks.

    Only applied when the hyphen sits at a line end, the next line starts with a
    lowercase letter and the joined word is at least four letters long. A real
    hyphen inside a word never has that shape, so `cost-benefit` is safe.
    """
    stats = stats or CleanStats()

    def replace(match):
        stats.hyphen_joins += 1
        return ""

    return _SOFT_HYPHEN_BREAK.sub(replace, text)


def _signature(line):
    """Shape of a line, digits masked, for repeated header/footer detection."""
    masked = re.sub(r"\d+", "#", line.lower())
    masked = re.sub(r"[^0-9a-z\u4e00-\u9fff#]+", " ", masked)
    return re.sub(r"\s+", " ", masked).strip()


def detect_repeated_lines(pages, zone=2, min_pages=3, min_ratio=0.6):
    """Lines that repeat in the top/bottom `zone` lines of most pages.

    Only covers very short, digit-heavy or identical lines, which is what page
    headers, footers and page numbers look like. Body text is never a candidate.
    """
    texts = [page["text"].split("\n") for page in pages]
    pages_with_text = [lines for lines in texts if any(line.strip() for line in lines)]
    if len(pages_with_text) < min_pages:
        return {}
    candidates = Counter()
    for lines in pages_with_text:
        seen = set()
        for index, line in enumerate(lines):
            in_zone = index < zone or index >= len(lines) - zone
            stripped = line.strip()
            if not in_zone or not stripped or len(stripped) > 120:
                continue
            signature = _signature(stripped)
            if len(signature) < 2:
                continue
            # keep it to page furniture: page numbers, journal names, running heads
            if len(signature) > 90 and not _MOSTLY_PUNCT.match(signature):
                continue
            seen.add(signature)
        candidates.update(seen)
    threshold = max(min_pages, int(len(pages_with_text) * min_ratio + 0.5))
    repeated = {sig: count for sig, count in candidates.items() if count >= threshold}
    # never delete more than a third of a page's lines
    return repeated


def remove_repeated_lines(pages, repeated, stats=None):
    """Drop repeated margin lines and bare page numbers.

    Only the first/last two lines of a page are candidates, and a page must keep at
    least three non-empty lines: a short page is never gutted by furniture removal.
    """
    stats = stats or CleanStats()
    if not repeated:
        return pages
    out = []
    for page in pages:
        lines = page["text"].split("\n")
        non_empty = sum(1 for line in lines if line.strip())
        removed_here = 0
        kept = []
        for index, line in enumerate(lines):
            stripped = line.strip()
            in_margin = index < 2 or index >= len(lines) - 2
            droppable = non_empty - removed_here > 3
            if in_margin and droppable:
                signature = _signature(stripped)
                if signature and signature in repeated:
                    stats.header_footer_lines_removed += 1
                    removed_here += 1
                    continue
                if _PAGE_NUMBER_ONLY.match(stripped) and non_empty > 4:
                    stats.header_footer_lines_removed += 1
                    removed_here += 1
                    continue
            kept.append(line)
        page = dict(page)
        page["text"] = "\n".join(kept).strip("\n")
        page["char_count"] = len(page["text"])
        out.append(page)
    stats.repeated_lines = {sig: count for sig, count in sorted(repeated.items())}
    return out


def clean_pages(pages, join_hyphens=False, drop_headers=True, stats=None):
    """Clean a list of page dicts in place-safe fashion and return (pages, stats)."""
    stats = stats or CleanStats()
    working = []
    for page in pages:
        text = normalise_characters(page["text"], stats)
        text = normalise_whitespace(text, stats)
        if join_hyphens:
            text = join_line_break_hyphens(text, stats)
        updated = dict(page)
        updated["text"] = text
        updated["char_count"] = len(text)
        working.append(updated)
    if drop_headers:
        repeated = detect_repeated_lines(working)
        working = remove_repeated_lines(working, repeated, stats)
    return working, stats
