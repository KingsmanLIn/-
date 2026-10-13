"""Figure/vision pipeline tests. Offline: no network, no poppler, no real PDF.

Run:  D:\\harness home\\pr-lab\\runtime\\python.exe D:\\harness home\\pr-lab\\tests\\test_vision.py

What is covered: which pages count as figure pages, how a page render becomes an
OpenAI-compatible image part, that the prompt explains the attachments, that the job passes
images only when the reader switched vision on *and* the adapter can carry them, and that the
saved card records which pages were sent as pictures.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import analysis        # noqa: E402
import figures         # noqa: E402
import providers       # noqa: E402
import reading_card    # noqa: E402
import schema          # noqa: E402
import server          # noqa: E402

try:                                                             # discovery imports flat
    from tests.test_multipass import (FakeAdapter, FakeExtractor, card_for, page_text,
                                      sentence, PAPER_ID)
except ImportError:
    from test_multipass import (FakeAdapter, FakeExtractor, card_for, page_text,
                                sentence, PAPER_ID)
import extraction      # noqa: E402


class FakeExtraction:
    """Same shape as extraction.ExtractionResult, with figure flags under test control."""

    def __init__(self, page_count, pages=None):
        self.ok = True
        self.code = "ok"
        self.message = "synthetic"
        self.backend = "test"
        self.page_count = page_count
        self.pages = pages if pages is not None else pages_with_images(page_count)


def pages_with_images(total, image_pages=()):
    out = []
    for number in range(1, total + 1):
        text = page_text(number)
        if number in image_pages:
            text += f"\nFigure {number}. Synthetic chart of variable X{number}.\n"
        out.append({"pdf_page": number, "text": text, "char_count": len(text),
                    "has_images": number in image_pages})
    return out


class FigurePages(unittest.TestCase):
    def test_pages_with_images_come_first(self):
        pages = pages_with_images(8, image_pages=(2, 5))
        self.assertEqual([2, 5], figures.figure_pages(pages))

    def test_a_vector_chart_is_found_by_its_caption(self):
        """Charts drawn as vector art carry no image flag; the caption is the signal."""
        pages = pages_with_images(6)
        pages[2]["text"] += "\nTable 3. Descriptive statistics of the sample.\n"
        self.assertEqual([3], figures.figure_pages(pages))

    def test_cjk_captions_are_recognised(self):
        pages = pages_with_images(4)
        pages[0]["text"] += "\n图 1  样本期间的趋势。\n"
        self.assertIn(1, figures.figure_pages(pages))

    def test_limit_is_applied(self):
        pages = pages_with_images(9, image_pages=(1, 2, 3, 4))
        self.assertEqual([1, 2], figures.figure_pages(pages, limit=2))

    def test_a_page_without_figures_is_not_selected(self):
        self.assertEqual([], figures.figure_pages(pages_with_images(5)))


class Render(unittest.TestCase):
    def test_missing_renderer_is_reported_not_raised(self):
        original = figures.renderer
        figures.renderer = lambda: ""
        try:
            images, note = figures.render_pages("whatever.pdf", [1])
        finally:
            figures.renderer = original
        self.assertEqual([], images)
        self.assertEqual("no_renderer", note)

    def test_missing_pdf_is_reported(self):
        original = figures.renderer
        figures.renderer = lambda: "pdftocairo"
        try:
            images, note = figures.render_pages(str(ROOT / "does-not-exist.pdf"), [1])
        finally:
            figures.renderer = original
        self.assertEqual([], images)
        self.assertEqual("missing_pdf", note)

    def test_rendered_pages_become_base64_png(self):
        """The renderer is stubbed: the command shape and the size caps are what is tested."""
        original_run, original_renderer = figures.subprocess.run, figures.renderer
        commands = []

        def fake_run(command, **kwargs):
            stem = Path(command[-1])
            stem.with_suffix(".png").write_bytes(b"\x89PNG" + b"x" * 2048)
            commands.append(command)
            return type("Done", (), {"returncode": 0})()

        figures.renderer = lambda: "pdftocairo"
        figures.subprocess.run = fake_run
        try:
            images, note = figures.render_pages(__file__, [3, 4])
        finally:
            figures.subprocess.run, figures.renderer = original_run, original_renderer
        self.assertEqual("", note)
        self.assertEqual([3, 4], [item["page"] for item in images])
        self.assertEqual("image/png", images[0]["mime"])
        self.assertGreater(len(images[0]["data"]), 100)
        self.assertEqual(2, len(commands))
        self.assertIn("-singlefile", commands[0])
        self.assertEqual(["3", "4"], [command[command.index("-f") + 1] for command in commands])

    def test_an_oversized_page_is_skipped(self):
        original_run, original_renderer = figures.subprocess.run, figures.renderer

        def fake_run(command, **kwargs):
            Path(command[-1]).with_suffix(".png").write_bytes(b"x" * 5000)
            return type("Done", (), {"returncode": 0})()

        figures.renderer = lambda: "pdftocairo"
        figures.subprocess.run = fake_run
        try:
            images, note = figures.render_pages(__file__, [1], max_image_bytes=1000)
        finally:
            figures.subprocess.run, figures.renderer = original_run, original_renderer
        self.assertEqual([], images)
        self.assertEqual("skipped:1", note)


class Deadlines(unittest.TestCase):
    """A slow reasoning call with images must not be cut off mid-flight."""

    def test_the_adapter_is_built_with_the_request_deadline(self):
        self.assertGreaterEqual(reading_card.REQUEST_TIMEOUT_SECONDS, 300)
        self.assertEqual(reading_card.REQUEST_TIMEOUT_SECONDS, analysis.REQUEST_DEADLINE_SECONDS)
        adapter = providers.build_adapter(
            {"provider": "openai_compatible", "mode": "cloud_https",
             "base_url": "https://example.invalid", "model": "m", "api_key_env": ""},
            None, timeout=analysis.REQUEST_DEADLINE_SECONDS)
        self.assertEqual(analysis.REQUEST_DEADLINE_SECONDS, adapter.timeout)

    def test_the_reading_card_requests_use_the_same_deadline(self):
        paper = {"title": "T", "language": "zh", "type": "empirical"}
        chunks = extraction.PdfTextExtractor.chunks(FakeExtraction(1), document_id="x")
        self.assertEqual(reading_card.REQUEST_TIMEOUT_SECONDS,
                         reading_card.build_request(paper, chunks, 1).timeout_seconds)
        self.assertEqual(reading_card.REQUEST_TIMEOUT_SECONDS,
                         reading_card.build_continue_request(
                             paper, chunks, 1, "{}", pass_index=2, pass_total=2)
                         .timeout_seconds)


class Payload(unittest.TestCase):
    def test_text_only_request_stays_a_plain_string(self):
        request = providers.CompletionRequest(system="s", user="u")
        self.assertEqual("u", providers._user_content(request))

    def test_images_become_content_parts(self):
        request = providers.CompletionRequest(
            system="s", user="分析这两页图表",
            images=({"page": 3, "mime": "image/png", "data": "QUJD"},))
        content = providers._user_content(request)
        self.assertIsInstance(content, list)
        self.assertEqual("text", content[0]["type"])
        self.assertEqual("分析这两页图表", content[0]["text"])
        self.assertEqual("image_url", content[1]["type"])
        self.assertEqual("data:image/png;base64,QUJD", content[1]["image_url"]["url"])
        self.assertEqual(["3"], [str(page) for page in request.as_dict()["image_pages"]])

    def test_the_prompt_explains_the_attached_pages(self):
        paper = {"title": "T", "language": "zh", "type": "empirical"}
        chunks = extraction.PdfTextExtractor.chunks(FakeExtraction(1), document_id="x")
        request = reading_card.build_request(paper, chunks, 1, figure_pages=[3, 7])
        self.assertIn("本次随请求附上 2 张页面图片（PDF 第 3、7 页）", request.user)
        self.assertIn("引文只能取上面原文里逐字出现的句子", request.user)
        self.assertIn("不要凭图自己写出引文", request.user)
        self.assertEqual([3, 7], request.input_report["figure_pages"])

    def test_no_figure_note_without_figures(self):
        paper = {"title": "T", "language": "zh", "type": "empirical"}
        chunks = extraction.PdfTextExtractor.chunks(FakeExtraction(1), document_id="x")
        request = reading_card.build_request(paper, chunks, 1)
        self.assertNotIn("张页面图片", request.user)
        self.assertEqual([], request.input_report["figure_pages"])

    def test_merged_report_unions_the_figure_pages(self):
        merged = reading_card.merge_input_reports(
            [{"pages_used": [1, 2], "figure_pages": [2]},
             {"pages_used": [3, 4], "figure_pages": [3]}],
            4, chunks_total=4, budget=1000, covers_all=True, figure_pages=[2, 3])
        self.assertEqual([2, 3], merged["figure_pages"])
        block = reading_card.input_block(merged, 4)
        self.assertIn("第 2、3 页的图表以图片形式一并发送", block["note"])


class FakeVisionAdapter(FakeAdapter):
    """An OpenAI-compatible adapter that never touches the network."""
    supports_images = True


class JobLevel(unittest.TestCase):
    def build(self, vision, max_figure_pages, adapter_class=FakeVisionAdapter, image_pages=(2, 3)):
        """A ready-to-run job. Settings come *before* enqueue: the snapshot freezes them."""
        directory = tempfile.mkdtemp(prefix="pr-vision-")
        store = server.Store(directory)
        store.update_ai_settings({"provider": "openai_compatible", "mode": "cloud_https",
                                  "base_url": "https://example.invalid/v1",
                                  "model": "fake-model", "api_key_env": "",
                                  "auto_analyze": False, "vision": vision,
                                  "max_figure_pages": max_figure_pages})
        store.state["papers"].append({
            "id": PAPER_ID, "title": "Synthetic figure paper", "authors": "", "year": "",
            "venue": "", "doi": "", "citation": "", "tags": "", "language": "en",
            "type": "empirical",
            "fields": {key: "" for key in reading_card.FIELD_KEYS},
            "steps": [False, False, False, False], "annotations": [], "pageOffset": 0,
            "pageLabels": [], "lastPage": 1, "revision": 0, "created": "2026-10-13T00:00:00Z",
            "updated": "2026-10-13T00:00:00Z", "archived": False, "note_name": "synthetic.md",
            "analysis": schema.default_analysis()})
        store.persist()
        (store.files.papers / f"{PAPER_ID}.pdf").write_bytes(b"%PDF-1.4 synthetic fixture")

        adapter = adapter_class(5)
        extractor = FakeExtractor(5)
        extractor.extract = lambda path, paper_id="": FakeExtraction(
            5, pages=pages_with_images(5, image_pages))
        manager = analysis.AnalysisManager(store, provider_factory=lambda settings: adapter,
                                           extractor_factory=lambda: extractor,
                                           log=lambda record: None)
        manager.approve_target()
        manager.enqueue(PAPER_ID, reason="manual")
        return store, manager, adapter

    def test_images_are_attached_when_vision_is_on(self):
        store, manager, adapter = self.build(True, 2)
        original = figures.render_pages
        figures.render_pages = lambda pdf, pages, **kwargs: (
            [{"page": page, "mime": "image/png", "data": "QUJD", "bytes": 3} for page in pages], "")
        try:
            manager.process(PAPER_ID)
        finally:
            figures.render_pages = original
        self.assertTrue(adapter.requests, "the adapter was never called")
        request = adapter.requests[0]
        self.assertEqual([2, 3], [item["page"] for item in request.images])
        content = providers._user_content(request)
        self.assertIsInstance(content, list)
        self.assertEqual(2, len([part for part in content if part["type"] == "image_url"]))
        card, error = store.analysis_answer(PAPER_ID)
        self.assertFalse(error, error)
        self.assertEqual([2, 3], card["input"]["figure_pages"])
        self.assertIn("图表以图片形式一并发送", card["input"]["note"])

    def test_nothing_is_sent_when_vision_is_off(self):
        store, manager, adapter = self.build(False, 2)
        original = figures.render_pages
        figures.render_pages = lambda pdf, pages, **kwargs: (_ for _ in ()).throw(
            AssertionError("rendering must not happen when vision is off"))
        try:
            manager.process(PAPER_ID)
        finally:
            figures.render_pages = original
        self.assertEqual((), tuple(adapter.requests[0].images))
        card, _error = store.analysis_answer(PAPER_ID)
        self.assertEqual([], card["input"]["figure_pages"])

    def test_a_text_only_adapter_gets_no_images(self):
        class PlainAdapter(FakeAdapter):
            supports_images = False

        store, manager, adapter = self.build(True, 2, adapter_class=PlainAdapter)
        original = figures.render_pages
        figures.render_pages = lambda pdf, pages, **kwargs: (_ for _ in ()).throw(
            AssertionError("a text-only adapter must not trigger rendering"))
        try:
            manager.process(PAPER_ID)
        finally:
            figures.render_pages = original
        self.assertEqual((), tuple(adapter.requests[0].images))

    def test_the_budget_is_not_exceeded(self):
        store, manager, adapter = self.build(True, 1)
        original = figures.render_pages
        figures.render_pages = lambda pdf, pages, **kwargs: (
            [{"page": page, "mime": "image/png", "data": "QUJD", "bytes": 3}
             for page in pages[:1]], "")
        try:
            manager.process(PAPER_ID)
        finally:
            figures.render_pages = original
        self.assertEqual([2], [item["page"] for item in adapter.requests[0].images])
        card, _error = store.analysis_answer(PAPER_ID)
        self.assertEqual([2], card["input"]["figure_pages"])


class TruncatingVisionAdapter(FakeVisionAdapter):
    """Fails the first (image-carrying) attempt the way a reasoning model does."""

    def __init__(self, page_count):
        super().__init__(page_count)
        self.saw_images = []

    def complete_json(self, request, on_start=None):
        self.saw_images.append(tuple(item["page"] for item in (request.images or ())))
        if request.images:
            self.calls += 1
            self.requests.append(request)
            self.request_sent = True
            if on_start:
                on_start()
            import secret_guard
            return providers.AdapterResult(
                False, category=providers.CATEGORY_INVALID_JSON,
                public_code=secret_guard.E_OUTPUT_TRUNCATED,
                message="输出被上限截断", parse_detail="empty_content")
        return super().complete_json(request, on_start=on_start)


class Fallback(unittest.TestCase):
    def test_a_truncated_image_pass_is_retried_as_text(self):
        store, manager, adapter = JobLevel().build(
            True, 2, adapter_class=TruncatingVisionAdapter)
        original = figures.render_pages
        figures.render_pages = lambda pdf, pages, **kwargs: (
            [{"page": page, "mime": "image/png", "data": "QUJD", "bytes": 3} for page in pages], "")
        try:
            manager.process(PAPER_ID)
        finally:
            figures.render_pages = original
        self.assertEqual([(2, 3), ()], adapter.saw_images[:2],
                         "the retry must go out without images")
        paper = store.paper(PAPER_ID)
        self.assertEqual("ready", paper["analysis"]["status"], paper["analysis"]["message"])
        card, _error = store.analysis_answer(PAPER_ID)
        self.assertEqual([], card["input"]["figure_pages"], "the retry sent no images")
        self.assertNotIn("张页面图片", card["input"]["note"])


class FigureEvidence(unittest.TestCase):
    """A caption that exists only inside the picture must not sink the whole card."""

    def build_card(self, quote_for, pages=3):
        card = card_for(pages, marker="figures")
        field = reading_card.FIELD_KEYS[0]
        card["answers"][field]["evidence"] = [{"page": 2, "quote": quote_for}]
        return card, field

    def index(self, pages=3):
        import quotes
        return quotes.PageTextIndex([{"pdf_page": number, "text": page_text(number)}
                                     for number in range(1, pages + 1)])

    def test_a_figure_only_quote_is_kept_as_an_unverified_candidate(self):
        card, field = self.build_card("FIGURE 2. Caption printed inside the picture only.")
        answer = reading_card.validate_reading_card(
            card, self.index(), 3, paper_id="p", figure_pages=[2])
        evidence = answer["answers"][field]["evidence"]
        self.assertEqual(1, len(evidence))
        self.assertFalse(evidence[0]["verified"])
        self.assertEqual("figure", evidence[0]["match_kind"])
        self.assertEqual([], [item for item in evidence if item.get("verified")])

    def test_the_same_quote_outside_a_figure_page_blocks(self):
        card, _field = self.build_card("FIGURE 2. Caption printed inside the picture only.")
        pages = [{"pdf_page": number, "text": page_text(number)} for number in range(1, 4)]
        for page in pages:                       # no sentence from the fixture may match
            page["text"] = "unrelated filler text only"
        import quotes
        with self.assertRaises(reading_card.ValidationError):
            reading_card.validate_reading_card(card, quotes.PageTextIndex(pages), 3,
                                               paper_id="p", figure_pages=[])

    def test_figure_quotes_do_not_count_against_the_verified_ratio(self):
        """Half the evidence read off pictures: the text half must still be judged on its own."""
        card = card_for(3, marker="ratio")
        for key in list(card["answers"])[::2]:   # every other field cites the figure instead
            card["answers"][key]["evidence"] = [
                {"page": 2, "quote": f"PICTURE-ONLY caption for {key}."}]
        answer = reading_card.validate_reading_card(
            card, self.index(), 3, paper_id="p", figure_pages=[2])
        figures = [item for field in answer["answers"].values()
                   for item in field["evidence"] if item.get("match_kind") == "figure"]
        self.assertGreaterEqual(len(figures), 5)
        self.assertGreaterEqual(answer["verification"]["verified_quotes"], 4)

    def test_a_long_verbatim_quote_is_verified_not_rejected(self):
        """The stored copy is short, but the match runs on the full text the model sent."""
        card = card_for(3, marker="long-quote")
        # A real slice of the page text: it appears verbatim, and it is longer than the limit.
        long_quote = " ".join(page_text(1).split())[:250]
        self.assertGreater(len(long_quote), 200)
        field = reading_card.FIELD_KEYS[0]
        card["answers"][field]["evidence"] = [{"page": 1, "quote": long_quote}]
        answer = reading_card.validate_reading_card(card, self.index(), 3, paper_id="p")
        evidence = answer["answers"][field]["evidence"][0]
        self.assertTrue(evidence["verified"], evidence)
        self.assertLessEqual(len(evidence["quote"]), 200)
        self.assertEqual("exact", evidence["match_kind"])

    def test_a_card_of_only_figure_quotes_still_fails(self):
        card = card_for(3, marker="figures-only")
        for key in card["answers"]:
            card["answers"][key]["evidence"] = [
                {"page": 2, "quote": f"PICTURE-ONLY caption for {key}."}]
        with self.assertRaises(reading_card.ValidationError) as caught:
            reading_card.validate_reading_card(card, self.index(), 3, paper_id="p",
                                               figure_pages=[2])
        codes = {problem["code"] for problem in caught.exception.details}
        self.assertIn("evidence_unverified", codes)


class Schema(unittest.TestCase):
    def test_vision_settings_are_validated(self):
        merged = schema.validate_ai_settings({"vision": True, "max_figure_pages": 6})
        self.assertTrue(merged["vision"])
        self.assertEqual(6, merged["max_figure_pages"])
        for bad in ({"vision": "yes"}, {"max_figure_pages": 99}, {"max_figure_pages": -1},
                    {"max_figure_pages": True}):
            with self.subTest(bad=bad):
                with self.assertRaises(schema.SchemaError):
                    schema.validate_ai_settings(dict(bad))

    def test_defaults_keep_vision_off(self):
        merged = schema.validate_ai_settings({})
        self.assertFalse(merged["vision"])
        self.assertEqual(4, merged["max_figure_pages"])

    def test_job_snapshot_carries_the_vision_flags(self):
        snapshot = __import__("job_policy").JobSnapshot.from_dict(
            {"job_id": "job-1", "paper_id": PAPER_ID, "vision": True, "max_figure_pages": 3})
        self.assertTrue(snapshot.vision)
        self.assertEqual(3, snapshot.max_figure_pages)
        self.assertTrue(snapshot.as_dict()["vision"])
        legacy = __import__("job_policy").JobSnapshot.from_dict(
            {"job_id": "job-2", "paper_id": PAPER_ID})
        self.assertFalse(legacy.vision)


if __name__ == "__main__":
    unittest.main(verbosity=2)
