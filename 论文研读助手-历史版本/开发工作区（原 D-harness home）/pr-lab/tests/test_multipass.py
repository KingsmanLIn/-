"""Job-level tests for multi-pass reading. Offline: no network, no real PDF, no provider.

Run:  D:\\harness home\\pr-lab\\runtime\\python.exe D:\\harness home\\pr-lab\\tests\\test_multipass.py

The manager is driven with an injected extractor and an injected provider, so the whole job
path runs - pass planning, budget reservations, the continue prompt, validation and saving -
without a single outbound request.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import analysis          # noqa: E402
import extraction        # noqa: E402
import providers         # noqa: E402
import reading_card      # noqa: E402
import schema            # noqa: E402
import secret_guard      # noqa: E402
import server            # noqa: E402

PAPER_ID = "11111111-2222-3333-4444-555555555555"
NOW = "2026-10-12T00:00:00Z"
PAGE_CHARS = 1200


def sentence(number):
    return f"variable X{number} moves with Y{number}"


def page_text(number):
    filler = " ".join(f"filler{number}_{index}" for index in range(PAGE_CHARS // 12))
    return (f"Synthetic page {number} of the test paper.\n"
            f"The sample shows that {sentence(number)} in the data.\n"
            f"{filler}\n")


class FakeExtraction:
    """Stands in for extraction.ExtractionResult."""

    def __init__(self, page_count):
        self.ok = True
        self.code = "ok"
        self.message = "synthetic"
        self.backend = "test"
        self.page_count = page_count
        self.pages = [{"pdf_page": number, "text": page_text(number),
                       "char_count": len(page_text(number))}
                      for number in range(1, page_count + 1)]


class FakeExtractor:
    def __init__(self, page_count):
        self.page_count = page_count

    def extract(self, path, paper_id=""):
        return FakeExtraction(self.page_count)

    def chunks(self, result, document_id=""):
        return extraction.PdfTextExtractor.chunks(result, document_id=document_id)


def card_for(page_count, marker):
    """A card that passes validation against the synthetic pages."""
    answers = {}
    for index, key in enumerate(reading_card.FIELD_KEYS):
        page = (index % page_count) + 1
        answers[key] = {"status": "found",
                        "answer": f"字段 {key} 的合成答案，依据是第 {page} 页的描述性陈述。",
                        "evidence": [{"page": page, "quote": sentence(page)}],
                        "confidence": "high",
                        "causal_strength": "descriptive"}
    return {"schema_version": 1,
            "overall": {"title": marker, "language": "en", "paper_type": "empirical",
                        "main_claim": "合成主张", "conclusion": "合成结论",
                        "confidence": "high"},
            "answers": answers,
            "terms": [{"term": "synthetic", "explanation": "合成术语", "page": 1}],
            "reader_questions": ["合成问题"],
            "warnings": []}


class FakeAdapter(providers.ProviderAdapter):
    name = "openai_compatible"
    requires_key = False

    def __init__(self, page_count):
        self.page_count = page_count
        self.requests = []
        self.calls = 0

    def health_check(self, timeout=10):
        return providers.Health(True, providers.CATEGORY_OK, "fake")

    def list_models(self, timeout=10):
        return ["fake-model"]

    def describe(self):
        return {"provider": self.name, "model": "fake-model", "base_url": "memory://fake"}

    def cancel(self):
        return False

    def complete_json(self, request, on_start=None):
        self.calls += 1
        self.requests.append(request)
        # Like the real adapters, which set this when the socket write actually happens.
        self.request_sent = True
        if on_start:
            on_start()
        payload = card_for(self.page_count, marker=f"pass-{self.calls}")
        return providers.AdapterResult(True, payload=payload, model=request.model or "fake-model",
                                       raw_text=json.dumps(payload, ensure_ascii=False))


def build_manager(page_count, directory, adapter_class=None):
    store = server.Store(directory)
    store.update_ai_settings({"provider": "openai_compatible", "mode": "cloud_https",
                              "base_url": "https://example.invalid/v1",
                              "model": "fake-model", "api_key_env": "",
                              "auto_analyze": False})
    paper = {"id": PAPER_ID, "title": "Synthetic multi-pass paper", "authors": "", "year": "",
             "venue": "", "doi": "", "citation": "", "tags": "", "language": "en",
             "type": "empirical",
             "fields": {key: "" for key in reading_card.FIELD_KEYS},
             "steps": [False, False, False, False], "annotations": [], "pageOffset": 0,
             "pageLabels": [], "lastPage": 1, "revision": 0, "created": NOW, "updated": NOW,
             "archived": False, "note_name": "synthetic.md",
             "analysis": schema.default_analysis()}
    store.state["papers"].append(paper)
    store.persist()
    (store.files.papers / f"{PAPER_ID}.pdf").write_bytes(b"%PDF-1.4 synthetic fixture")

    adapter = (adapter_class or FakeAdapter)(page_count)
    extractor = FakeExtractor(page_count)
    manager = analysis.AnalysisManager(store, provider_factory=lambda settings: adapter,
                                       extractor_factory=lambda: extractor,
                                       log=lambda record: None)
    # Approved and enqueued, but deliberately NOT run: each test drives execution itself, so a
    # retry cannot be picked up behind the test's back.
    manager.approve_target()
    manager.enqueue(PAPER_ID, reason="manual")
    return store, manager, adapter


class FlakyAdapter(FakeAdapter):
    """The first attempt cannot reach the provider and nothing leaves the machine."""

    def __init__(self, page_count):
        super().__init__(page_count)
        self.fail_first = True

    def complete_json(self, request, on_start=None):
        if self.fail_first:
            self.fail_first = False
            self.calls += 1
            self.requests.append(request)
            self.request_sent = False          # nothing was written to the socket
            return providers.AdapterResult(False,
                                           category=providers.CATEGORY_UNREACHABLE,
                                           public_code=secret_guard.E_UNREACHABLE,
                                           message="unreachable")
        return super().complete_json(request, on_start=on_start)


class BadThenRepaired(FakeAdapter):
    """Every pass answers with a card that fails validation; the repair call is correct."""

    def __init__(self, page_count):
        super().__init__(page_count)
        self.repair_seen = 0

    def complete_json(self, request, on_start=None):
        self.calls += 1
        self.requests.append(request)
        self.request_sent = True
        if on_start:
            on_start()
        if reading_card.REPAIR_SYSTEM_PROMPT == request.system:
            self.repair_seen += 1
            payload = card_for(self.page_count, marker="repaired")
            return providers.AdapterResult(True, payload=payload,
                                           model=request.model or "fake-model",
                                           raw_text=json.dumps(payload, ensure_ascii=False))
        broken = {"schema_version": 1, "overall": {}, "answers": {}}
        return providers.AdapterResult(True, payload=broken, model=request.model or "fake-model",
                                       raw_text=json.dumps(broken, ensure_ascii=False))


class RepairAllowance(unittest.TestCase):
    def test_the_repair_runs_on_a_multi_pass_job(self):
        """A three-pass paper plus its one repair is the plan this program made itself.

        The repair used to be checked against the *configured* per-job ceiling (2), so on a long
        paper it was refused with `budget_blocked` and the reader got nothing even though the
        plan allowed four transmissions.
        """
        directory = tempfile.mkdtemp(prefix="pr-repair-budget-")
        store, manager, adapter = build_manager(90, directory, adapter_class=BadThenRepaired)
        manager.process(PAPER_ID)
        meta = store.analysis_meta(PAPER_ID)
        self.assertEqual(reading_card.MAX_READING_PASSES, adapter.calls - adapter.repair_seen,
                         "the passes should use the whole pass allowance")
        self.assertEqual(1, adapter.repair_seen, "the repair must actually be sent")
        self.assertEqual("ready", meta["status"], meta.get("message"))


class MultiPassJob(unittest.TestCase):
    def run_paper(self, page_count):
        directory = tempfile.mkdtemp(prefix="pr-multipass-")
        store, manager, adapter = build_manager(page_count, directory)
        manager.process(PAPER_ID)
        answer, error = store.files.read_analysis(PAPER_ID)
        self.assertIsNone(error, error)
        meta = store.analysis_meta(PAPER_ID)
        return store, adapter, answer, meta

    def test_short_paper_is_still_one_request(self):
        """No regression: a paper that fits keeps the single-request prompt and one charge."""
        _store, adapter, answer, meta = self.run_paper(6)
        self.assertEqual("ready", meta["status"], meta.get("message"))
        self.assertEqual(1, adapter.calls)
        self.assertEqual(reading_card.SYSTEM_PROMPT, adapter.requests[0].system)
        self.assertNotIn("段续读", adapter.requests[0].user)
        self.assertEqual(1, answer["input"]["passes"])
        self.assertFalse(answer["input"]["truncated"])

    def test_long_paper_is_split_and_fully_covered(self):
        _store, adapter, answer, meta = self.run_paper(30)
        self.assertEqual("ready", meta["status"], meta.get("message"))
        self.assertEqual(2, adapter.calls, "30 pages must need exactly two passes")
        # the second pass is a continue request, not a fresh first pass
        self.assertEqual(reading_card.CONTINUE_SYSTEM_PROMPT, adapter.requests[1].system)
        self.assertIn("第 2/2 段续读", adapter.requests[1].user)
        self.assertIn("pass-1", adapter.requests[1].user, "the draft card must be carried over")
        # the saved card is the last pass's card
        self.assertEqual("pass-2", answer["overall"]["title"])
        # and the recorded input is the union of both passes
        self.assertEqual(2, answer["input"]["passes"])
        self.assertFalse(answer["input"]["truncated"])
        self.assertEqual(1, answer["input"]["pages_used"][0])
        self.assertIn(30, answer["input"]["pages_used"])
        self.assertIn("已分 2 段全部送入模型", answer["input"]["note"])

    def test_very_long_paper_stops_at_the_cap_and_says_so(self):
        _store, adapter, answer, meta = self.run_paper(120)
        self.assertEqual("ready", meta["status"], meta.get("message"))
        self.assertEqual(reading_card.MAX_READING_PASSES, adapter.calls,
                         "the pass count must stop at the ceiling")
        self.assertEqual(reading_card.MAX_READING_PASSES, answer["input"]["passes"])
        self.assertTrue(answer["input"]["truncated"])
        self.assertIn("其余页面未进入模型输入", answer["input"]["note"])

    def test_a_transient_failure_still_retries_within_the_pass_allowance(self):
        """An unreachable first attempt is refunded and the queued retry must run to ready."""
        directory = tempfile.mkdtemp(prefix="pr-retry-")
        store, manager, adapter = build_manager(30, directory, adapter_class=FlakyAdapter)
        manager.process(PAPER_ID)                    # attempt 1: unreachable, re-queued
        self.assertEqual("queued", store.analysis_meta(PAPER_ID)["status"])
        manager.process_once(timeout=60)             # attempt 2: two passes, ready
        meta = store.analysis_meta(PAPER_ID)
        self.assertEqual("ready", meta["status"], meta.get("message"))
        journal = store._job_ledger.state["journal"]
        refunded = [entry for entry in journal if entry.get("settled") == "not_sent"]
        charged = [entry for entry in journal if entry.get("settled") == "sent"]
        self.assertEqual(1, len(refunded), "the unsent attempt must be refunded")
        self.assertEqual(2, len(charged), "the retry's two passes must both be charged")
        self.assertEqual(3, adapter.calls)

    def test_every_pass_is_charged_to_the_job(self):
        store, adapter, _answer, _meta = self.run_paper(30)
        journal = store._job_ledger.state["journal"]
        charged = [entry for entry in journal if entry.get("settled") == "sent"]
        self.assertEqual(adapter.calls, len(charged))
        kinds = [entry.get("kind") for entry in journal]
        self.assertIn("first", kinds)
        self.assertIn("pass", kinds)


class PassPlanning(unittest.TestCase):
    def test_plan_covers_the_document_without_gaps(self):
        pages = FakeExtraction(40).pages
        chunks = extraction.PdfTextExtractor.chunks(FakeExtraction(40))
        paper = {"language": "en"}
        plan = reading_card.plan_passes(paper, chunks, 40)
        self.assertGreaterEqual(plan["passes"], 1)
        self.assertLessEqual(plan["passes"], reading_card.MAX_READING_PASSES)
        covered = [page for segment in plan["segments"] for page in segment["pages_used"]]
        self.assertEqual(sorted(covered), covered, "passes must run in reading order")
        self.assertEqual(1, covered[0])
        # Overlapping chunks may repeat a page, but the union must not skip one.
        self.assertEqual(list(range(1, max(covered) + 1)), sorted(set(covered)),
                         "the union of the passes must have no gaps")

    def test_single_short_document_is_one_segment(self):
        chunks = extraction.PdfTextExtractor.chunks(FakeExtraction(3))
        plan = reading_card.plan_passes({"language": "zh"}, chunks, 3)
        self.assertEqual(1, plan["passes"])
        self.assertTrue(plan["covers_all"])

    def test_real_58_page_paper_now_fits_three_passes(self):
        """The paper that motivated this change: 58 pages, previously only the first ~20 read."""
        cache = ROOT / "data" / "extracted" / "9edf1d4b-561b-47a2-860e-9119d865f54b.json"
        if not cache.is_file():
            self.skipTest("cached extraction not present in this copy")
        payload = json.loads(cache.read_text(encoding="utf-8"))
        page_count = int(payload["page_count"])

        class Cached:
            ok = True
            pages = payload["pages"]

        chunks = extraction.PdfTextExtractor.chunks(Cached())
        plan = reading_card.plan_passes({"language": "en"}, chunks, page_count)
        covered = [page for segment in plan["segments"] for page in segment["pages_used"]]
        self.assertTrue(plan["covers_all"],
                        f"{page_count} pages must fit the pass ceiling, got {plan['passes']}")
        self.assertLessEqual(plan["passes"], reading_card.MAX_READING_PASSES)
        self.assertEqual(1, min(covered))
        self.assertGreaterEqual(max(covered), page_count - 5)

    def test_merged_report_is_the_union(self):
        reports = [{"pages_used": [1, 2, 3], "chars": 100, "chunks_used": 2,
                    "chunks_total": 9, "first_page": 1, "last_page": 3, "truncated": True},
                   {"pages_used": [4, 5], "chars": 50, "chunks_used": 1,
                    "chunks_total": 3, "first_page": 4, "last_page": 5, "truncated": False}]
        merged = reading_card.merge_input_reports(reports, 5, chunks_total=4,
                                                  budget=36000, covers_all=True)
        self.assertEqual([1, 2, 3, 4, 5], merged["pages_used"])
        self.assertEqual(150, merged["chars"])
        self.assertEqual(3, merged["chunks_used"])
        self.assertEqual(4, merged["chunks_total"])
        self.assertFalse(merged["truncated"])
        self.assertEqual(2, merged["passes"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
