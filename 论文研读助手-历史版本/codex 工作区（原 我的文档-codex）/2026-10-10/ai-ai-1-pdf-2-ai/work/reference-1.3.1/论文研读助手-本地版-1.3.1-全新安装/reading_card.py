"""Reading-card answer schema, prompts and the validator that gates `ready`.

Prompt version: ``reading-card-v1``.

Nothing the model returns reaches the reader unchecked. `validate_reading_card` either
returns a normalised payload (with verified evidence) or a list of error codes that keep
the job out of `ready`, and the manager may ask the model exactly once to repair its JSON.

Schema (JSON)
-------------
{
  "schema_version": 1,
  "overall": {
      "title": str, "language": "zh"|"en", "paper_type": str,
      "main_claim": str, "conclusion": str, "confidence": "high"|"medium"|"low"
  },
  "answers": {
      "<one of the 13 field keys>": {
          "status": "found"|"partial"|"not_found",
          "answer": str,                       # 原文未说明 when status is not_found
          "evidence": [{"page": int>=1, "quote": str, "verified": bool}],
          "confidence": "high"|"medium"|"low",
          "causal_strength": "causal"|"correlational"|"descriptive"|"unclear"   (optional)
      }
  },
  "terms": [{"term": str, "explanation": str, "page": int|null}],
  "reader_questions": [str],
  "warnings": [str]
}

Validation rules (each maps to a stable error code, see ERROR_CODES)
-------------------------------------------------------------------
field_completeness   the 13 keys must be present exactly, no extras
answer_shape         status/answer/evidence/confidence types and enum values
missing_answer_text  status found/partial requires text; not_found requires 原文未说明
length_limit         per string and per list limits
page_range           every evidence page is an integer inside 1..page_count
quote_missing        a claimed quote cannot be found in the paper text
evidence_unverified  fewer than MIN_VERIFIED_QUOTES verified quotes overall
causal_overreach     causal wording without causal evidence in the quoted text
not_found_overreach  "no effect" wording on a not_found answer
unknown_field        a key outside the 13 reading-card fields
"""
from __future__ import annotations

import copy
import json
import re

import quotes as quotes_module

PROMPT_VERSION = "reading-card-v1"
SCHEMA_VERSION = 1

FIELD_KEYS = ("question", "motivation", "claim", "data", "variables", "method", "evidence",
              "robustness", "boundaries", "assessment", "summary", "connections", "next")
FIELD_LABELS_ZH = {
    "question": "研究问题", "motivation": "问题的重要性与文献缺口", "claim": "主要回答与贡献",
    "data": "数据与样本", "variables": "关键概念与变量", "method": "研究方法与比较逻辑",
    "evidence": "核心结果及原文位置", "robustness": "稳健性与替代解释",
    "boundaries": "结论的适用边界", "assessment": "我的判断与疑问",
    "summary": "合上论文后的复述", "connections": "与其他文献的联系",
    "next": "下一步阅读或研究行动",
}
ANSWER_STATUSES = ("found", "partial", "not_found")
CONFIDENCES = ("high", "medium", "low")
CAUSAL_STRENGTHS = ("causal", "correlational", "descriptive", "unclear")
LANGUAGES = ("zh", "en")
NOT_FOUND_TEXT = "原文未说明"

MAX_ANSWER_CHARS = 4000
MAX_OVERALL_CHARS = 2000
MAX_QUOTE_CHARS = 200
MIN_QUOTE_CHARS = 4
MAX_EVIDENCE_PER_ANSWER = 6
MAX_TERMS = 40
MAX_QUESTIONS = 20
MAX_WARNINGS = 20
MAX_TERM_CHARS = 300
MAX_QUESTION_CHARS = 500
MAX_WARNING_CHARS = 300
MIN_VERIFIED_QUOTES = 4          # across the whole card
MIN_VERIFIED_RATIO = 0.6         # of the quotes that claim to be evidence

# Causal wording is graded: strong verbs always trigger the check, while "effect of"
# style phrases only count when the answer shows no correlational or uncertainty framing.
STRONG_CAUSAL_MARKERS = (
    "导致", "导致了", "使得", "引起", "造成了", "促进了", "降低了", "提高了", "增加了",
    "改善了", "恶化了", "决定了",
    "causes", "caused", "leads to", "led to", "results in", "resulted in", "reduces",
    "increases", "raises", "lowers", "drives", "driven by", "because of",
)
WEAK_CAUSAL_MARKERS = ("效应", "影响", "作用", "因果", "effect of", "impact of", "causal")
CORRELATION_MARKERS = ("相关", "关联", "伴随", "同时", "负相关", "正相关",
                       "correlat", "associat", "accompan", "co-move", "covary")
UNCERTAINTY_MARKERS = ("不确定", "原文未说明", "无法判断", "需要核对", "可能", "或许",
                       "uncertain", "not reported", "unclear", "cannot tell", "may ")
NULL_MARKERS = ("没有证据", "未发现", "不存在", "无显著", "不显著", "没有影响",
                "no evidence", "not found", "no significant", "does not", "no effect",
                "absent")
CAUSAL_EVIDENCE_MARKERS = ("因果", "识别", "工具变量", "双重差分", "断点", "随机",
                           "反事实", "外生", "处理效应", "平行趋势",
                           "causal", "identif", "instrument", "difference-in-differences",
                           "regression discontinuity", "randomi", "counterfactual",
                           "exogenous", "treatment effect")

ERROR_CODES = ("bad_json", "not_an_object", "field_completeness", "answer_shape",
               "missing_answer_text", "length_limit", "page_range", "quote_missing",
               "evidence_unverified", "causal_overreach", "not_found_overreach",
               "unknown_field", "empty_card")

# Characters of page text sent in one request. A Chinese character costs roughly one
# token while English runs about four characters per token, so the Chinese budget is
# smaller. Anything beyond the budget is dropped and reported in the answer's `input`
# block instead of being silently ignored.
PROMPT_BUDGET_ZH = 28000
PROMPT_BUDGET_EN = 36000

# Codes that describe a repair the validator already made. They are reported but do not
# by themselves keep a job out of the ready state.
NON_BLOCKING_CODES = ("page_corrected",)


class ValidationError(Exception):
    """Raised with a list of coded problems."""

    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = list(details or [])

    def as_dict(self):
        return {"code": self.code, "message": self.message, "details": self.details[:12]}


def validation_failure(problems, message="模型返回的内容没有通过校验"):
    """Build a ValidationError carrying several coded problems."""
    codes = []
    for problem in problems:
        if problem["code"] not in codes:
            codes.append(problem["code"])
    error = ValidationError(codes[0] if codes else "empty_card", message, problems)
    error.codes = codes
    return error


# ---------------------------------------------------------------------------
# prompts
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """你是一名严谨的论文阅读助理，只依据用户在消息中提供的论文原文分段工作。

必须遵守：
1. 只使用提供的论文输入。输入里没有的信息一律写“原文未说明”，绝不补充外部知识、常识或猜测。
2. 清楚区分：作者自己的观点（claim）／数据与估计结果（result）／引用他人的研究（cited）／
   你自己的不确定性（uncertainty）。在文字里用“作者认为”“数据显示”“作者引用……的研究”
   “不确定：”这样的措辞明确标注。
3. 必须返回全部 13 个字段，一个都不能省略；确实没有依据的字段也要返回，
   status 写 not_found，answer 写“原文未说明”。
4. 每个关键判断都要给出 PDF 物理页码与原文短引文。页码是 PDF 文件里的第几页（第 1 页就是
   PDF 第 1 页），不要使用期刊印刷页码。引文必须逐字来自原文，不超过 40 字。
5. 不要把统计相关写成因果。只有原文明确做了因果识别（实验、工具变量、双重差分、断点回归、
   随机化等）才可以用因果表述；否则用“与……相关”“伴随”等表述，并把 causal_strength 写成
   correlational 或 descriptive。
6. 不要把“未发现证据”写成“证明不存在”。估计不显著、区间跨零时，要写成“没有发现稳健证据”，
   不能写成“不存在效应”。
7. 只输出一个 JSON 对象，不要输出解释文字、Markdown 代码块或注释。

输出结构（字段名必须完全一致）：
{{"schema_version": 1,
 "overall": {"title": "...", "language": "zh", "paper_type": "...", "main_claim": "...",
             "conclusion": "...", "confidence": "high|medium|low"},
 "answers": {"<字段名>": {"status": "found|partial|not_found", "answer": "...",
             "evidence": [{"page": 1, "quote": "..."}],
             "confidence": "high|medium|low",
             "causal_strength": "causal|correlational|descriptive|unclear"}},
 "terms": [{{"term": "...", "explanation": "...", "page": 1}}],
 "reader_questions": ["..."],
 "warnings": ["..."]}}

13 个字段名固定为：
question, motivation, claim, data, variables, method, evidence, robustness, boundaries,
assessment, summary, connections, next
"""

USER_PROMPT_TEMPLATE = """请为下面这篇论文生成阅读卡参考。

论文标题：{title}
论文语言：{language}
论文类型：{paper_type}
PDF 总页数：{page_count}

字段含义：
{field_guide}

要求回顾：13 个字段全部返回；每个关键判断附 PDF 物理页码与逐字短引文；
找不到依据写 status=not_found 且 answer=“原文未说明”；不要因果过度陈述；
不要输出 JSON 以外的任何内容。

论文原文分段（每段前标注了 PDF 页码范围）：
{chunks}
"""

REPAIR_SYSTEM_PROMPT = """你是 JSON 修复器。用户会给你一份不合格的 JSON 输出和校验错误列表。

要求：
1. 只修复报错的部分，不要新增原文中没有的信息，不要改变已经正确的结论。
2. 页码必须在 PDF 总页数范围内；引文必须逐字来自提供的论文原文分段；找不到依据时
   把该字段的 status 改成 not_found、answer 改成“原文未说明”、evidence 改成空数组。
3. 存在因果表述但原文没有因果识别时，改写成相关表述并把 causal_strength 改成
   correlational 或 descriptive。
4. 只输出修复后的完整 JSON 对象，不要输出解释文字或代码块。
"""

REPAIR_USER_TEMPLATE = """校验错误（按字段归集）：
{problems}

需要修复的 JSON：
{bad_json}

论文原文分段（只能从这里取引文；每段前标注了 PDF 页码范围）：
{chunks}
"""


def field_guide():
    from server import FIELDS  # local import: single source of truth for labels
    return "\n".join(f"- {key}（{FIELDS.get(key, key)}）" for key in FIELD_KEYS)


def build_prompt_body(paper, chunks, page_count, budget=24000):
    """Render the input section and report which pages it actually carries.

    Long papers exceed the budget; the caller records `pages_used`/`truncated` so the
    answer can say plainly which part of the paper the model was shown.
    """
    parts, used, pages = [], 0, []
    truncated = False
    for chunk in chunks:
        piece = chunk.text
        if used + len(piece) > budget:
            piece = piece[:max(0, budget - used)]
            truncated = True
        if not piece:
            break
        parts.append(f"【PDF 第 {chunk.start_page}-{chunk.end_page} 页】\n{piece}")
        pages.append(chunk.start_page)
        used += len(piece)
    used_pages = sorted({page for page in pages})
    if pages:
        span = list(range(min(pages), max(pages) + 1))
        truncated = truncated or len(used_pages) < len(span)
    return {"text": "\n\n".join(parts), "chars": used, "budget": budget,
            "pages_used": used_pages, "first_page": min(pages) if pages else 0,
            "last_page": max(pages) if pages else 0, "truncated": truncated,
            "chunks_used": len(parts), "chunks_total": len(chunks)}


def build_user_prompt(paper, chunks, page_count, budget=24000):
    body = build_prompt_body(paper, chunks, page_count, budget=budget)
    return USER_PROMPT_TEMPLATE.format(
        title=paper.get("title", ""), language=paper.get("language", ""),
        paper_type=paper.get("type", ""), page_count=page_count,
        field_guide=field_guide(), chunks=body["text"])


def build_repair_prompt(bad_text, problems, chunks, page_count, budget=20000):
    buckets = {}
    for problem in problems[:24]:
        buckets.setdefault(problem.get("field") or "overall", []).append(
            f"{problem['code']}: {problem.get('detail', '')}")
    lines = [f"- {name}: " + "；".join(items) for name, items in buckets.items()]
    body = build_prompt_body({}, chunks, page_count, budget=budget)
    return REPAIR_USER_TEMPLATE.format(
        problems="\n".join(lines) or "- 未知错误",
        bad_json=(bad_text or "")[:60000],
        chunks=body["text"])


def build_request(paper, chunks, page_count, model="", timeout_seconds=180, budget=None):
    import providers
    body = build_prompt_body(paper, chunks, page_count,
                             budget=budget or prompt_budget(paper))
    request = providers.CompletionRequest(
        system=SYSTEM_PROMPT,
        user=USER_PROMPT_TEMPLATE.format(
            title=paper.get("title", ""), language=paper.get("language", ""),
            paper_type=paper.get("type", ""), page_count=page_count,
            field_guide=field_guide(), chunks=body["text"]),
        model=model, temperature=0.2, timeout_seconds=timeout_seconds)
    request.input_report = body
    return request


def prompt_budget(paper):
    """Chinese text costs far more characters per token, so it gets a smaller budget."""
    language = (paper or {}).get("language", "")
    return PROMPT_BUDGET_EN if language == "en" else PROMPT_BUDGET_ZH


def build_repair_request(paper, chunks, page_count, bad_text, problems, model="",
                         timeout_seconds=180):
    import providers
    return providers.CompletionRequest(
        system=REPAIR_SYSTEM_PROMPT,
        user=build_repair_prompt(bad_text, problems, chunks, page_count),
        model=model, temperature=0.0, timeout_seconds=timeout_seconds)


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------

def _as_text(value, limit, field, problems, label, allow_empty=False):
    if not isinstance(value, str):
        problems.append({"code": "answer_shape", "field": field,
                         "detail": f"{label} 必须是文本"})
        return ""
    text = value.strip()
    if not text and not allow_empty:
        problems.append({"code": "missing_answer_text", "field": field,
                         "detail": f"{label} 不能为空"})
        return ""
    if len(text) > limit:
        problems.append({"code": "length_limit", "field": field,
                         "detail": f"{label} 超过 {limit} 字"})
        return text[:limit]
    return text


def _has_any(text, markers):
    lowered = (text or "").lower()
    return any(marker.lower() in lowered for marker in markers)


def validate_reading_card(payload, page_index, page_count, *, paper_id="", prompt_version=PROMPT_VERSION,
                          provider="", model="", language="", source_sha256="", created_at="",
                          strict=True, require_evidence=True, input_report=None):
    """Validate and normalise a model answer.

    Returns a normalised dict ready to be written to data/analyses/<id>.json, or raises
    `ValidationError` whose `.details` explains every problem found. `page_index` is a
    quotes.PageTextIndex used to verify pages and quotes. `input_report` records which
    pages were actually sent to the model, so a truncated input is visible in the answer.
    """
    problems = []
    if not isinstance(payload, dict):
        raise ValidationError("not_an_object", "模型返回的不是 JSON 对象")
    if payload.get("schema_version") not in (None, SCHEMA_VERSION):
        problems.append({"code": "answer_shape", "field": "schema_version",
                         "detail": f"schema_version 必须是 {SCHEMA_VERSION}"})

    overall = payload.get("overall")
    if not isinstance(overall, dict):
        problems.append({"code": "answer_shape", "field": "overall", "detail": "缺少 overall 对象"})
        overall = {}
    normalised_overall = {
        "title": _as_text(overall.get("title", ""), 400, "overall", problems, "overall.title",
                          allow_empty=True),
        "language": overall.get("language") if overall.get("language") in LANGUAGES else (language or "zh"),
        "paper_type": _as_text(overall.get("paper_type", ""), 60, "overall", problems,
                               "overall.paper_type", allow_empty=True),
        "main_claim": _as_text(overall.get("main_claim", ""), MAX_OVERALL_CHARS, "overall", problems,
                               "overall.main_claim", allow_empty=True),
        "conclusion": _as_text(overall.get("conclusion", ""), MAX_OVERALL_CHARS, "overall", problems,
                               "overall.conclusion", allow_empty=True),
        "confidence": overall.get("confidence") if overall.get("confidence") in CONFIDENCES else "low",
    }

    answers = payload.get("answers")
    if not isinstance(answers, dict):
        problems.append({"code": "answer_shape", "field": "answers", "detail": "缺少 answers 对象"})
        answers = {}
    for key in answers:
        if key not in FIELD_KEYS:
            problems.append({"code": "unknown_field", "field": key,
                             "detail": f"未知字段 {key}"})
    missing = [key for key in FIELD_KEYS if key not in answers]
    if missing:
        problems.append({"code": "field_completeness", "field": "answers",
                         "detail": "缺少字段：" + "、".join(missing)})

    normalised_answers = {}
    verified_total = claimed_total = corrected_pages = 0
    corrected_details = []
    for key in FIELD_KEYS:
        raw = answers.get(key)
        if raw is None:
            normalised_answers[key] = {"status": "not_found", "answer": NOT_FOUND_TEXT,
                                       "evidence": [], "confidence": "low",
                                       "causal_strength": "unclear"}
            continue
        if isinstance(raw, str):                     # tolerate a bare string answer
            raw = {"status": "partial", "answer": raw, "evidence": [], "confidence": "low"}
        if not isinstance(raw, dict):
            problems.append({"code": "answer_shape", "field": key, "detail": "答案必须是对象"})
            continue
        status = raw.get("status")
        if status not in ANSWER_STATUSES:
            problems.append({"code": "answer_shape", "field": key,
                             "detail": f"status 必须是 {'/'.join(ANSWER_STATUSES)}"})
            status = "partial"
        confidence = raw.get("confidence")
        if confidence not in CONFIDENCES:
            problems.append({"code": "answer_shape", "field": key,
                             "detail": f"confidence 必须是 {'/'.join(CONFIDENCES)}"})
            confidence = "low"
        answer_text = _as_text(raw.get("answer", ""), MAX_ANSWER_CHARS, key, problems, "answer")
        if status == "not_found" and answer_text and NOT_FOUND_TEXT not in answer_text:
            problems.append({"code": "missing_answer_text", "field": key,
                             "detail": f"status=not_found 时 answer 必须是“{NOT_FOUND_TEXT}”"})
            answer_text = NOT_FOUND_TEXT

        evidence_items = raw.get("evidence") or []
        if not isinstance(evidence_items, list):
            problems.append({"code": "answer_shape", "field": key, "detail": "evidence 必须是数组"})
            evidence_items = []
        if len(evidence_items) > MAX_EVIDENCE_PER_ANSWER:
            problems.append({"code": "length_limit", "field": key,
                             "detail": f"evidence 最多 {MAX_EVIDENCE_PER_ANSWER} 条"})
            evidence_items = evidence_items[:MAX_EVIDENCE_PER_ANSWER]
        clean_evidence = []
        for index, item in enumerate(evidence_items):
            item = item if isinstance(item, dict) else {}
            page = item.get("page")
            quote = item.get("quote", "")
            label = f"evidence[{index}]"
            if not isinstance(page, int) or isinstance(page, bool):
                try:
                    page = int(str(page))
                except (TypeError, ValueError):
                    page = None
            if page is None or not 1 <= page <= max(1, page_count):
                problems.append({"code": "page_range", "field": key,
                                 "detail": f"{label}.page={item.get('page')!r} 超出 1..{page_count}"})
                continue
            if not isinstance(quote, str) or not quote.strip():
                problems.append({"code": "quote_missing", "field": key,
                                 "detail": f"{label}.quote 为空"})
                continue
            quote = quote.strip()
            if len(quote) > MAX_QUOTE_CHARS:
                problems.append({"code": "length_limit", "field": key,
                                 "detail": f"{label}.quote 超过 {MAX_QUOTE_CHARS} 字"})
                quote = quote[:MAX_QUOTE_CHARS]
            if len(quote) < MIN_QUOTE_CHARS:
                problems.append({"code": "quote_missing", "field": key,
                                 "detail": f"{label}.quote 太短，无法核对"})
                continue
            claimed_total += 1
            match = page_index.find_anywhere(quote, claimed_page=page)
            if not match["found"]:
                problems.append({"code": "quote_missing", "field": key,
                                 "detail": f"{label} 的引文在原文中找不到：{quote[:30]}…"})
                if strict:
                    continue
                clean_evidence.append({"page": page, "quote": quote, "verified": False})
                continue
            if not match["exact_page"]:
                corrected_pages += 1
                # The quote is real but sits on another page: keep the verified quote and
                # record the correction instead of failing the card over a page slip. A
                # page number outside the document is still a hard `page_range` error.
                problems.append({"code": "page_corrected", "field": key,
                                 "detail": f"{label} 的引文实际在第 {match['page']} 页，"
                                           f"模型写成第 {page} 页（已按实际页码修正）"})
                corrected_details.append(f"{key}:{page}->{match['page']}")
            verified_total += 1
            clean_evidence.append({"page": int(match["page"]), "quote": quote,
                                   "claimed_page": int(page) if int(page) != int(match["page"]) else None,
                                   "verified": True, "similarity": match["similarity"]})
        if status in ("found", "partial") and not clean_evidence:
            problems.append({"code": "quote_missing", "field": key,
                             "detail": "没有一条可核对的引文，不能作为依据展示"})
        if status == "not_found":
            clean_evidence = []

        causal = raw.get("causal_strength")
        if causal is not None and causal not in CAUSAL_STRENGTHS:
            problems.append({"code": "answer_shape", "field": key,
                             "detail": f"causal_strength 必须是 {'/'.join(CAUSAL_STRENGTHS)}"})
            causal = "unclear"
        quoted_text = " ".join(item["quote"] for item in clean_evidence)
        if status != "not_found":
            strong = _has_any(answer_text, STRONG_CAUSAL_MARKERS)
            weak = _has_any(answer_text, WEAK_CAUSAL_MARKERS)
            causal_evidence = _has_any(quoted_text, CAUSAL_EVIDENCE_MARKERS)
            # Only two shapes count as overstatement: an explicit causal claim without a
            # causal-identification basis, or causal verbs the quoted text cannot support.
            # Ordinary paper language such as "费用效应" is not treated as a causal claim.
            if causal == "causal" and not causal_evidence:
                problems.append({"code": "causal_overreach", "field": key,
                                 "detail": "causal_strength=causal 但引文里没有因果识别依据"})
            elif strong and not causal_evidence:
                problems.append({"code": "causal_overreach", "field": key,
                                 "detail": "答案用了因果动词，但引文里没有因果识别依据"})
                causal = "correlational"
            elif weak and causal is None:
                causal = "unclear"
        if _has_any(answer_text, NULL_MARKERS) and _has_any(answer_text, ("证明", "表明不存在",
                                                                         "proves", "proof")):
            problems.append({"code": "not_found_overreach", "field": key,
                             "detail": "把“未发现证据”写成了“证明不存在”"})

        normalised_answers[key] = {
            "status": status, "answer": answer_text, "evidence": clean_evidence,
            "confidence": confidence, "causal_strength": causal or "unclear",
            "verified_quotes": sum(1 for item in clean_evidence if item.get("verified")),
        }

    terms = _list_of_objects(payload.get("terms"), "terms", MAX_TERMS, problems,
                             ("term", "explanation", "page"), page_count)
    reader_questions = _list_of_text(payload.get("reader_questions"), "reader_questions",
                                     MAX_QUESTIONS, MAX_QUESTION_CHARS, problems)
    warnings = _list_of_text(payload.get("warnings"), "warnings", MAX_WARNINGS,
                             MAX_WARNING_CHARS, problems)

    if require_evidence and claimed_total and verified_total / claimed_total < MIN_VERIFIED_RATIO:
        problems.append({"code": "evidence_unverified", "field": "answers",
                         "detail": f"只有 {verified_total}/{claimed_total} 条引文能在原文中核对"})
    if require_evidence and verified_total < MIN_VERIFIED_QUOTES:
        problems.append({"code": "evidence_unverified", "field": "answers",
                         "detail": f"可核对引文不足（{verified_total} < {MIN_VERIFIED_QUOTES}）"})

    if require_evidence:
        found_answers = [key for key, item in normalised_answers.items()
                         if item["status"] in ("found", "partial") and item["evidence"]]
        if not found_answers:
            problems.append({"code": "empty_card", "field": "answers",
                             "detail": "没有任何一个字段给出可核对的依据"})

    blocking = [problem for problem in problems if problem["code"] not in NON_BLOCKING_CODES]
    if blocking:
        raise validation_failure(problems, "模型返回的阅读卡没有通过校验")

    return {
        "schema": SCHEMA_VERSION,
        "schema_version": SCHEMA_VERSION,
        "paper_id": paper_id,
        "prompt_version": prompt_version,
        "provider": provider,
        "model": model,
        "language": normalised_overall["language"],
        "source_sha256": source_sha256,
        "generated_at": created_at,
        "created_at": created_at,
        "overall": normalised_overall,
        "answers": normalised_answers,
        "terms": terms,
        "reader_questions": reader_questions,
        "warnings": warnings,
        "verification": {
            "claimed_quotes": claimed_total,
            "verified_quotes": verified_total,
            "corrected_pages": corrected_pages,
            "corrections": corrected_details[:12],
            "page_count": page_count,
            "field_count": len(normalised_answers),
            "not_found_fields": [key for key, item in normalised_answers.items()
                                 if item["status"] == "not_found"],
            "evidence_pages": sorted({entry["page"] for item in normalised_answers.values()
                                      for entry in item["evidence"]}),
        },
        "input": input_block(input_report, page_count),
    }


def input_block(report, page_count):
    """What the model was actually shown. Keeps truncation visible instead of silent."""
    report = report or {}
    pages_used = [int(page) for page in report.get("pages_used", [])]
    truncated = bool(report.get("truncated"))
    first, last = report.get("first_page") or 0, report.get("last_page") or 0
    if not pages_used and not report:
        note = "未记录输入范围（旧版本生成的参考）"
    elif truncated:
        note = (f"原文共 {page_count} 页，本次只送入 PDF 第 {min(pages_used) if pages_used else first}"
                f"–{max(pages_used) if pages_used else last} 页；之后的页面未进入模型输入，"
                "相关栏目可能显示“原文未说明”。")
    else:
        note = f"原文共 {page_count} 页，全部页面已送入模型。"
    return {
        "pages_used": pages_used,
        "first_page": first,
        "last_page": last,
        "truncated": truncated,
        "chars": int(report.get("chars", 0) or 0),
        "budget": int(report.get("budget", 0) or 0),
        "chunks_used": int(report.get("chunks_used", 0) or 0),
        "chunks_total": int(report.get("chunks_total", 0) or 0),
        "note": note,
    }


def _list_of_text(value, field, maximum, per_item, problems):
    if value is None:
        return []
    if not isinstance(value, list):
        problems.append({"code": "answer_shape", "field": field, "detail": f"{field} 必须是数组"})
        return []
    if len(value) > maximum:
        problems.append({"code": "length_limit", "field": field,
                         "detail": f"{field} 最多 {maximum} 条"})
        value = value[:maximum]
    out = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            problems.append({"code": "answer_shape", "field": field,
                             "detail": f"{field}[{index}] 必须是文本"})
            continue
        text = item.strip()
        if not text:
            continue
        if len(text) > per_item:
            problems.append({"code": "length_limit", "field": field,
                             "detail": f"{field}[{index}] 超过 {per_item} 字"})
            text = text[:per_item]
        out.append(text)
    return out


def _list_of_objects(value, field, maximum, problems, keys, page_count):
    if value is None:
        return []
    if not isinstance(value, list):
        problems.append({"code": "answer_shape", "field": field, "detail": f"{field} 必须是数组"})
        return []
    if len(value) > maximum:
        problems.append({"code": "length_limit", "field": field,
                         "detail": f"{field} 最多 {maximum} 条"})
        value = value[:maximum]
    out = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            problems.append({"code": "answer_shape", "field": field,
                             "detail": f"{field}[{index}] 必须是对象"})
            continue
        entry = {}
        for key in keys:
            raw = item.get(key)
            if key == "page":
                if raw is None:
                    entry[key] = None
                    continue
                try:
                    page = int(raw)
                except (TypeError, ValueError):
                    problems.append({"code": "page_range", "field": field,
                                     "detail": f"{field}[{index}].page 不是页码"})
                    entry[key] = None
                    continue
                if not 1 <= page <= max(1, page_count):
                    problems.append({"code": "page_range", "field": field,
                                     "detail": f"{field}[{index}].page={page} 超出 1..{page_count}"})
                    entry[key] = None
                    continue
                entry[key] = page
                continue
            limit = MAX_TERM_CHARS if key == "explanation" else 120
            entry[key] = _as_text(raw if isinstance(raw, str) else "", limit, field, problems,
                                  f"{field}[{index}].{key}")
        if entry.get("term") and entry.get("explanation"):
            out.append(entry)
    return out


def parse_model_json(text):
    """Parse model output, tolerating code fences. Raises ValidationError(bad_json)."""
    if not text or not text.strip():
        raise ValidationError("bad_json", "模型返回了空内容")
    candidate = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", candidate, re.S)
    if fence:
        candidate = fence.group(1).strip()
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ValidationError("bad_json", f"返回内容不是合法 JSON：{exc.msg}（位置 {exc.pos}）")
    if not isinstance(payload, dict):
        raise ValidationError("not_an_object", "返回内容不是 JSON 对象")
    return payload


def summarise_problems(error):
    """Compact, log-safe description of a failure."""
    codes = getattr(error, "codes", None) or [error.code]
    details = [f"{problem['code']}@{problem.get('field', '-')}" for problem in error.details[:6]]
    return {"codes": codes, "message": str(error)[:300], "details": details}


def empty_card(paper_id, page_count, prompt_version=PROMPT_VERSION, provider="", model="",
               language="", source_sha256="", created_at=""):
    """A card where every field is explicitly not found (used by tests and dry runs)."""
    return validate_reading_card(
        {"schema_version": SCHEMA_VERSION,
         "overall": {"title": "", "language": language or "zh", "paper_type": "",
                     "main_claim": "", "conclusion": "", "confidence": "low"},
         "answers": {key: {"status": "not_found", "answer": NOT_FOUND_TEXT, "evidence": [],
                           "confidence": "low", "causal_strength": "unclear"}
                     for key in FIELD_KEYS},
         "terms": [], "reader_questions": [], "warnings": []},
        quotes_module.PageTextIndex([]), page_count, paper_id=paper_id,
        prompt_version=prompt_version, provider=provider, model=model, language=language,
        source_sha256=source_sha256, created_at=created_at, strict=False,
        require_evidence=False)
