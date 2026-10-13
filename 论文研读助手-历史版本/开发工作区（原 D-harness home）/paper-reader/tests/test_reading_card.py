"""Phase 4: reading-card schema, prompt version and validation gating.

Covers the legal Chinese/English cards and the failure samples that must never reach
`ready`: missing fields, broken JSON, out-of-range pages, forged quotes and causal
overstatement - plus the single repair attempt.

    python -m unittest discover -s tests -p "test_reading_card.py" -v
"""
from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

from baseline_helpers import PROJECT_ROOT

sys.path.insert(0, str(PROJECT_ROOT))

import providers       # noqa: E402
import quotes          # noqa: E402
import reading_card    # noqa: E402

ZH_PAGES = [
    {"pdf_page": 1, "text": "分级诊疗制度下的医疗控费机制\n"
                            "摘要：本文利用 2020—2023 年病案数据，估计分级诊疗制度对医疗费用的影响。\n"
                            "作者认为，基层诊疗适用度提高后，三级医院会调整入院门槛。"},
    {"pdf_page": 2, "text": "数据来自某城市 1687.7 万条病案记录，筛选后保留 320 万条。\n"
                            "基准回归结果显示费用下降约 8.3%，系数为 -0.083，在 5% 水平上显著。\n"
                            "本文使用的是双重差分识别策略，处理组为试点地区。"},
    {"pdf_page": 3, "text": "稳健性检验更换了样本窗口与指标定义，结论方向一致。\n"
                            "作者引用张三等（2019）的研究说明机制来源。\n"
                            "区间跨零的估计没有发现稳健证据支持费用上升。"},
]
EN_PAGES = [
    {"pdf_page": 1, "text": "Does Fertility Stabilize At Low Levels?\n"
                            "Abstract: We use period, cohort and subnational data to test whether "
                            "fertility stabilizes at low levels."},
    {"pdf_page": 2, "text": "Results: the estimated change is -0.15 (s.e. 0.04), so the "
                            "association is negative and precise.\n"
                            "The confidence interval is [-0.26, -0.10]."},
]


def index(pages=None):
    return quotes.PageTextIndex(pages or ZH_PAGES)


def evidence(page, quote):
    return {"page": page, "quote": quote}


def answer(text, page=2, quote="费用下降约 8.3%", status="found", confidence="medium",
           causal=None):
    item = {"status": status, "answer": text, "evidence": [evidence(page, quote)],
            "confidence": confidence}
    if causal:
        item["causal_strength"] = causal
    return item


def zh_card(**overrides):
    """A fully valid Chinese card built from ZH_PAGES quotes."""
    card = {
        "schema_version": 1,
        "overall": {"title": "分级诊疗制度下的医疗控费机制", "language": "zh",
                    "paper_type": "empirical", "main_claim": "分级诊疗降低了医疗费用",
                    "conclusion": "费用下降约 8.3%", "confidence": "medium"},
        "answers": {
            "question": answer("作者研究分级诊疗制度对医疗费用的影响", page=1,
                               quote="估计分级诊疗制度对医疗费用的影响"),
            "motivation": answer("作者认为基层承接能力提高后三级医院会调整入院门槛", page=1,
                                 quote="三级医院会调整入院门槛", causal="correlational"),
            "claim": answer("作者的回答是费用下降约 8.3%", page=2, quote="费用下降约 8.3%"),
            "data": answer("数据为某城市 1687.7 万条病案记录，筛选后保留 320 万条", page=2,
                           quote="1687.7 万条病案记录"),
            "variables": answer("被解释变量是医疗费用，核心解释变量是基层诊疗适用度"
                                "（原文未说明具体单位）", page=2, quote="系数为 -0.083"),
            "method": answer("作者使用双重差分识别策略，处理组为试点地区", page=2,
                             quote="双重差分识别策略"),
            "evidence": answer("基准回归显示费用下降约 8.3%，系数 -0.083，5% 水平显著", page=2,
                               quote="在 5% 水平上显著"),
            "robustness": answer("作者更换样本窗口与指标定义后结论方向一致", page=3,
                                 quote="更换了样本窗口与指标定义"),
            "boundaries": answer("结论限于该城市 2020—2023 年（原文未说明其他地区）", page=2,
                                 quote="筛选后保留 320 万条"),
            "assessment": answer("不确定：双重差分的平行趋势检验细节原文未说明", page=2,
                                 quote="处理组为试点地区"),
            "summary": answer("本文用某城市病案数据估计分级诊疗的费用效应，发现费用下降约 8.3%，"
                              "结论受样本与识别假设限制", page=2, quote="费用下降约 8.3%"),
            "connections": answer("作者引用张三等（2019）的研究说明机制来源", page=3,
                                  quote="引用张三等（2019）"),
            "next": answer("下一步核对双重差分的平行趋势图与事件研究结果", page=2,
                           quote="双重差分识别策略"),
        },
        "terms": [{"term": "基层诊疗适用度", "explanation": "值越高表示病例越适合基层诊疗", "page": 2}],
        "reader_questions": ["平行趋势是否成立？"],
        "warnings": ["费用下降幅度依赖情景假设"],
    }
    card.update(overrides)
    return card


class LegalCards(unittest.TestCase):
    def test_chinese_card_passes_and_normalises(self):
        card = reading_card.validate_reading_card(zh_card(), index(), 3, paper_id="paper-1",
                                                  provider="ollama", model="qwen2.5:7b",
                                                  language="zh", source_sha256="ab" * 32,
                                                  created_at="2026-10-10T00:00:00Z")
        self.assertEqual(reading_card.PROMPT_VERSION, card["prompt_version"])
        self.assertEqual(reading_card.PROMPT_VERSION, card["prompt_version"])
        self.assertEqual("reading-card-v2", card["prompt_version"])
        self.assertEqual("ab" * 32, card["source_sha256"])
        self.assertEqual("qwen2.5:7b", card["model"])
        self.assertEqual("2026-10-10T00:00:00Z", card["created_at"])
        self.assertEqual(13, len(card["answers"]))
        self.assertEqual(set(reading_card.FIELD_KEYS), set(card["answers"]))
        verified = card["verification"]["verified_quotes"]
        self.assertGreaterEqual(verified, reading_card.MIN_VERIFIED_QUOTES)
        self.assertEqual(0, card["verification"]["corrected_pages"])
        for key, item in card["answers"].items():
            self.assertIn(item["status"], reading_card.ANSWER_STATUSES)
            self.assertIn(item["confidence"], reading_card.CONFIDENCES)
            self.assertTrue(item["answer"])
            for entry in item["evidence"]:
                self.assertTrue(entry["verified"])
        self.assertEqual(1, len(card["terms"]))
        self.assertEqual(1, len(card["reader_questions"]))

    def test_english_card_passes(self):
        def en_answer(text, page, quote):
            return {"status": "found", "answer": text, "evidence": [evidence(page, quote)],
                    "confidence": "medium"}
        card = {
            "schema_version": 1,
            "overall": {"title": "Does Fertility Stabilize At Low Levels?", "language": "en",
                        "paper_type": "descriptive", "main_claim": "no robust evidence of stabilization",
                        "conclusion": "the association is negative and precise",
                        "confidence": "medium"},
            "answers": {
                "question": en_answer("Whether fertility stabilizes at low levels", 1,
                                      "test whether fertility stabilizes at low levels"),
                "motivation": en_answer("Prior work disagrees about stabilization", 1,
                                        "period, cohort and subnational data"),
                "claim": en_answer("The association is negative and precise", 2,
                                   "the association is negative and precise"),
                "data": en_answer("Period, cohort and subnational data", 1,
                                  "period, cohort and subnational data"),
                "variables": en_answer("Fertility change and starting level", 2,
                                       "the estimated change is -0.15"),
                "method": en_answer("Binned conditional means over starting levels", 2,
                                    "the estimated change is -0.15 (s.e. 0.04)"),
                "evidence": en_answer("Estimate -0.15 with s.e. 0.04", 2,
                                      "the estimated change is -0.15 (s.e. 0.04)"),
                "robustness": en_answer("Confidence interval [-0.26, -0.10]", 2,
                                        "The confidence interval is [-0.26, -0.10]."),
                "boundaries": en_answer("Subnational samples are sparse at very low levels", 2,
                                        "the association is negative and precise"),
                "assessment": en_answer("Uncertainty: binning choices are not fully reported", 2,
                                        "the estimated change is -0.15"),
                "summary": en_answer("Fertility change is negatively associated with the starting "
                                     "level; no robust evidence of stabilization", 2,
                                     "the association is negative and precise"),
                "connections": en_answer("Relates to the demographic transition literature (cited)",
                                         1, "Does Fertility Stabilize At Low Levels?"),
                "next": en_answer("Check the phase diagram in the appendix", 2,
                                  "The confidence interval is [-0.26, -0.10]."),
            },
            "terms": [{"term": "period TFR", "explanation": "synthetic cohort measure", "page": 1}],
            "reader_questions": ["How wide are the bins?"],
            "warnings": [],
        }
        card = reading_card.validate_reading_card(card, index(EN_PAGES), 2, paper_id="paper-2",
                                                  language="en")
        self.assertEqual(13, len(card["answers"]))
        self.assertEqual("en", card["overall"]["language"])
        self.assertGreaterEqual(card["verification"]["verified_quotes"], reading_card.MIN_VERIFIED_QUOTES)

    def test_not_found_answer_is_allowed_and_normalised(self):
        card = zh_card()
        card["answers"]["data"] = {"status": "not_found", "answer": "原文未说明", "evidence": [],
                                   "confidence": "low"}
        normalised = reading_card.validate_reading_card(card, index(), 3)
        self.assertEqual("not_found", normalised["answers"]["data"]["status"])
        self.assertEqual([], normalised["answers"]["data"]["evidence"])
        self.assertIn("data", normalised["verification"]["not_found_fields"])


class FailureSamples(unittest.TestCase):
    """Every sample below must keep the job out of `ready`."""

    def assertBlocked(self, card, expected_code, pages=None, page_count=3, strict=True):
        with self.assertRaises(reading_card.ValidationError) as caught:
            reading_card.validate_reading_card(card, index(pages), page_count, strict=strict)
        codes = getattr(caught.exception, "codes", [caught.exception.code])
        self.assertIn(expected_code, codes,
                      f"expected {expected_code}, got {codes}: {caught.exception.details[:3]}")
        return caught.exception

    def test_missing_fields(self):
        card = zh_card()
        del card["answers"]["robustness"]
        del card["answers"]["next"]
        self.assertBlocked(card, "field_completeness")

    def test_broken_json_is_not_parseable(self):
        with self.assertRaises(reading_card.ValidationError) as caught:
            reading_card.parse_model_json('{"answers": {"question": ')
        self.assertEqual("bad_json", caught.exception.code)
        with self.assertRaises(reading_card.ValidationError):
            reading_card.parse_model_json("")
        with self.assertRaises(reading_card.ValidationError):
            reading_card.parse_model_json("[1, 2, 3]")

    def test_out_of_range_page(self):
        card = zh_card()
        card["answers"]["claim"]["evidence"] = [evidence(99, "费用下降约 8.3%")]
        self.assertBlocked(card, "page_range")

    def test_zero_and_negative_page(self):
        for page in (0, -1):
            card = zh_card()
            card["answers"]["claim"]["evidence"] = [evidence(page, "费用下降约 8.3%")]
            self.assertBlocked(card, "page_range")

    def test_forged_quote(self):
        card = zh_card()
        card["answers"]["claim"]["evidence"] = [evidence(2, "本文证明了分级诊疗在任何地区都能降低费用")]
        self.assertBlocked(card, "quote_missing")

    def test_all_quotes_forged(self):
        card = zh_card()
        forged = "这段引文在原文中完全不存在"
        for key in reading_card.FIELD_KEYS:
            card["answers"][key]["evidence"] = [evidence(1, forged)]
        self.assertBlocked(card, "quote_missing")

    def test_causal_overstatement(self):
        card = zh_card()
        card["answers"]["evidence"] = answer("分级诊疗导致了医疗费用下降 8.3%", page=2,
                                             quote="费用下降约 8.3%", causal="causal")
        self.assertBlocked(card, "causal_overreach")

    def test_not_found_written_as_proof_of_absence(self):
        card = zh_card()
        card["answers"]["robustness"] = answer("本文证明了费用上升不存在", page=3,
                                               quote="没有发现稳健证据支持费用上升")
        self.assertBlocked(card, "not_found_overreach")

    def test_unknown_field(self):
        card = zh_card()
        card["answers"]["extra_field"] = answer("多余字段", page=2, quote="费用下降约 8.3%")
        self.assertBlocked(card, "unknown_field")

    def test_wrong_status_and_confidence_enums(self):
        card = zh_card()
        card["answers"]["claim"]["status"] = "maybe"
        card["answers"]["data"]["confidence"] = "certain"
        self.assertBlocked(card, "answer_shape")

    def test_empty_answer_text(self):
        card = zh_card()
        card["answers"]["claim"]["answer"] = "   "
        self.assertBlocked(card, "missing_answer_text")

    def test_evidence_missing_for_found_status(self):
        card = zh_card()
        card["answers"]["claim"]["evidence"] = []
        self.assertBlocked(card, "quote_missing")

    def test_too_few_verified_quotes(self):
        card = zh_card()
        for key in reading_card.FIELD_KEYS:
            card["answers"][key]["evidence"] = []
            card["answers"][key]["status"] = "not_found"
            card["answers"][key]["answer"] = "原文未说明"
        self.assertBlocked(card, "empty_card")

    def test_length_limits(self):
        card = zh_card()
        card["answers"]["summary"]["answer"] = "很长" * 3000
        self.assertBlocked(card, "length_limit")

    def test_overall_shape(self):
        card = zh_card()
        del card["overall"]
        self.assertBlocked(card, "answer_shape")
        card = zh_card()
        card["answers"] = "not a dict"
        self.assertBlocked(card, "answer_shape")

    def test_wrong_page_is_corrected_and_recorded(self):
        """A real quote on the wrong page is corrected, not accepted silently."""
        card = zh_card()
        card["answers"]["claim"]["evidence"] = [evidence(1, "费用下降约 8.3%")]
        normalised = reading_card.validate_reading_card(card, index(), 3)
        entry = normalised["answers"]["claim"]["evidence"][0]
        self.assertTrue(entry["verified"])
        self.assertEqual(2, entry["page"], "the real page must replace the claimed one")
        self.assertEqual(1, normalised["verification"]["corrected_pages"])
        self.assertIn("claim:1->2", normalised["verification"]["corrections"])

    def test_page_outside_the_document_is_a_hard_error(self):
        card = zh_card()
        card["answers"]["claim"]["evidence"] = [evidence(42, "费用下降约 8.3%")]
        with self.assertRaises(reading_card.ValidationError) as caught:
            reading_card.validate_reading_card(card, index(), 3)
        codes = getattr(caught.exception, "codes", [])
        self.assertIn("page_range", codes)


class LenientMode(unittest.TestCase):
    def test_common_prefix_truncation_is_accepted(self):
        card = zh_card()
        card["answers"]["claim"]["evidence"] = [evidence(2, "基准回归结果显示费用下降约 8.3%")]
        normalised = reading_card.validate_reading_card(card, index(), 3)
        entry = normalised["answers"]["claim"]["evidence"][0]
        self.assertTrue(entry["verified"])
        self.assertEqual(2, entry["page"])

    def test_whitespace_and_punctuation_differences_are_tolerated(self):
        card = zh_card()
        card["answers"]["claim"]["evidence"] = [evidence(2, "费用下降约8.3%")]
        normalised = reading_card.validate_reading_card(card, index(), 3)
        self.assertTrue(normalised["answers"]["claim"]["evidence"][0]["verified"])

    def test_quote_normalisation_map(self):
        self.assertEqual(quotes.normalise("费用 下降约 8.3%"), quotes.normalise("费用下降约8.3%"))
        self.assertEqual(quotes.normalise("（a）"), quotes.normalise("(a)"))


class PromptContract(unittest.TestCase):
    """The system prompt must carry every rule the phase requires."""

    def test_prompt_version(self):
        self.assertEqual("reading-card-v2", reading_card.PROMPT_VERSION)

    def test_system_prompt_states_all_rules(self):
        prompt = reading_card.SYSTEM_PROMPT
        self.assertIn("只使用提供的论文输入", prompt)
        self.assertIn("原文未说明", prompt)
        self.assertIn("作者自己的观点", prompt)
        self.assertIn("数据与估计结果", prompt)
        self.assertIn("引用他人的研究", prompt)
        self.assertIn("不确定性", prompt)
        self.assertIn("全部 13 个字段", prompt)
        self.assertIn("PDF 物理页码", prompt)
        self.assertIn("逐字来自原文", prompt)
        self.assertIn("不要把统计相关写成因果", prompt)
        self.assertIn("未发现证据", prompt)
        self.assertIn("只输出一个 JSON 对象", prompt)
        for key in reading_card.FIELD_KEYS:
            self.assertIn(key, prompt)

    def test_user_prompt_lists_all_fields_and_chunks(self):
        class Chunk:
            def __init__(self, start, end, text):
                self.start_page, self.end_page, self.text = start, end, text
        prompt = reading_card.build_user_prompt(
            {"title": "T", "language": "zh", "type": "empirical"},
            [Chunk(1, 2, "第一段原文"), Chunk(3, 3, "第二段原文")], 3)
        for key in reading_card.FIELD_KEYS:
            self.assertIn(key, prompt)
        self.assertIn("PDF 第 1-2 页", prompt)
        self.assertIn("PDF 第 3-3 页", prompt)
        self.assertIn("第一段原文", prompt)
        self.assertIn("总页数：3", prompt)

    def test_repair_prompt_carries_problems_and_bad_json(self):
        prompt = reading_card.build_repair_prompt(
            '{"answers": {}}',
            [{"code": "field_completeness", "field": "answers", "detail": "缺少字段：next"}],
            [], 3)
        self.assertIn("field_completeness", prompt)
        self.assertIn("缺少字段", prompt)
        self.assertIn('{"answers": {}}', prompt)

    def test_request_uses_the_prompt_version_system_prompt(self):
        class Chunk:
            start_page, end_page, text = 1, 1, "原文"
        request = reading_card.build_request({"title": "T", "language": "zh", "type": "empirical"},
                                             [Chunk()], 1, model="m")
        self.assertEqual(reading_card.SYSTEM_PROMPT, request.system)
        self.assertEqual("m", request.model)
        self.assertLessEqual(request.temperature, 0.3)

    def test_empty_card_helper_is_valid_and_explicit(self):
        card = reading_card.empty_card("paper-9", 3, provider="none", language="zh")
        self.assertEqual(13, len(card["answers"]))
        self.assertTrue(all(item["status"] == "not_found" for item in card["answers"].values()))
        self.assertTrue(all(item["answer"] == "原文未说明" for item in card["answers"].values()))
        self.assertEqual(13, len(card["verification"]["not_found_fields"]))
        self.assertEqual(0, card["verification"]["verified_quotes"])


class FailureLogFormat(unittest.TestCase):
    def test_summarise_problems_shape(self):
        try:
            reading_card.validate_reading_card(zh_card(answers={"claim": {}}), index(), 3)
        except reading_card.ValidationError as error:
            summary = reading_card.summarise_problems(error)
        else:
            self.fail("validation should have failed")
        self.assertIn("codes", summary)
        self.assertIn("message", summary)
        self.assertTrue(summary["details"])
        self.assertTrue(all("@" in item for item in summary["details"]))
        json.dumps(summary)                                   # log-safe

    def test_failure_record_fields(self):
        """The failure record keeps fixed fields and never the model's own text."""
        import inspect

        import analysis
        import secret_guard
        source = inspect.getsource(analysis.AnalysisManager._record_failure)
        for field in ("schema", "paper_id", "prompt_version", "provider", "adapter", "model",
                      "source_sha256", "created_at", "error_code", "errors", "duration_ms"):
            self.assertIn(f'{field}=', source, f"{field} missing from the failure record")
        for forbidden in ("raw_model_output", "raw_repair_output"):
            self.assertNotIn(forbidden, source,
                             f"{forbidden} must not be persisted (security release)")
        # the record is built from the whitelist in secret_guard, not from ad-hoc keys
        self.assertIn("secret_guard.failure_record", source)
        record = secret_guard.failure_record(schema=1, raw_model_output="dropped")
        self.assertNotIn("raw_model_output", record)
        self.assertEqual(1, record["schema"])
        self.assertEqual("failed", record["outcome"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
