"""Debug: why does a restored card lose claimed_page?"""
import json
import sys
from pathlib import Path

ROOT = Path(r"D:\harness home\paper-reader")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tests" / "security"))

import backup_guard  # noqa: E402
import quotes as quotes_module  # noqa: E402
import reading_card  # noqa: E402

page_text = "第一页正文内容示例文本，用于核对引文是否逐字匹配。" * 3
pages = [{"pdf_page": 1, "text": page_text},
         {"pdf_page": 2, "text": "第二页内容。" * 5},
         {"pdf_page": 3, "text": "第三页的引文目标文本在这里，应当可以精确匹配。" * 2}]
index = quotes_module.PageTextIndex(pages)

payload = {
    "schema_version": 1,
    "prompt_version": reading_card.PROMPT_VERSION,
    "source_sha256": "a" * 64,
    "overall": {"title": "t", "language": "zh", "paper_type": "empirical",
                "main_claim": "", "conclusion": "", "confidence": "low"},
    "answers": {key: {"status": "not_found", "answer": "原文未说明", "evidence": [],
                      "confidence": "low", "causal_strength": "unclear"}
                for key in reading_card.FIELD_KEYS},
    "terms": [], "reader_questions": [], "warnings": [],
}
# one answer with a quote deliberately attributed to the wrong page
payload["answers"]["claim"] = {
    "status": "found", "answer": "作者认为该效应存在。",
    "evidence": [{"page": 1, "quote": "第三页的引文目标文本在这里"}],
    "confidence": "medium", "causal_strength": "correlational"}

# make enough verified quotes to pass the ratio gate
for key in list(reading_card.FIELD_KEYS)[:5]:
    payload["answers"][key] = {
        "status": "found", "answer": "作者说明了该点。",
        "evidence": [{"page": 1, "quote": "第一页正文内容示例文本"},
                     {"page": 3, "quote": "第三页的引文目标文本在这里"}],
        "confidence": "medium", "causal_strength": "correlational"}

record, report = backup_guard.verify_analysis(
    payload, paper_id="p1", source_sha256="a" * 64, page_index=index, page_count=3)
print("report:", json.dumps(report, ensure_ascii=False)[:200])
if record:
    entry = record["answers"]["claim"]["evidence"][0]
    print("claim evidence:", json.dumps(entry, ensure_ascii=False))
    print("source tag:", record.get("source"))
else:
    print("REJECTED")
