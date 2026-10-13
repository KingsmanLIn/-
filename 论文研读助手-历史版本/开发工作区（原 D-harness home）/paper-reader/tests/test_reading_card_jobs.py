"""Phase 4 over HTTP: validation gates `ready`, with exactly one repair attempt.

    python -m unittest discover -s tests -p "test_reading_card_jobs.py" -v
"""
from __future__ import annotations

import contextlib
import json
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from baseline_helpers import PROJECT_ROOT, fresh_library_v1

sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import analysis      # noqa: E402
import providers     # noqa: E402
import reading_card  # noqa: E402
import server        # noqa: E402

PAPER = "medical-2026-zang"

# Real page text of the bundled paper, taken at test time; quotes below come from it.
PAGE_ONE_QUOTES = ("分级诊疗制度下的医疗控费机制", "基于大样本病案数据的研究证据")
PAGE_THREE_QUOTES = ("基层诊疗适用度",)


def valid_card(page_one: str, page_three: str) -> dict:
    """A schema-valid card whose quotes are taken from the real extracted text."""
    def item(text, page, quote, causal="unclear", confidence="medium"):
        return {"status": "found", "answer": text, "confidence": confidence,
                "evidence": [{"page": page, "quote": quote}], "causal_strength": causal}
    answers = {
        "question": item("作者研究分级诊疗制度的控费效果", 1, page_one),
        "motivation": item("作者认为需要评估分级诊疗的费用影响", 1, page_one),
        "claim": item("作者的主要回答与分级诊疗的费用变化有关", 1, page_one),
        "data": item("数据为大样本病案数据", 1, page_one),
        "variables": item("核心变量是基层诊疗适用度（原文未说明单位）", 3, page_three),
        "method": item("不确定：原文该页没有说明识别策略细节", 3, page_three),
        "evidence": item("该页给出了与结论相关的数据描述", 1, page_one),
        "robustness": item("不确定：稳健性检验细节原文未说明", 1, page_one),
        "boundaries": item("结论边界受样本限制（原文未说明外推条件）", 1, page_one),
        "assessment": item("不确定：需要核对控制变量", 3, page_three),
        "summary": item("本文用病案数据讨论分级诊疗与费用的关系", 1, page_one),
        "connections": item("与其他医疗控费文献的联系原文未说明", 3, page_three),
        "next": item("下一步核对原文的识别策略部分", 3, page_three),
    }
    return {"schema_version": 1,
            "overall": {"title": "分级诊疗制度下的医疗控费机制", "language": "zh",
                        "paper_type": "empirical", "main_claim": "分级诊疗与费用变化相关",
                        "conclusion": "见原文", "confidence": "low"},
            "answers": answers, "terms": [], "reader_questions": [], "warnings": []}


class CardJobsCase(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.tmp = Path(tempfile.mkdtemp(prefix="card-jobs-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        fresh_library_v1(self.tmp)
        self.store = server.Store(self.tmp)
        self.store.update_ai_settings({"provider": "ollama", "model": "fake-model",
                                       "auto_analyze": False})
        self.factory = analysis_extractor()
        self.fake = providers.FakeProvider(script=["success"],
                                           payload={"schema_version": 1, "overall": {},
                                                    "answers": {}, "terms": [],
                                                    "reader_questions": [], "warnings": []})
        self.manager = analysis.AnalysisManager(
            self.store, provider_factory=lambda settings: self.fake,
            extractor_factory=self.factory, log=lambda record: None)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        import session as session_module
        self.sessions = session_module.SessionStore()
        self.httpd = server.BoundedHTTPServer(
            ("127.0.0.1", port),
            server.handler_for(self.store, "", self.manager, sessions=self.sessions),
            max_concurrency=32)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.shutdown)
        self.base = f"http://127.0.0.1:{port}"
        # a real session: spend a one-time capability, keep the cookie and the CSRF token
        capability = self.sessions.issue_bootstrap(source="test")
        request = Request(self.base + "/api/session/bootstrap",
                          data=json.dumps({"capability": capability}).encode(),
                          method="POST",
                          headers={"Content-Type": "application/json",
                                   "Host": f"127.0.0.1:{port}"})
        with urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
            cookie = response.headers.get("Set-Cookie", "").split(";", 1)[0]
        self.auth = {"Cookie": cookie, "X-CSRF-Token": payload["csrf"]}

    # -- helpers -----------------------------------------------------------
    def page_quotes(self):
        """Two real quotes from pages 1 and 3 of the bundled paper (text layer only)."""
        pages = self.factory.pages

        def pick(page_index, minimum, length):
            lines = [line.strip() for line in pages[page_index].text.split("\n")]
            choices = [line for line in lines if len(line) >= minimum and "DOI" not in line]
            return choices[0][:length]

        return pick(0, 10, 24), pick(2, 8, 18)

    def run_job(self, payloads, script=None):
        page_one, page_three = self.page_quotes()
        cards = [valid_card(page_one, page_three) if item == "valid" else item
                 for item in payloads]
        self.fake = providers.FakeProvider(script=list(script or ["success"] * len(cards)),
                                           payloads=cards)
        self.manager.provider_factory = lambda settings: self.fake
        # what POST /generate does before queueing: record the user's approval for this paper
        # and the current target, which is what the send gate checks
        self.manager.grant_approval(PAPER, model="fake-model", scope="test")
        self.manager.enqueue(PAPER, reason="manual", force=True)
        self.manager.process_once()
        return self.store.analysis_meta(PAPER)

    def post(self, path, body):
        request = Request(self.base + path, data=json.dumps(body).encode(),
                          method="POST", headers=dict({"Content-Type": "application/json"},
                                                      **getattr(self, "auth", {})))
        try:
            with urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            return {"status": exc.code, **json.loads(exc.read().decode("utf-8", "replace"))}

    def get(self, path):
        with urlopen(Request(self.base + path, headers=self.auth),
                     timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))


class RealExtractorFactory:
    """Callable extractor factory that keeps the pages used for quote verification."""

    def __init__(self):
        import extraction
        from storage import DataStore
        directory = Path(tempfile.mkdtemp(prefix="card-extract-"))
        self.extractor = extraction.PdfTextExtractor(
            files=DataStore(directory).ensure_directories(), prefer=["builtin"])
        self.result = self.extractor.extract(PROJECT_ROOT / "data" / "papers" / f"{PAPER}.pdf",
                                             paper_id=PAPER)
        self.pages = self.result.pages

    def __call__(self):
        return self.extractor


def analysis_extractor():
    """Callable extractor factory (what AnalysisManager expects)."""
    return RealExtractorFactory()


class ValidPipeline(CardJobsCase):
    def test_valid_card_reaches_ready_with_verified_quotes(self):
        meta = self.run_job(["valid"])
        self.assertEqual("ready", meta["status"], meta["error_message"])
        self.assertEqual(reading_card.PROMPT_VERSION, meta["prompt_version"])
        payload, error = self.store.analysis_answer(PAPER)
        self.assertIsNone(error)
        self.assertEqual(reading_card.SCHEMA_VERSION, payload["schema_version"])
        self.assertEqual(13, len(payload["answers"]))
        self.assertEqual(PAPER, payload["paper_id"])
        self.assertEqual(meta["source_sha256"], payload["source_sha256"])
        self.assertEqual("fake-model", payload["model"])
        self.assertTrue(payload["created_at"])
        self.assertGreaterEqual(payload["verification"]["verified_quotes"],
                                reading_card.MIN_VERIFIED_QUOTES)
        self.assertEqual(1, len(self.fake.calls))
        self.assertFalse(self.store.files.has_failed_answer(PAPER))

    def test_ai_text_never_enters_paper_fields(self):
        before = json.dumps(self.store.paper(PAPER)["fields"], ensure_ascii=False, sort_keys=True)
        self.run_job(["valid"])
        after = json.dumps(self.store.paper(PAPER)["fields"], ensure_ascii=False, sort_keys=True)
        self.assertEqual(before, after, "AI text must never be written into paper.fields")
        self.assertEqual([False] * 4, self.store.paper(PAPER)["steps"])
        self.assertEqual([], self.store.paper(PAPER)["annotations"])

    def test_answer_endpoint_reports_verification(self):
        self.run_job(["valid"])
        payload = self.get(f"/api/analysis/{PAPER}")
        self.assertEqual("ready", payload["analysis"]["status"])
        self.assertEqual(13, len(payload["answer"]["answers"]))
        self.assertIn("verification", payload["answer"])


class OneRepairAttempt(CardJobsCase):
    def test_first_output_invalid_second_valid_reaches_ready(self):
        page_one, page_three = self.page_quotes()
        broken = valid_card(page_one, page_three)
        del broken["answers"]["next"]
        meta = self.run_job([broken, "valid"], script=["success", "success"])
        self.assertEqual("ready", meta["status"], meta["error_message"])
        self.assertEqual(2, len(self.fake.calls), "exactly one repair request")
        self.assertIn("修复", meta["message"])
        payload, _error = self.store.analysis_answer(PAPER)
        self.assertEqual(13, len(payload["answers"]))

    def test_both_outputs_invalid_fails_without_displaying_text(self):
        page_one, page_three = self.page_quotes()
        broken = valid_card(page_one, page_three)
        del broken["answers"]["next"]
        still_broken = valid_card(page_one, page_three)
        still_broken["answers"]["claim"]["evidence"] = [{"page": 1, "quote": "这段引文原文里没有"}]
        meta = self.run_job([broken, still_broken], script=["success", "success"])
        self.assertEqual("failed", meta["status"])
        self.assertEqual("validation_failed", meta["error_code"])
        self.assertEqual(2, len(self.fake.calls))
        answer, error = self.store.analysis_answer(PAPER)
        self.assertIsNone(answer, "no answer file may exist after a validation failure")
        self.assertIn("不存在", error)
        self.assertFalse(self.store.files.has_analysis(PAPER))

    def test_repair_request_failure_also_fails(self):
        page_one, page_three = self.page_quotes()
        broken = valid_card(page_one, page_three)
        del broken["answers"]["next"]
        meta = self.run_job([broken, "valid"], script=["success", "invalid_json"])
        self.assertEqual("failed", meta["status"])
        self.assertEqual("validation_failed", meta["error_code"])
        # security release: the stored message is a fixed-vocabulary sentence, so the
        # adapter category is mapped to its public code rather than copied verbatim
        self.assertIn("invalid_response", meta["error_message"])


class FailureSamplesBlockReady(CardJobsCase):
    """Each failure sample must keep the job out of ready (requirements list)."""

    def failure_card(self, mutate, expected_code):
        page_one, page_three = self.page_quotes()
        bad = valid_card(page_one, page_three)
        mutate(bad)
        meta = self.run_job([bad, bad], script=["success", "success"])
        self.assertEqual("failed", meta["status"],
                         f"{expected_code}: analysis unexpectedly reached {meta['status']}")
        record = json.loads(self.store.files.failed_answer_path(PAPER).read_text(encoding="utf-8"))
        codes = [code for entry in record["errors"] for code in entry["codes"]]
        self.assertIn(expected_code, codes, codes)
        return record

    def test_missing_field(self):
        self.failure_card(lambda card: card["answers"].pop("robustness"), "field_completeness")

    def test_bad_json(self):
        # the model answers with plain text instead of JSON: the adapter cannot parse it
        self.fake = providers.FakeProvider(script=["success", "success"],
                                           raw_text="I am sorry, here is prose instead of JSON")
        self.manager.provider_factory = lambda settings: self.fake
        self.manager.grant_approval(PAPER, model="fake-model", scope="test")
        self.manager.enqueue(PAPER, reason="manual", force=True)
        self.manager.process_once()
        meta = self.store.analysis_meta(PAPER)
        self.assertEqual("failed", meta["status"])
        record = json.loads(self.store.files.failed_answer_path(PAPER).read_text(encoding="utf-8"))
        codes = [code for entry in record["errors"] for code in entry["codes"]]
        # prose instead of JSON cannot satisfy the schema, so the card is rejected
        self.assertTrue({"bad_json", "not_an_object", "field_completeness"} & set(codes), codes)
        self.assertEqual("failed", meta["status"])
        # security release: the model's own text is never persisted
        self.assertNotIn("raw_model_output", record)
        self.assertNotIn("raw_repair_output", record)
        self.assertNotIn("prose instead of JSON", json.dumps(record, ensure_ascii=False))

    def test_page_out_of_range(self):
        self.failure_card(
            lambda card: card["answers"]["claim"].update({"evidence": [{"page": 999,
                                                                        "quote": "分级诊疗"}]}),
            "page_range")

    def test_forged_quote(self):
        self.failure_card(
            lambda card: card["answers"]["evidence"].update(
                {"evidence": [{"page": 1, "quote": "本文证明分级诊疗在全国范围内降低了费用"}]}),
            "quote_missing")

    def test_causal_overstatement(self):
        def mutate(card):
            card["answers"]["claim"].update({"answer": "分级诊疗导致了医疗费用下降",
                                             "causal_strength": "causal",
                                             "evidence": [{"page": 1,
                                                           "quote": card["answers"]["claim"]["evidence"][0]["quote"]}]})
        self.failure_card(mutate, "causal_overreach")

    def test_all_quotes_forged(self):
        def mutate(card):
            for key in reading_card.FIELD_KEYS:
                card["answers"][key]["evidence"] = [{"page": 1, "quote": "完全不存在的引文内容"}]
        self.failure_card(mutate, "quote_missing")


class FailureLogFormat(CardJobsCase):
    def test_failed_log_has_stable_fields_and_no_secret(self):
        page_one, page_three = self.page_quotes()
        broken = valid_card(page_one, page_three)
        del broken["answers"]["next"]
        self.run_job([broken, broken], script=["success", "success"])
        path = self.store.files.failed_answer_path(PAPER)
        self.assertTrue(path.is_file())
        record = json.loads(path.read_text(encoding="utf-8"))
        for field in ("schema", "paper_id", "prompt_version", "provider", "adapter", "model",
                      "source_sha256", "created_at", "error_code", "errors", "duration_ms",
                      "attempt", "outcome"):
            self.assertIn(field, record)
        self.assertEqual("validation_failed", record["error_code"])
        self.assertEqual(reading_card.PROMPT_VERSION, record["prompt_version"])
        self.assertTrue(record["errors"][0]["codes"])
        self.assertTrue(all("@" in item for item in record["errors"][0]["details"]))
        self.assertNotIn("sk-", json.dumps(record))
        # the raw model text must not be part of the record
        for forbidden in ("raw_model_output", "raw_repair_output", "raw_text"):
            self.assertNotIn(forbidden, record)

    def test_status_endpoint_exposes_the_failure(self):
        page_one, page_three = self.page_quotes()
        broken = valid_card(page_one, page_three)
        del broken["answers"]["next"]
        self.run_job([broken, broken], script=["success", "success"])
        payload = self.get(f"/api/analysis/{PAPER}")
        self.assertEqual("failed", payload["analysis"]["status"])
        self.assertEqual("validation_failed", payload["analysis"]["error_code"])
        self.assertIsNone(payload["answer"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
