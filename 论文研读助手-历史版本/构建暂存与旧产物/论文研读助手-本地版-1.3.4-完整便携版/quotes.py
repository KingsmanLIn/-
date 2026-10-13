"""Quote verification against the extracted paper text.

Phase 4 must never present model text that cannot be checked, so every quote the model
returns is looked up in the real page text before it is stored:

* matching tolerates the differences extraction and cleaning introduce (whitespace,
  line breaks, hyphenation, full-width vs half-width punctuation, ligatures) but never
  invents characters;
* when a quote is found on a different physical page than claimed, the verifier reports
  the real page so the value can be corrected instead of trusted;
* a quote that appears nowhere is reported as `found=False` and the caller decides.
"""
from __future__ import annotations

import re
import unicodedata

PUNCTUATION_MAP = {
    "，": ",", "。": ".", "、": ",", "；": ";", "：": ":", "？": "?", "！": "!",
    "（": "(", "）": ")", "【": "[", "】": "]", "《": "<", "》": ">", "“": '"',
    "”": '"', "‘": "'", "’": "'", "—": "-", "–": "-", "－": "-", "～": "~",
    "·": "", "•": "", "\u00a0": " ", "\u3000": " ",
}
_WHITESPACE = re.compile(r"\s+")
_DASHES = re.compile(r"[-\u2010-\u2015\u2212]+")


def normalise(text):
    """Aggressive but character-preserving form used for comparison only."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", str(text))
    text = "".join(PUNCTUATION_MAP.get(char, char) for char in text)
    text = text.lower()
    text = _DASHES.sub("-", text)
    text = _WHITESPACE.sub("", text)
    return text.strip()


def _collapse(text):
    """Whitespace-insensitive but order-preserving form (keeps punctuation)."""
    return _WHITESPACE.sub("", unicodedata.normalize("NFKC", str(text or "")).lower())


class PageTextIndex:
    """Searchable view over the extracted pages of one paper."""

    def __init__(self, pages, strip_hyphens=True):
        self.pages = []
        for page in pages or []:
            number = int(page["pdf_page"] if isinstance(page, dict) else page.pdf_page)
            text = page.get("text", "") if isinstance(page, dict) else getattr(page, "text", "")
            self.pages.append((number, text or ""))
        self.page_count = max((number for number, _text in self.pages), default=0)
        self._loose = {number: normalise(text) for number, text in self.pages}
        self._tight = {number: _collapse(text) for number, text in self.pages}
        self.all_loose = "".join(self._loose.values())
        self.all_tight = "".join(self._tight.values())
        self.strip_hyphens = strip_hyphens

    def alive(self):
        return bool(self.all_loose)

    def find(self, quote, claimed_page=None):
        """Look the quote up.

        Returns a dict: found, page (real page when found), claimed_page, exact_page
        (True when the quote is on the page the model claimed), similarity (0..1),
        matched (the text form that matched).
        """
        result = {"found": False, "page": None, "claimed_page": claimed_page,
                  "exact_page": False, "similarity": 0.0, "matched": "", "kind": "none"}
        needle = normalise(quote)
        if not needle or not self.alive():
            return result
        result["matched"] = needle
        for number, haystack in self._loose.items():
            if needle in haystack:
                result.update(found=True, page=number, kind="exact")
                break
        if not result["found"]:
            tight_needle = _collapse(quote)
            for number, haystack in self._tight.items():
                if tight_needle and tight_needle in haystack:
                    # the same text with extraction artefacts removed: still a complete match
                    result.update(found=True, page=number, similarity=0.99, kind="exact")
                    break
        if not result["found"]:
            best, best_page = 0.0, None
            for number, haystack in self._loose.items():
                score = _partial_score(needle, haystack)
                if score > best:
                    best, best_page = score, number
            if best >= 0.72:
                # A partial match is a *candidate*: the reader may look at it, but it is not
                # a verbatim quotation and must never be presented as verified.
                result.update(found=True, page=best_page, similarity=round(best, 3),
                              kind="approximate")
            else:
                result["similarity"] = round(best, 3)
                return result
        if result["page"] is not None and claimed_page is not None:
            result["exact_page"] = int(result["page"]) == int(claimed_page)
        result["similarity"] = result["similarity"] or 1.0
        return result

    def find_anywhere(self, quote, claimed_page=None):
        """Like `find`, but also tolerates characters split by extraction artefacts.

        Some PDFs (notably Chinese ones extracted from a CID font) come back with stray
        spaces between characters, so a literal quote never matches. This compares the
        whitespace-free forms with a sliding window and accepts a high overlap. When the
        quote is not on the claimed page the real page is returned so the caller can
        correct the citation instead of discarding a genuine quote.
        """
        result = self.find(quote, claimed_page=claimed_page)
        if result["found"]:
            return result
        needle_loose = normalise(quote)
        needle_tight = _collapse(quote)
        best = {"found": False, "page": None, "claimed_page": claimed_page,
                "exact_page": False, "similarity": 0.0, "matched": needle_loose,
                "kind": "none"}
        for number, haystack in self._loose.items():
            score = _window_similarity(needle_tight, haystack)
            if score > best["similarity"]:
                best = {"found": score >= 0.9, "page": number, "claimed_page": claimed_page,
                        "exact_page": claimed_page is not None and int(claimed_page) == number,
                        "similarity": round(score, 3), "matched": needle_loose,
                        "kind": "approximate" if score >= 0.9 else "none"}
        if best["found"]:
            return best
        # last resort: the quote may be real but attributed to the wrong page
        for number, haystack in self._tight.items():
            if needle_tight and needle_tight in haystack:
                return {"found": True, "page": number, "claimed_page": claimed_page,
                        "exact_page": claimed_page is not None and int(claimed_page) == number,
                        "similarity": 1.0, "matched": needle_loose, "kind": "exact"}
        best["page"] = None
        best["exact_page"] = False
        return best

    def exists_anywhere(self, text, minimum=12):
        needle = normalise(text)
        if len(needle) < minimum:
            return False
        return needle in self.all_loose

    def pages_for(self, text, minimum=12):
        needle = normalise(text)
        if len(needle) < minimum:
            return []
        return [number for number, haystack in self._loose.items() if needle in haystack]


def is_verbatim(match):
    """True only for a complete, normalised match.

    A fuzzy window hit is reported as a candidate so the reader can judge it, but it is not
    a verbatim quotation and must not be counted as verified evidence.
    """
    return bool(match) and bool(match.get("found")) and match.get("kind") == "exact"


def _partial_score(needle, haystack):
    """Length of the longest prefix of `needle` present in `haystack`, as a ratio."""
    if not needle or not haystack:
        return 0.0
    low, high = 0, len(needle)
    while low < high:
        middle = (low + high + 1) // 2
        if needle[:middle] in haystack:
            low = middle
        else:
            high = middle - 1
    return low / len(needle)


def _window_similarity(needle_tight, haystack, max_positions=600):
    """Best prefix+suffix overlap of `needle_tight` inside `haystack`.

    Catches quotes whose characters are split by extraction artefacts (spaces inserted
    between CJK characters) without ignoring real differences in the middle.
    """
    if not needle_tight or not haystack:
        return 0.0
    if needle_tight in haystack:
        return 1.0
    first, last = needle_tight[0], needle_tight[-1]
    length = len(needle_tight)
    best = 0
    checked = 0
    start = haystack.find(first)
    while start != -1 and checked < max_positions:
        checked += 1
        end = haystack.find(last, start + 1)
        while end != -1 and checked < max_positions:
            checked += 1
            span = haystack[start:end + 1]
            if len(span) <= length * 2 + 4:
                matching = sum(1 for a, b in zip(span, needle_tight) if a == b)
                score = matching / length
                best = max(best, score)
            if best >= 0.98:
                return best
            end = haystack.find(last, end + 1)
        start = haystack.find(first, start + 1)
    return best
