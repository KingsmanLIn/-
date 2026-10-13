"""The repair path: ask for the failed fields only, and fold the answer back in.

Why this exists: on a real 58-page paper the three reading passes all succeeded, then the repair
request - which used to ask for the whole 13-field card again - came back unparseable, and the
reader got nothing. A small answer is a reliable answer, so a field-level failure is now repaired
field by field; card-level failures (a quote that verifies nowhere, an empty card) keep the
full-card repair, because there is no single field to blame.

Run:  D:\\harness home\\pr-lab\\runtime\\python.exe D:\\harness home\\pr-lab\\tests\\test_repair.py
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import analysis        # noqa: E402
import extraction      # noqa: E402
import providers       # noqa: E402
import reading_card    # noqa: E402
import schema          # noqa: E402
import secret_guard    # noqa: E402
import server          # noqa: E402

try:                                                             # discovery imports flat
    from tests.test_multipass import (FakeAdapter, FakeExtractor, card_for, page_text,
                                      sentence, PAPER_ID)
except ImportError:
    from test_multipass import (FakeAdapter, FakeExtractor, card_for, page_text,
                                sentence, PAPER_ID)

PAGES = 6


class FieldRepairAdapter(FakeAdapter):
    """First card: one field points at a page outside the document. Repair: only that field.

    An out-of-range page is used because `quote_missing` is deliberately *not* fatal any more
    (an unverifiable quote marks its own field as 待人工核对 instead of discarding the card), so a
    test that wants to exercise the repair path needs a problem that still blocks.
    """

    def __init__(self, page_count):
        super().__init__(page_count)
        self.repair_prompts = []

    def complete_json(self, request, on_start=None):
        self.calls += 1
        self.requests.append(request)
        self.request_sent = True
        if on_start:
            on_start()
        if reading_card.REPAIR_SYSTEM_PROMPT == request.system:
            self.repair_prompts.append(request.user)
            if "只重新输出下面这些字段" in request.user:
                payload = {"answers": {"claim": {
                    "status": "found", "answer": "修复后的主张说明。",
                    "evidence": [{"page": 1, "quote": sentence(1)}],
                    "confidence": "high", "causal_strength": "descriptive"}}}
            else:
                payload = card_for(self.page_count, marker="repaired-full")
            return providers.AdapterResult(True, payload=payload,
                                           model=request.model or "fake-model",
                                           raw_text=json.dumps(payload, ensure_ascii=False))
        card = card_for(self.page_count, marker="first")
        card["answers"]["claim"]["evidence"] = [{"page": 9999, "quote": sentence(1)}]
        return providers.AdapterResult(True, payload=card, model=request.model or "fake-model",
                                       raw_text=json.dumps(card, ensure_ascii=False))


class CardLevelAdapter(FieldRepairAdapter):
    """The first card is missing most of its fields: a card-level failure, repaired as a whole."""

    def complete_json(self, request, on_start=None):
        if reading_card.REPAIR_SYSTEM_PROMPT != request.system:
            self.calls += 1
            self.requests.append(request)
            self.request_sent = True
            card = card_for(self.page_count, marker="first")
            card["answers"] = {"claim": card["answers"]["claim"]}   # 12 fields missing
            return providers.AdapterResult(True, payload=card, model=request.model or "fake-model",
                                           raw_text=json.dumps(card, ensure_ascii=False))
        return super().complete_json(request, on_start=on_start)


def build(adapter_class=FieldRepairAdapter, pages=PAGES):
    directory = tempfile.mkdtemp(prefix="pr-repair-")
    store = server.Store(directory)
    store.update_ai_settings({"provider": "openai_compatible", "mode": "cloud_https",
                              "base_url": "https://example.invalid/v1",
                              "model": "fake-model", "api_key_env": "", "auto_analyze": False})
    store.state["papers"].append({
        "id": PAPER_ID, "title": "Synthetic repair paper", "authors": "", "year": "",
        "venue": "", "doi": "", "citation": "", "tags": "", "language": "en",
        "type": "empirical", "fields": {key: "" for key in reading_card.FIELD_KEYS},
        "steps": [False] * 4, "annotations": [], "pageOffset": 0, "pageLabels": [], "lastPage": 1,
        "revision": 0, "created": "2026-10-13T00:00:00Z", "updated": "2026-10-13T00:00:00Z",
        "archived": False, "note_name": "synthetic.md", "analysis": schema.default_analysis()})
    store.persist()
    (store.files.papers / f"{PAPER_ID}.pdf").write_bytes(b"%PDF-1.4 synthetic fixture")
    adapter = adapter_class(pages)
    manager = analysis.AnalysisManager(store, provider_factory=lambda settings: adapter,
                                       extractor_factory=lambda: FakeExtractor(pages),
                                       log=lambda record: None)
    manager.approve_target()
    manager.enqueue(PAPER_ID, reason="manual")
    manager.process(PAPER_ID)
    return store, adapter


class RepairFields(unittest.TestCase):
    def test_field_level_problems_keep_the_field_names(self):
        fields = analysis._repair_fields([{"field": "claim"}, {"field": "method"},
                                          {"field": "overall"}])
        self.assertEqual(("claim", "method", "overall"), fields)

    def test_card_level_problems_ask_for_a_full_repair(self):
        self.assertEqual((), analysis._repair_fields([{"field": "answers"}]))
        self.assertEqual(("evidence",), analysis._repair_fields([{"field": "evidence"}]))

    def test_too_many_fields_are_repaired_in_batches(self):
        problems = [{"field": key} for key in reading_card.FIELD_KEYS]
        batch = analysis._repair_fields(problems)
        self.assertEqual(analysis.REPAIR_FIELD_LIMIT, len(batch),
                         "a repair never asks for a whole card: it takes a small batch")
        self.assertEqual(tuple(reading_card.FIELD_KEYS[:analysis.REPAIR_FIELD_LIMIT]), batch)

    def test_the_field_prompt_lists_the_fields_and_not_the_whole_card(self):
        paper = {"title": "T", "language": "zh", "type": "empirical"}
        chunks = extraction.PdfTextExtractor.chunks(
            type("E", (), {"pages": [{"pdf_page": 1, "text": page_text(1)}], "page_count": 1,
                           "ok": True, "code": "ok", "message": "", "backend": "test"})(),
            document_id="x")
        request = reading_card.build_repair_request(
            paper, chunks, 1, '{"answers": {}}', [{"field": "claim", "code": "quote_missing",
                                                   "detail": "没有逐字出现"}],
            only_fields=["claim"])
        self.assertIn("只重新输出下面这些字段", request.user)
        self.assertIn("claim", request.user)
        self.assertNotIn("需要修复的 JSON", request.user)
        self.assertEqual(["claim"], request.input_report["repair_fields"])

    def test_merge_replaces_only_the_named_fields(self):
        base = {"answers": {"claim": {"answer": "旧"}, "method": {"answer": "方法"}},
                "overall": {"title": "原"}}
        patch = {"answers": {"claim": {"answer": "新"}}, "overall": {"title": "改"}}
        merged = reading_card.merge_repaired_fields(base, patch, ["claim"])
        self.assertEqual("新", merged["answers"]["claim"]["answer"])
        self.assertEqual("方法", merged["answers"]["method"]["answer"])
        self.assertEqual("原", merged["overall"]["title"])
        self.assertEqual("旧", base["answers"]["claim"]["answer"], "the base must not be mutated")


class RepairJob(unittest.TestCase):
    def test_a_field_repair_rescues_the_paper(self):
        store, adapter = build(FieldRepairAdapter)
        meta = store.analysis_meta(PAPER_ID)
        self.assertEqual("ready", meta["status"], meta.get("message"))
        self.assertEqual(2, adapter.calls, "one pass plus one repair")
        self.assertTrue(adapter.repair_prompts, "the repair must have been sent")
        self.assertIn("只重新输出下面这些字段", adapter.repair_prompts[0])
        card, error = store.analysis_answer(PAPER_ID)
        self.assertFalse(error, error)
        claim = card["answers"]["claim"]["evidence"][0]
        self.assertTrue(claim["verified"], claim)
        self.assertEqual(sentence(1), claim["quote"])
        # the rest of the card is the first attempt's, untouched by the repair
        self.assertEqual("first", card["overall"]["title"])

    def test_a_card_level_failure_keeps_the_full_repair(self):
        store, adapter = build(CardLevelAdapter)
        meta = store.analysis_meta(PAPER_ID)
        self.assertEqual("ready", meta["status"], meta.get("message"))
        self.assertTrue(adapter.repair_prompts)
        self.assertIn("需要修复的 JSON", adapter.repair_prompts[0])
        self.assertNotIn("只重新输出下面这些字段", adapter.repair_prompts[0])
        card, _error = store.analysis_answer(PAPER_ID)
        self.assertEqual("repaired-full", card["overall"]["title"])


class MalformedThenGood(FakeAdapter):
    """The first reply is not valid JSON; the retry of the same pass is fine."""

    def __init__(self, page_count):
        super().__init__(page_count)
        self.prompts = []

    def complete_json(self, request, on_start=None):
        self.calls += 1
        self.requests.append(request)
        self.request_sent = True
        if on_start:
            on_start()
        self.prompts.append(request.user)
        if self.calls == 1:
            return providers.AdapterResult(False, category=providers.CATEGORY_INVALID_JSON,
                                           public_code=secret_guard.E_INVALID_RESPONSE,
                                           message="返回内容不是合法 JSON", parse_detail="bad_json@3924")
        payload = card_for(self.page_count, marker="retried")
        return providers.AdapterResult(True, payload=payload, model=request.model or "fake-model",
                                       raw_text=json.dumps(payload, ensure_ascii=False))


class TruncatedThenGood(MalformedThenGood):
    """The first reply is truncated (a ceiling problem), which must NOT be retried."""

    def complete_json(self, request, on_start=None):
        if self.calls == 0:
            self.calls += 1
            self.requests.append(request)
            self.request_sent = True
            if on_start:
                on_start()
            self.prompts.append(request.user)
            return providers.AdapterResult(False, category=providers.CATEGORY_INVALID_JSON,
                                           public_code=secret_guard.E_OUTPUT_TRUNCATED,
                                           message="输出被上限截断", parse_detail="empty_content")
        return super().complete_json(request, on_start=on_start)


class MalformedRepairThenGood(FieldRepairAdapter):
    """Pass fails validation, the first repair answer is malformed, the retry repairs it."""

    def __init__(self, page_count):
        super().__init__(page_count)
        self.repairs = 0

    def complete_json(self, request, on_start=None):
        if reading_card.REPAIR_SYSTEM_PROMPT == request.system:
            self.repairs += 1
            if self.repairs == 1:
                self.calls += 1
                self.requests.append(request)
                self.request_sent = True
                if on_start:
                    on_start()
                return providers.AdapterResult(False, category=providers.CATEGORY_INVALID_JSON,
                                               public_code=secret_guard.E_INVALID_RESPONSE,
                                               message="返回内容不是合法 JSON",
                                               parse_detail="bad_json@77")
        return super().complete_json(request, on_start=on_start)


class PassRetry(unittest.TestCase):
    def build(self, adapter_class, pages=PAGES):
        directory = tempfile.mkdtemp(prefix="pr-retry-")
        store = server.Store(directory)
        store.update_ai_settings({"provider": "openai_compatible", "mode": "cloud_https",
                                  "base_url": "https://example.invalid/v1",
                                  "model": "fake-model", "api_key_env": "", "auto_analyze": False})
        store.state["papers"].append({
            "id": PAPER_ID, "title": "Synthetic retry paper", "authors": "", "year": "",
            "venue": "", "doi": "", "citation": "", "tags": "", "language": "en",
            "type": "empirical", "fields": {key: "" for key in reading_card.FIELD_KEYS},
            "steps": [False] * 4, "annotations": [], "pageOffset": 0, "pageLabels": [],
            "lastPage": 1, "revision": 0, "created": "2026-10-13T00:00:00Z",
            "updated": "2026-10-13T00:00:00Z", "archived": False, "note_name": "synthetic.md",
            "analysis": schema.default_analysis()})
        store.persist()
        (store.files.papers / f"{PAPER_ID}.pdf").write_bytes(b"%PDF-1.4 synthetic fixture")
        adapter = adapter_class(pages)
        manager = analysis.AnalysisManager(store, provider_factory=lambda settings: adapter,
                                           extractor_factory=lambda: FakeExtractor(pages),
                                           log=lambda record: None)
        manager.approve_target()
        manager.enqueue(PAPER_ID, reason="manual")
        manager.process(PAPER_ID)
        return store, adapter

    def test_a_malformed_reply_is_retried_once(self):
        store, adapter = self.build(MalformedThenGood)
        meta = store.analysis_meta(PAPER_ID)
        self.assertEqual("ready", meta["status"], meta.get("message"))
        self.assertEqual(2, adapter.calls, "one pass plus one JSON retry")
        self.assertIn("不是合法 JSON", adapter.prompts[1])
        self.assertIn("只输出一个 JSON 对象", adapter.prompts[1])

    def test_a_truncated_reply_is_not_retried(self):
        store, adapter = self.build(TruncatedThenGood)
        meta = store.analysis_meta(PAPER_ID)
        self.assertEqual("failed", meta["status"])
        self.assertEqual(1, adapter.calls, "a ceiling problem is not retried, it is reported")
        self.assertIn("输出上限", meta.get("message", "") + meta.get("error_message", ""))

    def test_a_malformed_repair_answer_is_retried_once(self):
        store, adapter = self.build(MalformedRepairThenGood)
        meta = store.analysis_meta(PAPER_ID)
        self.assertEqual("ready", meta["status"], meta.get("message"))
        self.assertEqual(2, adapter.repairs, "the repair is re-asked once when it is unusable")
        card, _error = store.analysis_answer(PAPER_ID)
        self.assertEqual("first", card["overall"]["title"])
        self.assertTrue(card["answers"]["claim"]["evidence"][0]["verified"])


class TwoBatchAdapter(FakeAdapter):
    """Two fields point outside the document; the first repair fixes one, the second the other."""

    def __init__(self, page_count):
        super().__init__(page_count)
        self.repairs = 0

    def complete_json(self, request, on_start=None):
        self.calls += 1
        self.requests.append(request)
        self.request_sent = True
        if on_start:
            on_start()
        if reading_card.REPAIR_SYSTEM_PROMPT == request.system:
            self.repairs += 1
            wanted = ("claim", reading_card.FIELD_KEYS[1])[min(self.repairs, 2) - 1]
            payload = {"answers": {wanted: {
                "status": "found", "answer": f"{wanted} 的修复说明。",
                "evidence": [{"page": 1, "quote": sentence(1)}],
                "confidence": "high", "causal_strength": "descriptive"}}}
            return providers.AdapterResult(True, payload=payload,
                                           model=request.model or "fake-model",
                                           raw_text=json.dumps(payload, ensure_ascii=False))
        card = card_for(self.page_count, marker="first")
        card["answers"]["claim"]["evidence"] = [{"page": 9999, "quote": sentence(1)}]
        card["answers"][reading_card.FIELD_KEYS[1]]["evidence"] = [
            {"page": 9999, "quote": sentence(2)}]
        return providers.AdapterResult(True, payload=card, model=request.model or "fake-model",
                                       raw_text=json.dumps(card, ensure_ascii=False))


class TwoBatches(unittest.TestCase):
    def test_a_second_repair_fixes_what_the_first_left_broken(self):
        store, adapter = PassRetry().build(TwoBatchAdapter)
        meta = store.analysis_meta(PAPER_ID)
        self.assertEqual("ready", meta["status"], meta.get("message"))
        self.assertEqual(2, adapter.repairs, "both batches were repaired")
        card, _error = store.analysis_answer(PAPER_ID)
        self.assertTrue(card["answers"]["claim"]["evidence"][0]["verified"])
        self.assertTrue(card["answers"][reading_card.FIELD_KEYS[1]]["evidence"][0]["verified"])


class TolerantParsing(unittest.TestCase):
    def test_prose_around_the_object_is_tolerated(self):
        payload = reading_card.parse_model_json(
            '好的，这是结果：\n{"overall": {"title": "T"}, "answers": {}}\n希望有帮助。')
        self.assertEqual("T", payload["overall"]["title"])

    def test_a_fenced_object_with_trailing_text_is_tolerated(self):
        payload = reading_card.parse_model_json(
            '```json\n{"overall": {"title": "T"}, "answers": {}}\n```\n以上。')
        self.assertEqual("T", payload["overall"]["title"])

    def test_a_brace_inside_a_string_does_not_end_the_object(self):
        payload = reading_card.parse_model_json(
            '前言 {"overall": {"title": "a } b"}, "answers": {}} 后记')
        self.assertEqual("a } b", payload["overall"]["title"])

    def test_broken_json_is_still_refused(self):
        with self.assertRaises(reading_card.ValidationError) as caught:
            reading_card.parse_model_json('{"overall": {"title": "T"}, "answers":')
        self.assertEqual("bad_json", caught.exception.code)

    def test_duplicate_keys_are_still_refused(self):
        with self.assertRaises(reading_card.ValidationError) as caught:
            reading_card.parse_model_json('{"overall": {"title": "a", "title": "b"}, "answers": {}}')
        self.assertEqual("duplicate_key", caught.exception.code)


if __name__ == "__main__":
    unittest.main(verbosity=2)
