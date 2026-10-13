"""Shared phase-4 test fixture: a schema-valid reading card built from real page text.

Kept in one place so the job tests assert on the pipeline, not on hand-written JSON.
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import reading_card  # noqa: E402

FIELDS = reading_card.FIELD_KEYS


def pick_quote(page_text, minimum=8, length=24, avoid=("DOI",), repeated=()):
    """First usable line of a page that is not a running header.

    `repeated` holds lines that appear on several pages (running titles); citing them
    would point at the wrong page, so they are skipped when something else is available.
    """
    candidates = [part.strip() for part in (page_text or "").split("\n")]
    for line in candidates:
        if len(line) >= minimum and not any(marker in line for marker in avoid) \
                and line not in repeated:
            return line[:length]
    for line in candidates:
        if len(line) >= minimum and not any(marker in line for marker in avoid):
            return line[:length]
    return (page_text or "").strip()[:length]


def pages_from_prompt(prompt, minimum=20):
    """Rebuild page-like objects from a reading-card prompt.

    The prompt lists its input as "PDF 第 X-Y 页" followed by the page text, so a fake
    provider can quote exactly what the model was given - and the validator then checks
    those quotes against that same text.
    """
    import re

    class _Page:
        __slots__ = ("pdf_page", "text", "char_count", "backend")

        def __init__(self, number, text):
            self.pdf_page = number
            self.text = text
            self.char_count = len(text)
            self.backend = "prompt"

    pages = []
    blocks = re.split(r"PDF 第 (\d+)-(\d+) 页", prompt or "")
    for index in range(1, len(blocks) - 2, 3):
        start = int(blocks[index])
        text = blocks[index + 2]
        if len(text.strip()) >= minimum:
            pages.append(_Page(start, text))
    return pages


def repeated_lines(pages, minimum=6, max_pages=6):
    """Short lines that appear on several pages: running heads and journal names."""
    seen = {}
    for page in pages[:max_pages]:
        text = page.text if hasattr(page, "text") else page.get("text", "")
        for line in {part.strip() for part in text.split("\n") if part.strip()}:
            if len(line) >= minimum:
                seen[line] = seen.get(line, 0) + 1
    return {line for line, count in seen.items() if count > 1}


def later_page_index(pages, wanted=3):
    """Index (1-based) of a page whose text differs from page 1, for citation variety."""
    shared = repeated_lines(pages)

    def fingerprint(index):
        page = pages[index - 1]
        text = page.text if hasattr(page, "text") else page.get("text", "")
        return pick_quote(text, minimum=10, length=24, repeated=shared)
    first = fingerprint(1)
    for number in range(2, len(pages) + 1):
        if fingerprint(number) != first:
            return number if number >= wanted else wanted
    return wanted


def build_card(pages, *, language="zh", not_found=(), causal=None, mutate=None,
               later_page_field="data"):
    """Build a card whose quotes come from `pages` (list of Page or dict).

    pages: physical page texts, index 0 == PDF page 1.
    not_found: field keys that should be reported as "原文未说明".
    causal: field key that gets causal_strength="causal".
    mutate: optional callable(card) for failure samples.
    """
    def text_of(index):
        page = pages[index]
        return page.text if hasattr(page, "text") else page.get("text", "")

    shared = repeated_lines(pages)
    quotes_by_page = {number: pick_quote(text_of(number - 1), repeated=shared)
                      for number in range(1, len(pages) + 1)}
    fallback_page = min(quotes_by_page) if quotes_by_page else 1
    fallback_quote = quotes_by_page.get(fallback_page, "原文")

    def item(key, page, quote, text):
        if key == later_page_field and key not in not_found and later_page != page:
            page, quote = later_page, quotes_by_page.get(later_page, quote)
        return {"status": "not_found" if key in not_found else "found",
                "answer": reading_card.NOT_FOUND_TEXT if key in not_found else text,
                "evidence": [] if key in not_found else [{"page": page, "quote": quote}],
                "confidence": "low" if key in not_found else "medium",
                "causal_strength": "causal" if key == causal else "unclear"}

    third = min(3, len(pages)) or fallback_page
    # One field deliberately cites a page other than the first, so page navigation can
    # be exercised (the first pages of these papers share the same running title).
    later_page = max(third, min(len(pages), later_page_index(pages))) if pages else fallback_page
    answers = {
        "question": item("question", fallback_page, fallback_quote,
                         "作者研究的是本文标题所指的问题"),
        "motivation": item("motivation", fallback_page, fallback_quote,
                           "作者认为该问题值得研究，并与已有文献相关"),
        "claim": item("claim", fallback_page, fallback_quote,
                      "作者的主要回答与原文结果一致"),
        "data": item("data", fallback_page, fallback_quote, "数据来源与样本见原文该页"),
        "variables": item("variables", third, quotes_by_page.get(third, fallback_quote),
                          "关键概念与变量见原文（原文未说明单位）"),
        "method": item("method", third, quotes_by_page.get(third, fallback_quote),
                       "不确定：原文该页没有完整说明识别策略"),
        "evidence": item("evidence", fallback_page, fallback_quote,
                         "核心结果与原文位置见该页描述"),
        "robustness": item("robustness", fallback_page, fallback_quote,
                           "不确定：稳健性检验细节原文未说明"),
        "boundaries": item("boundaries", fallback_page, fallback_quote,
                           "结论边界受样本与时期限制（原文未说明外推条件）"),
        "assessment": item("assessment", third, quotes_by_page.get(third, fallback_quote),
                           "不确定：需要回到原文核对控制变量"),
        "summary": item("summary", fallback_page, fallback_quote,
                        "本文用原文数据讨论核心问题与结果"),
        "connections": item("connections", third, quotes_by_page.get(third, fallback_quote),
                            "与其他文献的联系原文未说明"),
        "next": item("next", third, quotes_by_page.get(third, fallback_quote),
                     "下一步核对原文的方法与附录部分"),
    }
    card = {
        "schema_version": reading_card.SCHEMA_VERSION,
        "overall": {"title": "测试论文", "language": language, "paper_type": "empirical",
                    "main_claim": "与原文结果一致", "conclusion": "见原文", "confidence": "low"},
        "answers": answers,
        "terms": [],
        "reader_questions": ["原文的识别策略是什么？"],
        "warnings": [],
    }
    if mutate is not None:
        mutate(card)
    return card
