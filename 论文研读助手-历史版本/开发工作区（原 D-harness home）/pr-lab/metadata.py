"""Bibliographic metadata from *already extracted* PDF text (stdlib only).

Why this module exists
----------------------
``server.py``'s ``import_pdf`` currently derives only ``title`` from the file name
and leaves ``authors`` / ``year`` / ``venue`` / ``doi`` / ``citation`` empty.  This
module fills those fields from the app's own extraction cache
(``data/extracted/<paper_id>.json``) without any network call and without touching
the PDF again.

Public API
----------
    parse_metadata(pages, filename="")             -> dict   # exactly 6 keys
    parse_metadata_file(extracted_json_path, ...)  -> dict

``pages`` is the decoded ``pages`` list of the cache, but a whole cache dict, a
path to one, or a JSON string are accepted as well.  Both functions accept an
optional keyword-only ``report`` dict which they fill with per-field provenance
and confidence; the six returned keys are never extended.

Returned keys: ``title``, ``authors``, ``year``, ``venue``, ``doi``, ``citation``
Caps: title <= 400, authors <= 300, venue <= 100, doi <= 200, year = 4 digits.
A field is ``""`` whenever the evidence is not good enough - missing is always
better than invented.

The app's front matter is dirty on purpose
-----------------------------------------
All four real caches in ``data/extracted`` were produced by the pure-Python
``builtin`` backend (the isolated child process cannot see ``pdftotext`` because
``pdf_worker.scrub_environment`` reduces ``PATH`` to the interpreter directory plus
``System32``).  That backend both *drops* glyphs it cannot map and *substitutes*
wrong glyph names, so the front matter really looks like this:

    scar Jord / Moritz Schularick / Alan M. Taylor / Felix Ward   # "Oscar Jorda"
    By Robert W. Hahn, ..., and Befln Sprung-Keyser*              # "Benjamin"
    By Thomas Piketty, Li Yang, and Gabriel Zucmanfi*             # + a stray "fi"

Heuristics (and where each one breaks)
-------------------------------------
*navigation* - only the first ``MAX_FRONT_PAGES`` pages are read, and of those only
the first ``MAX_LINES_PER_PAGE`` lines.  Failure mode: a cover page longer than the
cap, or front matter pushed to page 4+, is missed.

*furniture* - mastheads, DOIs/URLs/e-mails, ``NBER WORKING PAPER SERIES``,
``Working Paper 24677``, addresses, ``(JEL ...)``, ``ABSTRACT``, ``©`` lines and bare
page numbers are classified as furniture before anything else looks at them, so a
masthead is never mistaken for a title.  Failure mode: a journal whose name contains
a digit, or a publisher line not in the table, is treated as prose.

*title* - the first run of early lines that are neither furniture nor an author
block nor prose.  Two-line titles are joined (a trailing hyphen joins without a
space); trailing footnote marks (``* † ‡ 1``) are stripped.  A line only ends the
title when it starts an author block, is an explicit ``By ...`` byline, or is
furniture/prose.  Failure mode: a title whose last line has exactly two capitalised
words and is followed by an abstract heading can be truncated (a single "name-shaped"
line is only an author when it has <= 2 tokens and is followed by a strong boundary).
An ALL-CAPS title is re-cased only when (a) the same title appears in mixed case
elsewhere in the front matter (preferred), or (b) ``_smart_title_case`` applies.  Inside
an ALL-CAPS title *every* token is upper case, so being upper case proves nothing: a
token keeps its case only when it contains a digit, is in a small known-acronym list,
or is used in upper case somewhere else in the front matter (which is how "GDP" and
"NBER" survive while "RISK" becomes "Risk").

*authors* - four sources in priority order: an explicit ``By ...`` byline (possibly
continued on the next line), a block of byline lines directly under the title, the
``© 2018 by A, B, and C`` copyright line, and nothing else.  Without a ``By`` marker a
block is accepted in exactly three shapes: one line that is already a comma/``and``-
separated list of >= 2 valid names with no fragment dropped and that is *immediately
followed* by page furniture, a date, an abstract heading, a footnote line or a byline
marker; a run of >= 2 consecutive single-name lines; or a lone <= 2-token name line
followed by one of those strong boundaries (or ending the front matter).  The follower
requirement is the precision guard that keeps a title continuation such as
"Firm Heterogeneity, Credit Constraints / and the Credit Channel" a title.
Names are split on ``,``, ``;``, `` and ``, `` & `` and CJK separators; each candidate
must look like a person: 1-5 alphabetic tokens, no digits, no affiliation word, and a
capitalised surname.  A lowercase *first* token is tolerated (that is exactly how
"Oscar Jorda" survives as "scar Jord"), but a lowercase surname is not - so
"Comments and suggestions" can never become an author.  A trailing ``fi``/``fl``/
``ffi``/``ffl`` ligature artifact is stripped from a token of >= 5 characters
("Zucmanfi" -> "Zucman"); this is the one rewrite, it is reported in
``report["fields"]["authors"]["artifact_stripped"]``, and its false-positive risk is a
real surname ending in those two letters.  The file name is *never* used for authors.
Failure modes: a byline printed in small caps, a "Surname, Given" list, a byline that
flows straight into the abstract paragraph with no heading, or a name whose accented
first letter was dropped (a dropped *initial* is silent) is rejected or incomplete;
when nothing passes the confidence gate, authors is ``""``.

*year* - only explicit publication-date evidence counts, in this order: ``© 2019`` /
``Copyright 2019``, a page-1 masthead (``American Economic Review 2019, 109(7): ...``),
an arXiv id (``arXiv:2401.12345`` -> 2024), a CJK date (``2 0 23 年 12 月`` -> 2023), a
month+year line (``June 2018``), then the single distinct year among the remaining
front-matter *furniture* lines, then the file name.  Years inside URLs/DOIs and inside
bare page-number lines are removed first, and a year in the title or abstract (for
example the subject period "1978-2015") is never used, because the title/body is not a
date source.  Failure mode: a paper with no copyright, no masthead date and no arXiv id
returns ``""`` instead of a plausible-looking guess.

*venue* - NBER header -> ``NBER Working Paper No. 24677``; arXiv id ->
``arXiv preprint arXiv:2401.12345``; otherwise a page-1 masthead
(``Journal Vol(Issue): pages`` or ``Vol. 12, No. 3``) -> the journal name; a bare
``Working Paper 1234`` / ``Discussion Paper`` number; ``SSRN``.  Volume/issue/pages are
reported in ``report["extras"]`` but are not glued into the venue.  Failure mode: a
journal printed without a volume/issue or without a year is not recognised.

*doi* - ``(?<![\\w.])10\\.\\d{4,9}/[^\\s"'<>]+`` over the front matter, first match
wins (topmost), with a ``doi:`` / ``https://doi.org/`` prefix accepted and trailing
punctuation stripped (balanced parentheses are kept, they can be part of a DOI).
Never derived from the file name.

*citation* - assembled from the parts that were actually parsed:
``"Authors (Year). Title. Venue. https://doi.org/<doi>"``, skipping absent parts and
ending with a period only when the last part is not a DOI URL.  A bare title is not a
citation, so at least one of authors/year/venue/doi must exist or ``citation`` is ``""``.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

__all__ = ["parse_metadata", "parse_metadata_file", "RESULT_KEYS", "CAPS",
           "SCHEMA_VERSION", "MAX_FRONT_PAGES", "DOI_URL_PREFIX"]

SCHEMA_VERSION = 1
RESULT_KEYS = ("title", "authors", "year", "venue", "doi", "citation")
CAPS = {"title": 400, "authors": 300, "year": 4, "venue": 100, "doi": 200}
DOI_URL_PREFIX = "https://doi.org/"

MAX_FRONT_PAGES = 3          # front matter lives on the cover; 3 pages is generous
MAX_LINES_PER_PAGE = 60      # the title/byline/abstract always sit at the top
MAX_TITLE_LINES = 3          # a title longer than three lines is not a title
AUTHOR_RUN_LIMIT = 6         # consecutive byline lines we are willing to absorb

# --- confidence vocabulary -------------------------------------------------
HIGH, MEDIUM, LOW, NONE = "high", "medium", "low", "none"

# --- regular expressions ---------------------------------------------------
_DOI_RE = re.compile(r"(?<![\w.])10\.\d{4,9}/[^\s\"'<>]+")
_DOI_PREFIX_RE = re.compile(r"(?:https?://(?:dx\.)?doi\.org/|doi\s*[:=]\s*)", re.I)
_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.I)
_EMAIL_RE = re.compile(r"\S+@\S+")
_MONTH_YEAR_RE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s*,?\s+((?:19|20)\d{2})\b", re.I)
_COPYRIGHT_RE = re.compile(r"(?:©|\(c\)|copyright)\s*(?:19|20)?((?:19|20)\d{2})", re.I)
_CJK_YEAR_RE = re.compile(r"((?:\d[ \t]*){4})\s*年")
_CJK_MONTH_RE = re.compile(r"(\d{1,2})\s*月")
_YEAR_RE = re.compile(r"(?<![\d.])((?:19|20)\d{2})(?![\d])")
_MASTHEAD_A = re.compile(
    r"^(?P<journal>[A-Z][^\d]{2,60}?)\s+(?P<year>(?:19|20)\d{2})\s*,\s*"
    r"(?P<volume>\d{1,4})\s*\(\s*(?P<issue>\d{1,3})\s*\)\s*[:.]?\s*"
    r"(?P<pages>\d{1,5}\s*[–\-]\s*\d{1,5})?\s*$")
_MASTHEAD_B = re.compile(
    r"^(?P<journal>[A-Z][^\d]{2,60}?)\s*,?\s*(?:Vol\.?|Volume)\s*(?P<volume>\d{1,4})"
    r"\s*(?:[,;]\s*)?(?:(?:No\.?|Number|Issue)\s*(?P<issue>\d{1,4}))?\s*[\(\[]?\s*"
    r"(?P<year>(?:19|20)\d{2})?\s*[\)\]]?", re.I)
_NBER_HEADER_RE = re.compile(r"NBER\s+WORKING\s+PAPER\s+SERIES|"
                             r"NATIONAL\s+BUREAU\s+OF\s+ECONOMIC\s+RESEARCH", re.I)
_NBER_PAPER_RE = re.compile(
    r"(?:NBER\s+)?Working\s+Paper\s*(?:No\.?|Number|#)?\s*([A-Z]?\d{1,6})\b", re.I)
_REPORT_NO_RE = re.compile(
    r"\b(Working Paper|Discussion Paper|Technical Report|Research Report|Policy Paper)"
    r"\s*(?:No\.?|Number|#)?\s*(\d{1,6})\b")
_ARXIV_ID_RE = re.compile(r"arXiv\s*:\s*(\d{2})(\d{2})\.(\d{4,5})(?:v\d+)?", re.I)
_SSRN_RE = re.compile(r"\bSSRN\b|ssrn\.com/abstract", re.I)
_BYLINE_RE = re.compile(r"^(?:by|By|BY)\b[\s:：]+(?P<rest>\S.*)$")
_JEL_RE = re.compile(r"^\(?\s*JEL\b|^JEL\s+(?:codes?|classification)", re.I)
_ABSTRACT_RE = re.compile(r"^\s*(?:abstract|ABSTRACT|Abstract|摘要|内容提要)\b[\s:：.]*$")
_PAGE_NUMBER_RE = re.compile(r"^[\s\-–—·|/]*\d{1,4}[\s\-–—·|/]*$")
_ADDRESS_RE = re.compile(r"^\d{1,5}\s+\S+.*\b(?:Avenue|Street|Road|Drive|Boulevard|"
                         r"Suite|Floor)\b|^[A-Z][A-Za-z .]+,\s*[A-Z]{2}\s*\d{5}")
# "* Author: Department of ..." / "† Affiliation" style footnote lines
_FOOTNOTE_LINE_RE = re.compile(r"^[*†‡§¶]|^[A-Z][A-Za-z.\- ]{2,30}:\s")
_FURNITURE_RE = re.compile(
    r"^(?:NBER\s+WORKING\s+PAPER\s+SERIES|NATIONAL\s+BUREAU\s+OF\s+ECONOMIC\s+RESEARCH|"
    r"Working\s+Paper\b|NBER\s+Working\s+Paper\b|Discussion\s+Paper\b|"
    r"Electronic\s+copy\s+available\s+at|Downloaded\s+from|Provided\s+in\s+cooperation|"
    r"All\s+rights\s+reserved|This\s+version\s*:|First\s+draft\b|"
    r"Preliminary\s+(?:draft|version)\b|Keywords?\s*[:：]|JEL\s|DOI\s*[:：]|"
    r"doi\s*[:：]|https?://|www\.|Copyright\b|©|\(c\)\s*(?:19|20)\d{2}|"
    r"Go\s+to\s+https?://|Published\s+by\b|Available\s+online\s+at\b|"
    r"arXiv\s*[:.]|Contents\s*$|References\s*$|Appendix\s*$)", re.I)

# --- vocabulary ------------------------------------------------------------
_FUNCTION_WORDS = {
    "a", "an", "the", "and", "or", "of", "in", "on", "at", "to", "for", "with",
    "from", "by", "as", "is", "are", "was", "were", "be", "been", "we", "our",
    "this", "that", "these", "those", "it", "its", "not", "no", "but", "if",
    "when", "where", "which", "who", "how", "why", "what", "than", "then", "there",
    "here", "also", "such", "can", "will", "would", "should", "may", "might",
    "have", "has", "had", "do", "does", "did", "using", "used", "use", "study",
    "paper", "results", "evidence", "data", "analysis", "between", "into", "over",
}
# A token anywhere in a candidate name that makes it an affiliation, not a person.
_AFFILIATION_WORDS = {
    "university", "universite", "universitat", "universidad", "department", "dept",
    "institute", "institution", "school", "college", "faculty", "center", "centre",
    "bureau", "foundation", "laboratory", "lab", "hospital", "clinic", "corporation",
    "company", "group", "inc", "ltd", "llc", "gmbh", "email", "e-mail", "http",
    "https", "www", "nber", "ssrn", "arxiv", "federal", "reserve", "national",
    "academy", "society", "association", "ministry", "bank", "press", "journal",
    "editor", "editors", "referee", "referees", "abstract", "keywords", "jel",
    "research", "economics", "economic", "studies", "sciences", "science", "policy",
    "impacts", "thanks", "thank", "acknowledge", "acknowledgements", "comments",
    "suggestions", "support", "grant", "funding", "views", "responsibility",
    "working", "paper", "discussion", "report", "chapter", "volume", "issue",
    "received", "accepted", "published", "revised", "available", "online",
}
_PARTICLES = {"van", "von", "de", "del", "della", "der", "den", "dos", "da", "di",
              "du", "la", "le", "el", "al", "bin", "ibn", "ter", "ten", "op", "st",
              "mac", "mc", "o", "van't", "zu", "zur", "af", "av"}
_SUFFIXES = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv", "v", "vi", "esq",
             "phd", "ph.d.", "md", "2nd", "3rd", "4th"}
_SMALL_TITLE_WORDS = {
    "a", "an", "and", "as", "at", "but", "by", "for", "from", "in", "into", "nor",
    "of", "on", "onto", "or", "over", "per", "the", "to", "up", "upon", "via",
    "with", "vs", "versus", "among", "between", "during", "under", "without",
}
# Acronyms longer than four letters that must survive ALL-CAPS -> Title Case.
_KNOWN_ACRONYMS = {
    "NATO", "OPEC", "COVID", "LASSO", "NASH", "ARIMA", "GARCH", "VAR", "OLS",
    "GMM", "DID", "RDD", "LLM", "LLMS", "EU", "US", "UK", "UN", "IMF", "WTO",
    "OECD", "GDP", "JEL", "NBER", "SSRN", "CO2", "ESG", "CEO", "CFO", "SME",
    "IPO", "FED", "ECB", "FDA", "NIH", "NSF", "EPA", "IEA", "WTO", "ASCII",
    "JSON", "HTML", "HTTP", "API", "SQL", "CSS", "PDF", "URL", "AI", "ML",
}
_LIGATURE_TAILS = ("ffi", "ffl", "fi", "fl")
_NAME_TOKEN_RE = re.compile(r"^[^\W\d_](?:[^\W\d_]|['\u2019.\-])*$")
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_MARK_CHARS = "*†‡§¶#^"
#: Trailing affiliation markers on a byline: a lone lowercase letter, a digit group, or a
#: symbol cluster, optionally followed by punctuation ("Jane Doe a,∗" / "John Smith 1,2").
_AFFILIATION_TAIL_RE = re.compile(
    r"(?:\s+(?:[a-z]|\d{1,2}(?:\s*,\s*\d{1,2})*))[,;]?$")
_MULTISPACE_RE = re.compile(r"\s+")


# ===========================================================================
# public API
# ===========================================================================
def parse_metadata(pages, filename="", *, report=None):
    """Return ``{title, authors, year, venue, doi, citation}`` for extracted pages.

    ``pages``  - decoded cache ``pages`` list, a whole cache dict with a ``"pages"``
                 key, a path to a cache JSON file, or a JSON string of one.
    ``filename`` - the original PDF file name; a *weak* signal used only for the year
                 and the venue (and for the title if the page text is unusable).
    ``report`` - optional dict, filled in place with provenance/confidence detail.
                 It never changes the six returned keys.
    """
    page_texts = _as_page_texts(pages)
    rep = report if isinstance(report, dict) else {}
    rep.clear()
    rep.update({"schema": SCHEMA_VERSION,
                "source": {"pages": len(page_texts),
                           "pages_with_text": sum(1 for t in page_texts if t.strip()),
                           "front_pages_used": min(len(page_texts), MAX_FRONT_PAGES)},
                "fields": {}, "extras": {}, "warnings": []})
    fields = rep["fields"]
    extras = rep["extras"]
    warnings = rep["warnings"]

    result = {"title": "", "authors": "", "year": "", "venue": "", "doi": "",
              "citation": ""}
    lines = _front_lines(page_texts)
    # A publisher cover puts the journal name and a volume line above the article title; those
    # lines are removed here and come back as the most reliable venue/year/DOI source. The
    # trigger is a *cover banner* (ScienceDirect, a homepage line, a VOL. head, an ISSN), never
    # a bare doi.org link: a first page whose running head is followed by its DOI URL is a
    # normal journal page, and stripping it used to lose the venue.
    lines, cover = _strip_cover_masthead(lines)
    if cover["stripped"]:
        extras["cover_masthead_lines"] = cover["stripped"]
    fname = _decode_filename(filename)

    # --- DOI first: it is the most reliable single signal -------------------
    doi, doi_meta = _detect_doi(lines)
    if not doi and cover["doi"]:
        doi = cover["doi"]
        doi_meta = {"confidence": HIGH, "source": "cover masthead", "value": doi}
    result["doi"] = _cap(doi, CAPS["doi"])
    fields["doi"] = doi_meta

    # --- title -------------------------------------------------------------
    title, title_meta = _detect_title(lines, fname)
    result["title"] = _cap(title, CAPS["title"])
    fields["title"] = title_meta
    warnings.extend(title_meta.pop("warnings", []))

    # --- authors, anchored just below the title ----------------------------
    authors, authors_meta = _detect_authors(lines, title_meta.get("end_index"))
    result["authors"] = _cap(authors, CAPS["authors"])
    fields["authors"] = authors_meta
    warnings.extend(authors_meta.pop("warnings", []))

    # --- year and venue ----------------------------------------------------
    # A cover masthead states the publication year and the journal; both win over anything the
    # body text suggests (the body's first year is often the submission date).
    year, year_meta = _detect_year(lines, fname)
    if cover["year"]:
        year = cover["year"]
        year_meta = {"confidence": HIGH, "source": "cover masthead", "value": year}
    result["year"] = year if re.fullmatch(r"(?:19|20)\d{2}", year or "") else ""
    fields["year"] = year_meta
    warnings.extend(year_meta.pop("warnings", []))

    venue, venue_meta, venue_extras = _detect_venue(lines, fname)
    if cover["venue"]:
        venue = cover["venue"]
        venue_meta = {"confidence": HIGH, "source": "cover masthead", "value": venue}
    result["venue"] = _cap(venue, CAPS["venue"])
    fields["venue"] = venue_meta
    warnings.extend(venue_meta.pop("warnings", []))
    extras.update(venue_extras)

    # --- citation from parsed parts only -----------------------------------
    result["citation"] = _compose_citation(result)
    fields["citation"] = {"confidence": _weakest(
        fields, tuple(key for key in ("authors", "year", "title", "venue", "doi")
                      if result[key])),
        "source": "composed",
        "parts": [key for key in ("authors", "year", "title", "venue", "doi")
                  if result[key]]}
    return result


def parse_metadata_file(extracted_json_path, filename="", *, report=None, strict=False):
    """Load ``data/extracted/<paper_id>.json`` and delegate to :func:`parse_metadata`.

    A missing or corrupt cache returns empty fields (and a warning in ``report``)
    unless ``strict=True``, so an import can never fail because of metadata.
    """
    rep = report if isinstance(report, dict) else {}
    try:
        with Path(extracted_json_path).open(encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            raise ValueError("缓存顶层不是对象")
    except (OSError, ValueError) as exc:
        if strict:
            raise
        result = parse_metadata([], filename, report=rep)
        rep["warnings"].append(f"读取抽取缓存失败：{type(exc).__name__}: {exc}")
        return result
    return parse_metadata(payload, filename, report=rep)


# ===========================================================================
# input normalisation
# ===========================================================================
def _as_page_texts(pages):
    """Accept a cache dict / path / JSON string / page list; return page strings."""
    if pages is None:
        return []
    if isinstance(pages, dict):
        return _as_page_texts(pages.get("pages"))
    if isinstance(pages, (str, bytes, os.PathLike)):
        if isinstance(pages, bytes):
            pages = pages.decode("utf-8", "replace")
        if isinstance(pages, str):
            candidate = pages.strip()
            if candidate.startswith("{"):
                try:
                    return _as_page_texts(json.loads(candidate))
                except ValueError:
                    return []
            if os.path.exists(candidate):
                return _as_page_texts(_read_json(candidate))
            return []
        return _as_page_texts(_read_json(pages))
    texts = []
    if isinstance(pages, (list, tuple)):
        for page in pages:
            if isinstance(page, dict):
                text = page.get("text")
            elif isinstance(page, str):
                text = page
            else:
                text = getattr(page, "text", None)
            texts.append(text if isinstance(text, str) else "")
    return texts


def _read_json(path):
    try:
        with Path(path).open(encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _decode_filename(filename):
    if not filename:
        return ""
    name = str(filename).replace("\\", "/").rsplit("/", 1)[-1]
    if "%" in name:
        try:
            from urllib.parse import unquote
            name = unquote(name)
        except (ValueError, TypeError):
            pass
    return name


# ===========================================================================
# line helpers
# ===========================================================================
def _front_lines(page_texts):
    """[(page_index, line_index, text)] for the top of the first pages."""
    out = []
    for page_index, text in enumerate(page_texts[:MAX_FRONT_PAGES]):
        if not text:
            continue
        for line_index, raw in enumerate(text.split("\n")[:MAX_LINES_PER_PAGE]):
            line = _squash(raw)
            if line:
                out.append((page_index, line_index, line))
    return out


# --- publisher cover mastheads ---------------------------------------------
# A journal article's page 1 is often a publisher cover: the journal name, a volume/issue/page
# line, "Contents lists available at ScienceDirect", a homepage URL - and only then the article
# title. Without stripping those lines the title scan glued them together and recorded the
# journal name as the paper title, which is a reported defect. The stripped lines are also the
# most reliable place to read the venue and the publication year from, so they are not dropped:
# they come back as hints.
_COVER_BANNER_RE = re.compile(
    r"^(?:contents lists available at|available online at|journal homepage\s*:|"
    r"science ?direct|check for updates|see discussions, stats|"
    r"frontiers in|elsevier|springer|wiley online library)", re.I)
_VOLUME_HEAD_RE = re.compile(r"^(?:vol\.?|volume)\s*[ivxlcdm\d]", re.I)
_ISSN_RE = re.compile(r"^(?:issn|isbn)\b", re.I)
# "Journal of Monetary Economics 152 (2024) 103-126"
_JOURNAL_VOLUME_RE = re.compile(
    r"^(?P<journal>[A-Z][A-Za-z&'\u2019.\- ]{3,70}?)\s+"
    r"(?:\d{1,4}|[IVXLCDM]{1,6})\s*"
    r"\((?P<year>(?:19|20)\d{2})\)"
    r"(?:\s*\d{1,5}\s*[\u2013\u2014-]\s*\d{1,5})?$")
# "The Journal of Finance" / "Journal of Public Economics" and similar standalone lines.
_JOURNAL_NAME_RE = re.compile(
    r"^(?:the\s+)?[A-Z][A-Za-z&'\u2019.\-]*(?:\s+(?:of|and|for|the|in|on|&|[A-Z][A-Za-z'\u2019.\-]*))*$")
_JOURNAL_WORDS = ("journal", "review", "economics", "economic", "letters", "proceedings",
                  "transactions", "studies", "quarterly", "annals", "bulletin", "science",
                  "nature", "cell", "finance", "management", "psychology", "sociology",
                  "medicine", "policy", "research", "statistics", "mathematics", "physics")


def _looks_like_journal_name(text):
    """True for a line that names a journal on a cover page (conservative)."""
    if not text or len(text) > 90 or _CJK_RE.search(text):
        return False
    if re.search(r"\d", text) or not _JOURNAL_NAME_RE.match(text):
        return False
    lowered = text.lower()
    return any(word in lowered for word in _JOURNAL_WORDS)


def _strip_cover_masthead(lines, window=25):
    """Remove a publisher cover masthead. Returns ``(lines, hints)``.

    Only acts when the top of the document actually carries cover markers, so a normal first
    page (title, authors, abstract) is left untouched. ``hints`` carries the venue, the year
    and a DOI read from the removed lines.
    """
    hints = {"venue": "", "year": "", "doi": "", "stripped": 0}
    if not lines:
        return lines, hints
    head = lines[:window]
    markers = [index for index, (_page, _line, text) in enumerate(head)
               if _COVER_BANNER_RE.match(text) or _VOLUME_HEAD_RE.match(text)
               or _ISSN_RE.match(text)]
    if not markers:
        return lines, hints
    cut = markers[-1]
    if cut + 1 >= len(head):
        return lines, hints
    stripped = head[:cut + 1]

    for _page, _line, text in stripped:
        match = _JOURNAL_VOLUME_RE.match(text)
        if match:
            hints["venue"] = hints["venue"] or match.group("journal").strip()
            hints["year"] = hints["year"] or match.group("year")
            continue
        if not hints["year"]:
            years = [int(value) for value in re.findall(r"\b(?:19|20)\d{2}\b", text)]
            if years:
                hints["year"] = str(max(years))
    if not hints["venue"]:
        for _page, _line, text in stripped:
            if _looks_like_journal_name(text):
                hints["venue"] = text
                break
    if not hints["venue"] and len(stripped) < len(head):
        # Elsevier prints the journal name again directly above the homepage line.
        candidate = head[cut][2] if _COVER_BANNER_RE.match(head[cut][2]) else ""
        if not candidate and cut:
            candidate = head[cut - 1][2]
        if _looks_like_journal_name(candidate):
            hints["venue"] = candidate
    if not hints["doi"]:
        doi, _meta = _detect_doi(stripped)
        hints["doi"] = doi

    hints["stripped"] = cut + 1
    return lines[cut + 1:], hints


def _squash(text):
    text = text.replace("\u00ad", "").replace("\ufeff", "").replace("\u200b", "")
    return _MULTISPACE_RE.sub(" ", text.replace("\xa0", " ")).strip()


def _squash_cjk(text):
    """Drop the spaces a broken extractor inserts between CJK glyphs."""
    if not _CJK_RE.search(text):
        return text
    keep = []
    for index, char in enumerate(text):
        if char == " ":
            before = text[index - 1] if index else ""
            after = text[index + 1] if index + 1 < len(text) else ""
            connector = "˙·•"
            if (before and (bool(_CJK_RE.match(before)) or before in connector)
                    and after and (bool(_CJK_RE.match(after)) or after in connector)):
                continue
        keep.append(char)
    return _MULTISPACE_RE.sub(" ", "".join(keep)).strip()


def _key(text):
    return _MULTISPACE_RE.sub(" ", re.sub(r"[^\w\u4e00-\u9fff]+", " ", text.lower())).strip()


def _strip_marks(text):
    return text.strip().strip(_MARK_CHARS).strip()


def _is_furniture(line):
    if not line:
        return True
    if _PAGE_NUMBER_RE.match(line):
        return True
    if _ABSTRACT_RE.match(line):
        return True
    if _JEL_RE.match(line):
        return True
    if _URL_RE.search(line) or _EMAIL_RE.search(line):
        return True
    if _FURNITURE_RE.match(line):
        return True
    if _parse_masthead(line):          # "American Economic Review 2019, 109(7): ..."
        return True
    if _ADDRESS_RE.match(line):
        return True
    if len(line) < 3:
        return True
    letters = sum(1 for char in line if char.isalpha())
    return letters == 0


def _is_prose(line):
    """A body-text line: long, wordy, sentence-like."""
    if len(line) >= 170:
        return True
    if len(line) >= 90 and line.count(" ") >= 12 and not line.endswith((":", "：")):
        return True
    return False


def _join_title_lines(parts):
    out = ""
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if not out:
            out = part
        elif out.endswith("-") and part[:1].islower():
            out = out[:-1] + part          # hyphenated word split across lines
        elif " " in part:
            out += " " + part
        elif _CJK_RE.search(out[-2:]) and _CJK_RE.search(part[:2]):
            out += part                     # CJK: no space between lines
        else:
            out += " " + part
    return _MULTISPACE_RE.sub(" ", out).strip()


# ===========================================================================
# DOI
# ===========================================================================
def _detect_doi(lines):
    meta = {"confidence": NONE, "source": "", "value": ""}
    for page_index, line_index, line in lines:
        for match in _DOI_RE.finditer(line):
            doi = _trim_doi(match.group(0))
            if len(doi) < 8 or "/" not in doi or not any(c.isalnum() for c in doi.split("/", 1)[1]):
                continue
            meta.update({"confidence": HIGH if _DOI_PREFIX_RE.search(line) or
                          page_index == 0 else MEDIUM,
                          "source": f"page {page_index + 1} line {line_index + 1}",
                          "value": doi})
            return doi, meta
    return "", meta


def _trim_doi(doi):
    doi = doi.strip().rstrip("\u200b")
    while doi and doi[-1] in ".,;:!?\"'<>*":
        doi = doi[:-1]
    # A closing parenthesis may belong to the DOI itself; only drop it when unbalanced.
    while doi.endswith(")") and doi.count("(") < doi.count(")"):
        doi = doi[:-1]
    while doi.endswith("]") and doi.count("[") < doi.count("]"):
        doi = doi[:-1]
    return doi


# ===========================================================================
# title
# ===========================================================================
def _detect_title(lines, filename):
    meta = {"confidence": NONE, "source": "", "value": "", "warnings": [],
            "all_caps": False, "end_index": None}
    if not lines:
        meta.update(_filename_title(filename))
        return meta["value"], meta

    start = None
    for index, (_page, _line, text) in enumerate(lines[:12]):
        if _is_furniture(text) or _is_date_line(text):
            continue
        start = index
        break
    if start is None:
        meta.update(_filename_title(filename))
        if not meta["value"]:
            meta["warnings"].append("前 12 行全部是页眉/页脚，未能定位标题")
        return meta["value"], meta

    block, end = [], start - 1
    for index in range(start, min(len(lines), start + 12)):
        page, line_index, text = lines[index]
        if block and (_is_furniture(text) or _is_date_line(text)):
            break
        if not block and (_is_furniture(text) or _is_date_line(text)):
            continue
        if _is_prose(text) and block:
            break
        if block and _starts_author_run(lines, index):
            break
        block.append(text)
        end = index
        if len(block) >= MAX_TITLE_LINES:
            break

    cleaned = []
    for index, part in enumerate(block):
        part = _clean_title_line(part)
        if part:
            cleaned.append(part)
    candidate = _squash_cjk(_join_title_lines(cleaned)).strip(" ,;:")
    if not candidate:
        meta.update(_filename_title(filename))
        meta["warnings"].append("标题块为空，标题取自文件名" if meta["value"]
                                else "未能从页面文本或文件名中确定标题")
        return meta["value"], meta
    if not _title_is_plausible(candidate):
        meta.update(_filename_title(filename))
        if meta["value"]:
            meta["warnings"].append("页面文本没有可用的标题块，标题取自文件名")
        else:
            meta["warnings"].append("未能从页面文本或文件名中确定标题")
        return meta["value"], meta

    meta.update({"value": candidate, "end_index": end,
                 "source": f"page {lines[start][0] + 1} line {lines[start][1] + 1}"
                           f"{'-' + str(lines[end][1] + 1) if end != start else ''}",
                 "confidence": HIGH if end == start or len(block) > 1 else MEDIUM})
    meta["all_caps"] = _is_all_caps(candidate)
    if meta["all_caps"]:
        mixed = _find_mixed_case_twin(lines, candidate)
        if mixed:
            meta["value"] = mixed
            meta["source"] += "（同一标题的小写渲染）"
        else:
            caps_words = _caps_words(lines, skip=set(range(start, end + 1)))
            meta["value"] = _smart_title_case(candidate, caps_words)
            meta["confidence"] = MEDIUM
            meta["warnings"].append("标题全大写且没有小写副本，已按保守规则转换大小写")
    meta["end_index"] = end
    return meta["value"], meta


def _clean_title_line(line):
    line = _strip_marks(line)
    line = re.sub(r"\s*[\(\[]\s*(?:19|20)\d{2}\s*[\)\]]\s*$", "", line)  # trailing year
    line = re.sub(r"^arXiv\s*:\s*\S+\s*(?:\[[^\]]*\]\s*)?(?:\d{1,2}\s+\w{3,9}\s+\d{4}\s*)?",
                  "", line, flags=re.I)                                   # arXiv stamp
    return line.strip(" ,;")


def _is_all_caps(text):
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 6:
        return False
    return all(c.isupper() for c in letters) and len(text.split()) >= 2


def _title_is_plausible(text):
    if not text or len(text) < 8 or len(text) > CAPS["title"]:
        return False
    if not any(c.isalpha() for c in text):
        return False
    if _EMAIL_RE.search(text) or _URL_RE.search(text) or "10." in text and "/" in text:
        return False
    if _is_furniture(text):
        return False
    return True


def _find_mixed_case_twin(lines, title):
    """Prefer a mixed-case rendering of the same ALL-CAPS title, if one exists."""
    key = _key(title)
    if not key:
        return ""
    for _page, _line, text in lines:
        if text == title or not any(c.islower() for c in text):
            continue
        if _key(text) == key and len(text) >= 8:
            return _strip_marks(text)
    return ""


def _keeps_caps(token, caps_context=None):
    """True for tokens that must not be re-cased (acronyms, numbers, units).

    Inside an ALL-CAPS title every token is upper case, so "it is upper case" is no
    evidence at all.  An acronym therefore has to prove itself: it contains a digit,
    it is in ``_KNOWN_ACRONYMS``, or the front matter uses the same token in upper
    case *outside* the title (as running text does for GDP, DSGE, NBER ...).  A word
    such as RISK never passes, so it becomes "Risk".
    """
    core = token.strip("()[]{}\"'.,;:")
    if not core:
        return False
    if any(char.isdigit() for char in core):
        return True
    if core in _KNOWN_ACRONYMS:
        return True
    if core.lower() in _SMALL_TITLE_WORDS:
        return False                          # THE / AND / OF are words, not acronyms
    if caps_context is None:                  # file-name slug: caps were typed by hand
        return core.isupper() and 2 <= len(core) <= 4
    return core.isupper() and core in caps_context


def _smart_title_case(text, caps_context=None):
    """Conservative ALL-CAPS -> Title Case that keeps acronyms and numbers intact."""
    words = text.split()
    out = []
    for index, word in enumerate(words):
        core = word.strip("()[]{}\"'")
        if not core:
            out.append(word)
            continue
        if core.lower() in _SMALL_TITLE_WORDS and 0 < index < len(words) - 1:
            out.append(word.lower())
            continue
        if _keeps_caps(core, caps_context):
            out.append(word)          # acronym: GDP, US, COVID-19, NBER
            continue
        out.append(_capitalise_segments(word, caps_context))
    return " ".join(out)


def _capitalise_segments(word, caps_context=None):
    parts = re.split(r"([\-\u2010-\u2015/])", word)
    out = []
    for index, part in enumerate(parts):
        if not part or re.fullmatch(r"[\-\u2010-\u2015/]+", part):
            out.append(part)
            continue
        if index > 0 and part.lower() in _SMALL_TITLE_WORDS:
            out.append(part.lower())
        elif _keeps_caps(part, caps_context):
            out.append(part)
        else:
            out.append(part[:1].upper() + part[1:].lower())
    return "".join(out)


def _caps_words(lines, skip=()):
    """Upper-case tokens used outside the title block (acronym evidence)."""
    words = set()
    for index, (_page, _line, text) in enumerate(lines):
        if index in skip:
            continue
        for token in re.findall(r"[^\W\d_][\w\-]*", text):
            if len(token) >= 2 and token.isupper():
                words.add(token)
    return words


def _filename_title(filename):
    """Weak title from the file name, used only when the page text is unusable."""
    if not filename:
        return {"value": "", "confidence": NONE, "source": "", "warnings": [],
                "all_caps": False, "end_index": None}
    stem = filename.rsplit(".", 1)[0]
    slug = re.sub(r"^(?:[a-z]+(?:[-_](?:et[-_]al|and[-_]others))?[-_])(?:19|20)\d{2}[-_]",
                  "", stem, flags=re.I)
    slug = re.sub(r"^(?:19|20)\d{2}[-_]", "", slug)
    slug = re.sub(r"[-_](?:19|20)\d{2}$", "", slug)
    words = [w for w in re.split(r"[-_\s]+", slug) if w]
    if not words:
        return {"value": "", "confidence": NONE, "source": "", "warnings": [],
                "all_caps": False, "end_index": None}
    value = _smart_title_case(" ".join(words))
    # only a plausible conversion of a real slug counts (needs enough words)
    if len(words) < 3 or not any(w.isalpha() for w in words):
        return {"value": "", "confidence": NONE, "source": "", "warnings": [],
                "all_caps": False, "end_index": None}
    return {"value": value, "confidence": LOW, "source": "文件名",
            "warnings": ["标题来自文件名（弱信号），请核对"], "all_caps": False,
            "end_index": None}


# ===========================================================================
# dates / years
# ===========================================================================
def _is_date_line(line):
    if _CJK_YEAR_RE.search(line) and _CJK_MONTH_RE.search(line):
        return True
    if _MONTH_YEAR_RE.fullmatch(line.strip(" .")):
        return True
    if re.fullmatch(r"\d{4}\s*年(?:\s*\d{1,2}\s*月(?:\s*\d{1,2}\s*日)?)?", line):
        return True
    return False


def _strip_urls(line):
    line = _URL_RE.sub(" ", line)
    line = _DOI_RE.sub(" ", line)
    return line


def _year_values(line):
    """Years in a line, minus URLs/DOIs, volume(issue) numbers and bare numbers."""
    text = _strip_urls(line)
    if _PAGE_NUMBER_RE.match(text):
        return []
    text = re.sub(r"\b\d{1,4}\s*\(\s*\d{1,3}\s*\)", " ", text)   # 109(7)
    return [match.group(1) for match in _YEAR_RE.finditer(text)]


def _plausible_year(year):
    if not re.fullmatch(r"(?:19|20)\d{2}", year or ""):
        return False
    return 1850 <= int(year) <= 2100


def _detect_year(lines, filename):
    meta = {"confidence": NONE, "source": "", "value": "", "warnings": []}
    for page, line_index, line in lines:
        match = _COPYRIGHT_RE.search(line)
        if match and _plausible_year(match.group(1)):
            meta.update({"value": match.group(1), "confidence": HIGH,
                         "source": f"版权行（page {page + 1} line {line_index + 1}）"})
            return meta["value"], meta
    for page, line_index, line in lines[:6]:
        if page > 0:
            break
        masthead = _parse_masthead(line)
        if masthead and masthead.get("year"):
            meta.update({"value": masthead["year"], "confidence": HIGH,
                         "source": f"期刊页眉（page {page + 1} line {line_index + 1}）"})
            return meta["value"], meta
    for page, line_index, line in lines:
        match = _ARXIV_ID_RE.search(line)
        if match:
            year = _arxiv_year(match.group(1))
            if year:
                meta.update({"value": year, "confidence": MEDIUM,
                             "source": f"arXiv 编号（page {page + 1} line {line_index + 1}）"})
                return meta["value"], meta
        cjk = _CJK_YEAR_RE.search(line)
        if cjk:
            digits = re.sub(r"\s", "", cjk.group(1))
            if _plausible_year(digits):
                meta.update({"value": digits, "confidence": HIGH,
                             "source": f"中文日期（page {page + 1} line {line_index + 1}）"})
                return meta["value"], meta
    for page, line_index, line in lines[:10]:
        match = _MONTH_YEAR_RE.search(line)
        if match and _plausible_year(match.group(2)):
            meta.update({"value": match.group(2), "confidence": HIGH,
                         "source": f"月份行（page {page + 1} line {line_index + 1}）"})
            return meta["value"], meta
    # weak: a single distinct year among the remaining furniture lines
    weak = []
    for page, line_index, line in lines:
        if not (_is_furniture(line) or _is_date_line(line)):
            continue
        weak.extend(y for y in _year_values(line) if _plausible_year(y))
    distinct = sorted(set(weak))
    if len(distinct) == 1:
        meta.update({"value": distinct[0], "confidence": LOW,
                     "source": "页眉/页脚中的唯一 4 位年份"})
        return meta["value"], meta
    filename_year = _year_from_filename(filename)
    if filename_year:
        meta.update({"value": filename_year, "confidence": LOW, "source": "文件名",
                     "warnings": ["年份来自文件名（弱信号），请核对"]})
        return meta["value"], meta
    meta["warnings"].append("没有找到可信的出版年份，年份留空而不是猜测")
    return "", meta


def _arxiv_year(yy):
    value = int(yy)
    year = 2000 + value if value <= 90 else 1900 + value
    return str(year) if _plausible_year(str(year)) else ""


def _year_from_filename(filename):
    if not filename:
        return ""
    for match in _YEAR_RE.finditer(filename):
        if _plausible_year(match.group(1)):
            return match.group(1)
    return ""


def _parse_masthead(line):
    for pattern in (_MASTHEAD_A, _MASTHEAD_B):
        match = pattern.match(line)
        if not match:
            continue
        groups = match.groupdict()
        journal = _strip_marks(groups.get("journal") or "").strip(" ,;:.&")
        if not journal or len(journal) < 3 or _EMAIL_RE.search(journal):
            continue
        if any(ch.isdigit() for ch in journal):
            continue
        if not groups.get("volume") and not groups.get("pages"):
            continue
        return {"journal": journal, "year": groups.get("year") or "",
                "volume": groups.get("volume") or "", "issue": groups.get("issue") or "",
                "pages": _squash(groups.get("pages") or ""), "line": line}
    return None


# ===========================================================================
# authors
# ===========================================================================
def _name_tokens(part):
    """Split one candidate name into tokens, keeping a middle initial's period."""
    out = []
    for token in re.split(r"\s+", _strip_marks(part)):
        token = token.strip(" ,;:")
        if not token:
            continue
        if re.fullmatch(r"[^\W\d_]", token.rstrip(".")):      # single-letter initial
            token = token.rstrip(".") + "."
        else:
            token = token.rstrip(".")
        out.append(token)
    return out


def _looks_like_a_name_token(token):
    return bool(_NAME_TOKEN_RE.match(token.rstrip(".")))


def _name_score(tokens, allow_caps=False):
    """>= 2 means "confidently a person's name"; -99 means never.

    `allow_caps` is set by the callers that already know the line is a byline (it carries a
    "By" marker): journals print bylines in capitals on cover pages ("By MARIA L. GARCIA and
    PETER CHEN"), and without the flag the all-caps guard below threw those names away.
    """
    if not tokens or len(tokens) > 5:
        return -99
    lowered = [t.lower().strip(".") for t in tokens]
    if any(t in _AFFILIATION_WORDS for t in lowered):
        return -99
    if any(t in _FUNCTION_WORDS for t in lowered):
        return -99
    core = tokens[:]
    if core and core[-1].lower().strip(".") in _SUFFIXES:
        core = core[:-1]
    if not core:
        return -99
    for token in core:
        if not _looks_like_a_name_token(token):
            return -99
    if not allow_caps and all(token.isupper() and len(token) > 1 for token in core):
        return -99                                    # a heading, not a byline
    last = core[-1]
    score = 0
    if last[:1].isupper():
        score += 2
    else:
        return -99                                     # surname must be capitalised
    if all(t[:1].isupper() or t.lower() in _PARTICLES for t in core):
        score += 1
    if len(core) == 1:
        score -= 3
    if 2 <= len(core) <= 3:
        score += 1
    if any(len(t.rstrip(".")) == 1 and t.endswith(".") for t in core[:-1]):
        score += 1
    return score


def _strip_ligature_tail(token):
    """'Zucmanfi' -> 'Zucman'.  Only for a long token, only at the very end."""
    stripped = False
    for tail in _LIGATURE_TAILS:
        if token.lower().endswith(tail) and len(token) - len(tail) >= 4:
            token = token[: len(token) - len(tail)]
            stripped = True
            break
    return token, stripped


def _format_name(tokens):
    out = []
    for index, token in enumerate(tokens):
        if index == len(tokens) - 1:
            token, _ = _strip_ligature_tail(token)
        out.append(token)
    return " ".join(out)


def _split_name_list(text):
    if not text:
        return []
    text = _strip_marks(text)
    text = re.sub(r"\bet\s+al\.?\b", "", text, flags=re.I)
    text = re.sub(r"\band\s+others\b", "", text, flags=re.I)
    text = text.replace(" & ", " and ")
    text = re.sub(r"([，、；])", ",", text)
    parts = re.split(r"[,;]|\s+and\s+", text)
    cleaned = []
    for part in parts:
        part = re.sub(r"^\s*(?:and|&)\s+", "", part.strip())
        # A publisher byline carries affiliation markers right after the name - "Jane Q. Author
        # a,∗", "John B. Smith 1,2", "Maria L. Garcia†". They are not part of the name, and
        # leaving them on made the whole line fail the name gate, which is how a cover page's
        # byline ended up glued into the title.
        while part:
            trimmed = _AFFILIATION_TAIL_RE.sub("", part).strip()
            if trimmed == part:
                break
            part = trimmed
        if part:
            cleaned.append(part)
    return cleaned


def _parse_name_list(text, relaxed=False, allow_caps=False):
    """-> (names, dropped, artifacts) for one candidate byline string."""
    names, dropped, artifacts = [], [], False
    for part in _split_name_list(text):
        tokens = _name_tokens(part)
        if not tokens:
            continue
        score = _name_score(tokens, allow_caps=allow_caps)
        if score >= (0 if relaxed else 2):
            formatted = _format_name(tokens)
            if formatted != " ".join(tokens):
                artifacts = True
            names.append(formatted)
        elif len(tokens) <= 4 and all(_looks_like_a_name_token(t) for t in tokens):
            dropped.append(part)
    return names, dropped, artifacts


def _is_author_follower(line):
    """A line that can *follow* a byline but cannot be the next line of a title."""
    return bool(_is_furniture(line) or _is_date_line(line) or _ABSTRACT_RE.match(line)
                or _BYLINE_RE.match(line) or _FOOTNOTE_LINE_RE.match(line)
                or _is_affiliation_line(line))


#: An affiliation block directly under a byline: "a Department of Economics, ...",
#: "1 University of Somewhere", "Corresponding author: jane@example.edu".
_AFFILIATION_HEAD_RE = re.compile(
    r"^(?:[a-z]\s*|\d{1,2}\s*)?(?:department|dept\.?|division|faculty|school|institute|"
    r"university|college|academy|bank|ministry|laborator|centre|center|graduate\s+school)\b",
    re.I)
_AFFILIATION_HINT_RE = re.compile(
    r"\b(?:university|institute|department|school of|college|corresponding author|"
    r"e-?mail|electronic address)\b|@\w+\.", re.I)


def _is_affiliation_line(text):
    """True for an author-affiliation line (the line a byline is followed by)."""
    return bool(_AFFILIATION_HEAD_RE.match(text or "")
                or _AFFILIATION_HINT_RE.search(text or ""))


def _starts_author_run(lines, index):
    """True when ``lines[index]`` begins a block of byline lines.

    Three shapes qualify, and nothing else:

    * an explicit ``By ...`` marker (never part of a title);
    * one line that is already a comma/``and``-separated list of >= 2 valid names
      with no fragment dropped, immediately followed by page furniture, a date, an
      abstract heading, a footnote line or another byline marker;
    * a run of >= 2 consecutive single-name lines (the NBER cover page).

    A lone name-shaped line is the ambiguous case (it can be the third line of a
    title), so it only qualifies when it has <= 2 tokens and is followed by one of
    those same strong boundaries.  Counting *names per line* rather than tokens is
    what lets a no-``By`` byline be recognised; the "must be followed by furniture or
    an abstract heading" guard is what keeps a title continuation such as
    "Firm Heterogeneity, Credit Constraints / and the Credit Channel" out.
    """
    if _BYLINE_RE.match(lines[index][2]):
        return True
    run = 0
    cursor = index
    while cursor < len(lines) and run < AUTHOR_RUN_LIMIT:
        text = lines[cursor][2]
        if _is_furniture(text) or _is_date_line(text) or _BYLINE_RE.match(text):
            break
        names, dropped, _artifacts = _parse_name_list(text)
        if not names or dropped:
            break
        if cursor == index and len(names) >= 2:
            following = lines[index + 1][2] if index + 1 < len(lines) else ""
            return _is_author_follower(following)
        run += 1
        cursor += 1
    if run >= 2:
        return True
    if run == 1:
        text = lines[index][2]
        if len(_name_tokens(text)) <= 2:
            if cursor >= len(lines):
                return True          # the front matter simply ends after the byline
            following = lines[cursor][2]
            if (_is_furniture(following) or _is_date_line(following)
                    or _ABSTRACT_RE.match(following) or _BYLINE_RE.match(following)):
                return True
    return False


def _absorb_byline(lines, index, first_text):
    """Join the byline marker line with the continuation lines it asks for.

    A byline is continued only while the previous part ends with ``,``, ``and`` or
    ``&`` - that is how "By Robert W. Hahn, Nathaniel Hendren,\\nRobert D. Metcalfe,
    and Befln Sprung-Keyser*" becomes one list.
    """
    parts = [first_text.strip()]
    cursor = index + 1
    while cursor < len(lines) and len(parts) < 4:
        if not parts[-1].rstrip().endswith((",", "and", "&")):
            break
        text = lines[cursor][2]
        if _is_furniture(text) or _is_date_line(text) or _is_prose(text):
            break
        names, _dropped, _artifacts = _parse_name_list(text)
        if not names:
            break
        parts.append(re.sub(r"^\s*(?:and|&)\s+", "", text.strip()))
        cursor += 1
    return " ".join(parts)


def _detect_authors(lines, title_end_index):
    meta = {"confidence": NONE, "source": "", "value": "", "warnings": [],
            "suspicious": [], "dropped": [], "artifact_stripped": False}
    if not lines:
        return "", meta

    # S1: an explicit "By ..." marker inside the front matter -----------------
    for index, (page, line_index, text) in enumerate(lines[:30]):
        match = _BYLINE_RE.match(text)
        if not match or _is_furniture(text):
            continue
        candidate = _absorb_byline(lines, index, match.group("rest"))
        names, dropped, artifacts = _parse_name_list(candidate, relaxed=True, allow_caps=True)
        if names:
            meta.update({"value": _format_names(names), "confidence": HIGH,
                         "source": f"By 署名行（page {page + 1} line {line_index + 1}）",
                         "dropped": dropped, "artifact_stripped": artifacts})
            if dropped:
                meta["confidence"] = MEDIUM
                meta["warnings"].append(f"署名行中有 {len(dropped)} 个片段没有通过姓名校验：" +
                                        "；".join(dropped))
            meta["suspicious"] = _suspicious_tokens(names)
            if meta["suspicious"]:
                meta["confidence"] = MEDIUM
            return meta["value"], meta

    # S2: a block of name-shaped lines directly under the title ---------------
    anchor = 0 if title_end_index is None else min(len(lines) - 1, title_end_index + 1)
    cursor = anchor
    budget = 0
    while cursor < len(lines) and budget < 12:
        if _starts_author_run(lines, cursor):
            names, dropped, artifacts = [], [], False
            while cursor < len(lines) and len(names) < AUTHOR_RUN_LIMIT:
                text = lines[cursor][2]
                if _is_furniture(text) or _is_date_line(text):
                    break
                found, lost, art = _parse_name_list(
                    text, allow_caps=bool(_BYLINE_RE.match(text)))
                if not found:
                    break
                names.extend(found)
                dropped.extend(lost)
                artifacts = artifacts or art
                cursor += 1
            if names:
                # _starts_author_run() already applied the strict gate; a single name
                # can only get here through one of its single-line cases, which is
                # weaker evidence than a run of names or a comma-separated list.
                meta.update({"value": _format_names(names),
                             "confidence": HIGH if len(names) >= 2 else MEDIUM,
                             "source": f"标题下方的姓名块（page {lines[anchor][0] + 1} 起）",
                             "dropped": dropped, "artifact_stripped": artifacts})
                meta["suspicious"] = _suspicious_tokens(names)
                if dropped or meta["suspicious"]:
                    meta["confidence"] = MEDIUM
                if dropped:
                    meta["warnings"].append("姓名块中有片段未通过校验，已丢弃：" +
                                            "；".join(dropped))
                return meta["value"], meta
            break
        if _is_prose(lines[cursor][2]) or _ABSTRACT_RE.match(lines[cursor][2]):
            break
        cursor += 1
        budget += 1

    # S3: the copyright line "© 2018 by A, B, and C" --------------------------
    for page, line_index, text in lines:
        if not _COPYRIGHT_RE.search(text) or " by " not in text.lower():
            continue
        tail = re.split(r"\bby\b", text, flags=re.I)[-1]
        names, dropped, artifacts = _parse_name_list(tail, relaxed=True)
        if names and len(names) >= 1:
            meta.update({"value": _format_names(names), "confidence": MEDIUM,
                         "source": f"版权行（page {page + 1} line {line_index + 1}）",
                         "dropped": dropped, "artifact_stripped": artifacts,
                         "suspicious": _suspicious_tokens(names)})
            meta["warnings"].append("作者来自版权行，可能与正式署名不一致")
            return meta["value"], meta

    meta["warnings"].append("没有找到可信的署名信息，作者留空而不是猜测")
    return "", meta


def _suspicious_tokens(names):
    """Tokens that look like the extractor dropped or substituted characters."""
    bad = []
    for name in names:
        for token in name.split():
            core = token.strip(".,")
            if not core:
                continue
            if core[:1].islower():
                bad.append(token)                     # lost capital (dropped accent)
            elif re.search(r"[bcdfghjklmnpqrstvwxz]{4,}", core.lower()):
                bad.append(token)                     # impossible consonant run
    return bad


def _recase_caps_name(name):
    """Title-case one all-caps personal name, keeping initials and particles sane."""
    small = {"and", "of", "the", "de", "van", "von", "der", "la", "le", "dos", "da", "del"}
    words = name.split()
    out = []
    for index, word in enumerate(words):
        core = word.strip("()[]{}\"'")
        if not core:
            out.append(word)
            continue
        if len(core.rstrip(".")) == 1:                       # a middle initial: M. / L.
            out.append(core.rstrip(".").upper() + ("." if core.endswith(".") else ""))
            continue
        if core.lower() in small and 0 < index < len(words) - 1:
            out.append(core.lower())
            continue
        out.append("-".join(part.capitalize() for part in core.split("-")))
    return " ".join(out)


def _tidy_names(names):
    """Re-case a byline that the journal printed in capitals, so the library does not shout.

    Cover pages of some journals print the byline in all caps ("By MARIA L. GARCIA and PETER
    CHEN"). The names are kept and only their casing is normalised, so initials and particles
    survive while nothing is guessed.
    """
    if names and all(_is_all_caps(name) for name in names):
        return [_recase_caps_name(name) for name in names]
    return names


def _format_names(names):
    names = _tidy_names(names)
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return ", ".join(names[:-1]) + f", and {names[-1]}"


# ===========================================================================
# venue
# ===========================================================================
def _detect_venue(lines, filename):
    meta = {"confidence": NONE, "source": "", "value": ""}
    extras = {}
    head = lines[:14]

    nber_header = any(_NBER_HEADER_RE.search(text) for _p, _l, text in head)
    paper_no = ""
    nber_numbered = False
    for _page, _line, text in head:
        match = _NBER_PAPER_RE.search(text)
        if match:
            paper_no = match.group(1)
            # "NBER Working Paper No. 24677" on its own line names the series itself;
            # a mid-line mention is a citation, not this paper's venue.
            nber_numbered = bool(re.match(r"\s*NBER\b", text, re.I))
            break
    if nber_header or nber_numbered:
        value = f"NBER Working Paper No. {paper_no}" if paper_no else "NBER Working Paper"
        extras["working_paper_no"] = paper_no
        meta.update({"value": value, "confidence": HIGH,
                     "source": "NBER 页眉" if nber_header else "NBER 报告编号"})
        return meta["value"], meta, extras

    for page, line_index, text in lines:
        match = _ARXIV_ID_RE.search(text)
        if match:
            arxiv_id = f"{match.group(1)}{match.group(2)}.{match.group(3)}"
            extras["arxiv_id"] = arxiv_id
            meta.update({"value": f"arXiv preprint arXiv:{arxiv_id}", "confidence": HIGH,
                         "source": f"arXiv 标识（page {page + 1} line {line_index + 1}）"})
            return meta["value"], meta, extras

    for page, line_index, text in lines[:3]:
        if page > 0:
            break
        masthead = _parse_masthead(text)
        if masthead:
            extras.update({"journal": masthead["journal"], "volume": masthead["volume"],
                           "issue": masthead["issue"], "pages": masthead["pages"],
                           "masthead": masthead["line"]})
            meta.update({"value": masthead["journal"], "confidence": HIGH,
                         "source": f"期刊页眉（page {page + 1} line {line_index + 1}）"})
            return meta["value"], meta, extras

    for page, line_index, text in head:
        match = _REPORT_NO_RE.search(text)
        if match:
            value = f"{match.group(1)} {match.group(2)}"
            extras["working_paper_no"] = match.group(2)
            meta.update({"value": value, "confidence": MEDIUM,
                         "source": f"报告编号（page {page + 1} line {line_index + 1}）"})
            return meta["value"], meta, extras

    for page, line_index, text in head:
        if _SSRN_RE.search(text):
            extras["ssrn"] = True
            meta.update({"value": "SSRN", "confidence": LOW,
                         "source": f"SSRN 标识（page {page + 1} line {line_index + 1}）"})
            return meta["value"], meta, extras

    lowered = (filename or "").lower()
    for token, value in (("nber", "NBER Working Paper"), ("arxiv", "arXiv preprint"),
                         ("ssrn", "SSRN")):
        if token in lowered:
            meta.update({"value": value, "confidence": LOW, "source": "文件名",
                         "warnings": ["来源来自文件名（弱信号），请核对"]})
            return meta["value"], meta, extras
    meta["confidence"] = NONE
    return "", meta, extras


# ===========================================================================
# citation
# ===========================================================================
def _compose_citation(result):
    """"Authors (Year). Title. Venue. https://doi.org/<doi>" - parsed parts only.

    At least two parts must exist; a bare title (or a bare year) is not a citation.
    """
    present = [key for key in ("authors", "year", "title", "venue", "doi")
               if result.get(key)]
    if len(present) < 2:
        return ""
    head = result.get("authors", "")
    year = result.get("year", "")
    if head and year:
        head = f"{head} ({year})"
    elif year:
        head = f"({year})"
    segments = [segment.rstrip(" .;") for segment in
                (head, result.get("title", ""), result.get("venue", "")) if segment]
    text = ". ".join(segments)
    doi = result.get("doi", "")
    if doi:
        text = (text + ". " if text else "") + DOI_URL_PREFIX + doi
    else:
        text = text + "." if text else ""
    return _squash(text)


# ===========================================================================
# small utilities
# ===========================================================================
def _cap(text, limit):
    text = _squash(text or "")
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    if space >= int(limit * 0.6):
        cut = cut[:space]
    return cut.rstrip(" ,;:.-–—")


def _weakest(fields, keys):
    order = {NONE: 0, LOW: 1, MEDIUM: 2, HIGH: 3}
    levels = [fields.get(key, {}).get("confidence", NONE) for key in keys]
    levels = [level for level in levels if level]
    if not levels:
        return NONE
    return min(levels, key=lambda level: order.get(level, 0))


# ===========================================================================
# self check
# ===========================================================================
_SYNTHETIC = (
    ("(a) 干净期刊论文", [{"pdf_page": 1, "text": "\n".join([
        "Journal of Environmental Economics and Management 2021, 108(2): 45-67",
        "https://doi.org/10.1016/j.jeem.2021.102468",
        "Carbon Taxation and Household Behaviour:",
        "Evidence from a Natural Experiment",
        "By Jane Q. Doe, Wei Zhang, and Andrei Popescu",
        "Abstract",
        "We estimate how households respond to a carbon tax using administrative",
        "data covering 1.2 million households between 2015 and 2019. The average",
        "reduction in emissions is 7.3 percent, and the effect is concentrated",
        "among high-income households."] + ["body text " * 20] * 4)}],
     "doe-et-al-2021-carbon-taxation-and-household-behaviour.pdf"),
    ("(b) arXiv 预印本", [{"pdf_page": 1, "text": "\n".join([
        "arXiv:2401.01234v2  [cs.CL]  3 Jan 2024",
        "Sparse Attention Is All You Need for Long Documents",
        "Maria Lopez, Chen Wei, and Rahul Kumar",
        "Abstract",
        "We propose a sparse attention mechanism that scales to 128k tokens",
        "while matching dense attention on retrieval benchmarks."] + ["text " * 30] * 5)}],
     "lopez-2024-sparse-attention.pdf"),
    ("(c) NBER 工作论文封面", [{"pdf_page": 1, "text": "\n".join([
        "NBER WORKING PAPER SERIES",
        "MONETARY POLICY AND THE GLOBAL FINANCIAL CYCLE",
        "scar Jord",
        "Moritz Schularick",
        "Alan M. Taylor",
        "Felix Ward",
        "Working Paper 24677",
        "http://www.nber.org/papers/w24677",
        "NATIONAL BUREAU OF ECONOMIC RESEARCH",
        "1050 Massachusetts Avenue",
        "Cambridge, MA 02138",
        "June 2018",
        "The views expressed here are those of the authors.",
        "© 2018 by scar Jord, Moritz Schularick, and Alan M. Taylor. All rights reserved."])},
     ],
     "GLOBAL FINANCIAL CYCLES.pdf"),
    ("(d) 双栏 + 脚注块", [{"pdf_page": 1, "text": "\n".join([
        "Review of Economic Studies 2022, 89(4): 1801-1840",
        "https://doi.org/10.1093/restud/rdab099",
        "Bank Competition and Credit Supply",
        "By Elena Petrova and Kenji Watanabe*",
        "ABSTRACT",
        "* Petrova: Department of Economics, University of Warwick (email: e.petrova@warwick.ac.uk).",
        "Watanabe: Bank of Japan, 2-1-1 Nihonbashi-Hongokucho, Tokyo 103-8660.",
        "We study how bank competition affects credit supply in two columns of text",
        "that the extractor reads left to right, line by line, mixing both columns",
        "so that a naive parser sees prose where the byline used to be. The left",
        "column continues here and the right column continues here as well."] + ["more " * 25] * 3)}],
     "petrova-watanabe-2022-bank-competition.pdf"),
    ("(e) 扫描件/无文本层", [{"pdf_page": page, "text": "", "char_count": 0,
                              "has_text": False, "has_images": True, "warnings": []}
                             for page in (1, 2, 3)],
     "smith-2019.pdf"),
    ("(f) 无文本层但文件名干净", [{"pdf_page": page, "text": "", "char_count": 0,
                                  "has_text": False, "has_images": True, "warnings": []}
                                 for page in (1, 2, 3)],
     "hahn-et-al-2026-a-welfare-analysis-of-policies-impacting-climate-change.pdf"),
    ("(g) 无 By 前缀的署名行", [{"pdf_page": 1, "text": "\n".join([
        "GLOBAL SYNTHETIC CYCLES AND RISK PREMIUMS",
        "Jane Q. Author, John Doe",
        "NBER Working Paper No. 24677",
        "June 2018",
        "doi:10.1234/synthetic.2018.1"])}],
     "synthetic-2018-global-cycles.pdf"),
)


def _self_check():
    _stdout_utf8()
    root = Path(__file__).resolve().parent
    cache_dir = root / "data" / "extracted"
    import time

    print("=" * 78)
    print("metadata.py 自检 —— 真实缓存文件")
    print("=" * 78)
    files = sorted(cache_dir.glob("*.json")) if cache_dir.is_dir() else []
    if not files:
        print(f"（没有找到缓存目录 {cache_dir}）")
    for path in files:
        report = {}
        start = time.perf_counter()
        for _ in range(20):
            result = parse_metadata_file(path, filename="", report=report)
        elapsed = (time.perf_counter() - start) / 20 * 1000
        print(f"\n--- {path.name}  ({report['source']['pages']} 页, "
              f"{elapsed:.2f} ms/次) ---")
        for key in RESULT_KEYS:
            print(f"    {key:<8} = {result[key]!r}")
        print("    字段置信度：")
        for key in ("title", "authors", "year", "venue", "doi"):
            info = report["fields"].get(key, {})
            print(f"      {key:<8} {info.get('confidence', 'none'):<7} "
                  f"{info.get('source', '')}")
        if report["fields"].get("authors", {}).get("suspicious"):
            print("      可疑作者片段（抽取器可能丢字）："
                  f"{report['fields']['authors']['suspicious']}")
        for warning in report["warnings"]:
            print(f"    ! {warning}")

    print("\n" + "=" * 78)
    print("metadata.py 自检 —— 合成用例")
    print("=" * 78)
    for name, pages, filename in _SYNTHETIC:
        report = {}
        result = parse_metadata(pages, filename=filename, report=report)
        print(f"\n--- {name}  (filename={filename}) ---")
        for key in RESULT_KEYS:
            print(f"    {key:<8} = {result[key]!r}")
        print(f"    置信度：" + "  ".join(
            f"{key}={report['fields'].get(key, {}).get('confidence', 'none')}"
            for key in ("title", "authors", "year", "venue", "doi")))
        for warning in report["warnings"]:
            print(f"    ! {warning}")

    print("\n" + "=" * 78)
    print("边界情况")
    print("=" * 78)
    empty = parse_metadata([])
    print(f"    空输入 -> {empty}")
    print(f"    字符串输入 -> {parse_metadata('not json')}")
    print(f"    路径输入 -> {bool(parse_metadata(cache_dir / 'missing.json'))}")
    if files:
        big = parse_metadata_file(files[0])
        print(f"    截断检查 -> title={len(big['title'])} authors={len(big['authors'])} "
              f"venue={len(big['venue'])} doi={len(big['doi'])}")
    return 0


def _stdout_utf8():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


if __name__ == "__main__":
    raise SystemExit(_self_check())
