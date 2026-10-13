"""Phase 1 security tests: credential lifecycle and the unified output boundary.

Covers C01-C08 and O01-O07 from the security test specification at the level the phase 1
patch implements. Every credential is a fictional sentinel created in this process; failures
report the channel (file/field/endpoint), never the value.

    python -m unittest discover -s tests/security -p "test_phase1_*.py"
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tests" / "security"))

import analysis        # noqa: E402
import credentials     # noqa: E402
import providers       # noqa: E402
import reading_card    # noqa: E402
import schema          # noqa: E402
import secret_guard    # noqa: E402
import server          # noqa: E402
import storage         # noqa: E402
from baseline_helpers import fresh_library_v1  # noqa: E402
from stubs import CountingSink, Sentinel, find_in_tree, register  # noqa: E402

PAPER = "medical-2026-zang"
PAGE_TEXT = "分级诊疗制度下的医疗控费机制：本文基于 2015-2020 年省级面板数据估计了控费效果。"


# ======================================================================= C: credentials
class CredentialLifecycle(unittest.TestCase):
    """C01-C08: references only, no plaintext fallback, no value in any DTO."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sec-c-"))
        self.addCleanup(__import__("shutil").rmtree, self.tmp, ignore_errors=True)
        fresh_library_v1(self.tmp)
        self.store = server.Store(self.tmp)

    # C03 - the legacy plaintext file is reported, never read
    def test_legacy_plaintext_file_is_never_read(self):
        sentinel = Sentinel("c03", "prefixed")
        legacy = self.tmp / "credentials.json"
        legacy.write_text(json.dumps({"PAPER_READER_TEST_KEY": sentinel.value}),
                          encoding="utf-8")
        before = legacy.read_bytes()

        secret, code, _ref = providers.resolve_api_key("PAPER_READER_TEST_KEY", self.store.files)
        self.assertIsNone(secret, "a credential must not come from the plaintext file")
        self.assertEqual(credentials.CODE_LEGACY_FILE, code)
        self.assertEqual(before, legacy.read_bytes(), "the legacy file must not be rewritten")

        # the tripwire on the store refuses as well
        payload, error = self.store.files.read_credentials()
        self.assertIsNone(payload)
        self.assertEqual("plaintext_credentials_removed", error)

        # nothing the user sees may contain the value
        report = providers.credential_report(self.store.ai_settings(), self.store.files)
        blob = json.dumps(report, ensure_ascii=False)
        self.assertFalse(sentinel.channels(blob), "sentinel leaked into the credential report")
        self.assertTrue(report["legacy_plaintext"]["present"])
        self.assertIn("环境变量", report["legacy_plaintext"]["action"])

    # C01 - state/settings only expose a reference and a state
    def test_settings_expose_reference_not_value(self):
        sentinel = Sentinel("c01", "prefixed")
        with mock.patch.dict(os.environ, {sentinel.name: sentinel.value}, clear=False):
            self.store.update_ai_settings({"provider": "openai_compatible",
                                           "base_url": "https://api.example.invalid/v1",
                                           "model": "m", "api_key_env": sentinel.name})
            report = providers.credential_report(self.store.ai_settings(), self.store.files)
        blob = json.dumps(report, ensure_ascii=False)
        self.assertFalse(sentinel.channels(blob), "sentinel leaked into the report")
        self.assertEqual("bound", report["credential_state"])
        self.assertEqual(f"env:{sentinel.name}", report["credential_ref"])
        # a fingerprint is allowed, but it must not contain the value
        self.assertEqual(8, len(report["fingerprint"]))
        self.assertNotIn(sentinel.value[:6], report["fingerprint"])

    # C02 - an unregistered environment name is refused without touching the environment
    def test_unregistered_name_is_refused(self):
        sentinel = Sentinel("c02", "opaque")
        with mock.patch.dict(os.environ, {"AWS_SECRET_ACCESS_KEY": sentinel.value}, clear=False):
            real_get = os.environ.get
            seen = []
            with mock.patch.dict(os.environ, {}, clear=False):
                def watched(key, *args, **kwargs):
                    seen.append(key)
                    return real_get(key, *args, **kwargs)
                os.environ.get = watched
                try:
                    secret, code, _ref = providers.resolve_api_key("AWS_SECRET_ACCESS_KEY")
                finally:
                    del os.environ.get
        self.assertIsNone(secret)
        self.assertEqual(credentials.CODE_UNREGISTERED, code)
        self.assertEqual([], [name for name in seen if name == "AWS_SECRET_ACCESS_KEY"],
                         "an unregistered name must not be read at all")

    # C05 - value validation: control characters, over-long values, legal odd formats
    def test_value_validation_rejects_control_characters_and_length(self):
        cases = {
            "crlf": "sk-test-abcdefghij\r\nInjected: 1",
            "nul": "sk-test-abcdefghij\x00tail",
            "newline": "sk-test-abc\ndef",
            "spaces": " sk-test-abcdefghij ",
            "too_long": "k" * (credentials.MAX_KEY_BYTES + 1),
        }
        for label, value in cases.items():
            ok, code, message = credentials.validate_value(value)
            self.assertFalse(ok, label)
            self.assertEqual(credentials.CODE_INVALID, code, label)
            self.assertNotIn(value[:8], message, f"{label}: message echoed the value")

        for label, value in {"opaque": "t3st" + "a" * 20,
                             "symbols": "Zx9._-abcDEF123~+/",
                             "short": "abc123"}.items():
            ok, code, _message = credentials.validate_value(value)
            self.assertTrue(ok, f"{label} must be accepted: {code}")

        # an empty environment variable counts as missing, not as a valid empty key
        sentinel = Sentinel("c05", "opaque")
        with mock.patch.dict(os.environ, {sentinel.name: ""}, clear=False):
            secret, code, _ref = providers.resolve_api_key(sentinel.name)
        self.assertIsNone(secret)
        self.assertEqual(credentials.CODE_MISSING, code)

    # C07 - fail closed and keep manual reading available
    def test_missing_credential_fails_closed_without_any_request(self):
        sentinel = Sentinel("c07", "opaque")
        settings = {"provider": "openai_compatible", "model": "m",
                    "base_url": "https://api.example.invalid/v1",
                    "api_key_env": sentinel.name}
        with mock.patch.dict(os.environ, {}, clear=True):
            adapter = providers.build_adapter(settings, self.store.files)
            self.assertTrue(adapter.credential_missing)
            result = adapter.complete_json(providers.CompletionRequest(system="s", user="u",
                                                                      model="m"))
        self.assertFalse(result.ok)
        self.assertEqual(secret_guard.E_CREDENTIAL_MISSING, result.public_code)
        # manual reading is a different code path and stays available
        self.assertTrue(self.store.paper(PAPER)["fields"])
        self.assertTrue(self.store.files.papers.joinpath(f"{PAPER}.pdf").is_file())

    # C08 - the shipped package and the credential registry carry no secret
    def test_no_plaintext_secret_in_documents_or_registry(self):
        sentinel = Sentinel("c08", "prefixed")
        register(sentinel)
        for name in ("docs", "static"):
            folder = ROOT / name
            if folder.is_dir():
                hits = find_in_tree(folder, sentinel)
                self.assertEqual({}, hits, f"sentinel found under {name}: {list(hits)}")
        # the registry itself must not be persisted anywhere
        self.assertFalse((self.tmp / "secret_registry.json").exists())
        for item in self.tmp.rglob("*"):
            if item.is_file():
                self.assertFalse(sentinel.channels(item.read_bytes()),
                                 f"sentinel found in {item.name}")


# ================================================================= O: output and errors
class OutputBoundary(unittest.TestCase):
    """O01-O07: fixed errors, no raw diagnostics, secret echoes blocked."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sec-o-"))
        self.addCleanup(__import__("shutil").rmtree, self.tmp, ignore_errors=True)
        fresh_library_v1(self.tmp)
        self.store = server.Store(self.tmp)
        self.logs = []
        self.manager = analysis.AnalysisManager(self.store, log=self.logs.append)

    # O01 - an upstream body that echoes the key never reaches UI, library or log
    def test_upstream_error_body_never_reaches_outputs(self):
        sentinel = Sentinel("o01", "prefixed")
        with CountingSink(style="openai", reply="echo_error", status=401) as sink:
            adapter = providers.OpenAICompatibleAdapter(
                base_url=f"http://127.0.0.1:{sink.port}/v1", model="m", api_key=sentinel.value)
            result = adapter.complete_json(providers.CompletionRequest(system="s", user="u",
                                                                      model="m"))
        self.assertFalse(result.ok)
        self.assertEqual(401, result.http_status)
        self.assertEqual("stub-request-1", result.request_id)
        self.assertEqual(1, sink.count)
        self.assertTrue(sink.any_auth(), "the credential must reach the approved endpoint")

        blob = json.dumps({"as_dict": result.as_dict(), "public": result.public_message(),
                           "raw_attr": getattr(result, "message", "")}, ensure_ascii=False)
        self.assertFalse(sentinel.channels(blob), "sentinel leaked into the result DTO")
        self.assertEqual(secret_guard.E_AUTH_FAILED, result.public_code)
        self.assertNotIn("invalid_request", result.public_message(),
                         "the upstream sentence must not be shown")

        # the same object rendered into a log line stays clean
        line = json.dumps(secret_guard.pick(result.as_dict(), secret_guard.LOG_FIELDS),
                          ensure_ascii=False)
        self.assertFalse(sentinel.channels(line))

    # O02 - an exception that carries the key in its text is not propagated
    def test_exception_text_with_secret_is_not_propagated(self):
        sentinel = Sentinel("o02", "opaque")
        adapter = providers.OpenAICompatibleAdapter(
            base_url="http://127.0.0.1:9/v1", model="m", api_key=sentinel.value)
        with mock.patch.object(providers.urllib.request, "urlopen",
                               side_effect=providers.urllib.error.URLError(
                                   f"tunnel failed for {sentinel.value}")):
            result = adapter.complete_json(providers.CompletionRequest(system="s", user="u",
                                                                      model="m"))
        self.assertFalse(result.ok)
        blob = json.dumps(result.as_dict(), ensure_ascii=False)
        self.assertFalse(sentinel.channels(blob), "sentinel leaked from an exception message")
        self.assertNotIn("tunnel failed", result.as_dict()["message"])

    # O03 - a secret inside a valid-looking answer blocks the whole response
    def _card_with(self, mutate):
        """A card built from a synthetic one-line paper, optionally mutated."""
        import card_fixtures
        from pdftext import Page
        page = Page(pdf_page=1, text=PAGE_TEXT, char_count=len(PAGE_TEXT))
        return card_fixtures.build_card([page], language="zh", mutate=mutate)

    def test_secret_in_any_answer_field_blocks_the_response(self):
        sentinel = Sentinel("o03", "prefixed")
        register(sentinel)
        for field in ("overall", "terms", "warnings"):
            with self.subTest(field=field):
                card = self._card_with(lambda item: item.__setitem__(
                    field, sentinel.value if field != "terms" else [sentinel.value]))
                result = providers.AdapterResult(True, payload=card, raw_text=json.dumps(card))
                blocked = self.manager._secret_echo(PAPER, result)
                self.assertTrue(blocked, f"a secret in {field} must block the response")
                self.assertEqual(secret_guard.E_SECRET_ECHO_BLOCKED,
                                 self.store.analysis_meta(PAPER)["error_code"])
                message = json.dumps(self.store.analysis_meta(PAPER), ensure_ascii=False)
                self.assertFalse(sentinel.channels(message))
        # a clean card is not blocked
        clean = providers.AdapterResult(True, payload=self._card_with(lambda item: None),
                                        raw_text="{}")
        self.assertFalse(self.manager._secret_echo(PAPER, clean))

    # O04 - the failure file keeps codes and counts, never model text
    def test_failure_file_has_no_model_text(self):
        sentinel = Sentinel("o04", "opaque")
        register(sentinel)
        record = secret_guard.failure_record(
            schema=1, paper_id=PAPER, error_code="validation_failed",
            errors=[{"codes": ["field_completeness"], "details": ["field_completeness@answers"]}],
            raw_model_output=sentinel.value, raw_repair_output=sentinel.value,
            outcome="failed")
        self.assertNotIn("raw_model_output", record)
        self.assertNotIn("raw_repair_output", record)
        blob = json.dumps(record, ensure_ascii=False)
        self.assertFalse(sentinel.channels(blob), "sentinel leaked into the failure record")
        # the source no longer writes those fields at all
        source = (ROOT / "analysis.py").read_text(encoding="utf-8")
        self.assertNotIn("raw_model_output", source)
        self.assertNotIn("raw_repair_output", source)

    # O05 - every sentinel style and encoding is caught by the guard
    def test_guard_catches_all_sentinel_styles_and_encodings(self):
        styles = ["prefixed", "opaque", "symbols", "long", "short"]
        for style in styles:
            sentinel = Sentinel(f"o05-{style}", style)
            register(sentinel)
            for name, form in sentinel.encodings().items():
                with self.subTest(style=style, encoding=name):
                    blob = f"prefix {form} suffix"
                    if form == sentinel.value:
                        self.assertTrue(secret_guard.contains_secret(blob))
                    cleaned = secret_guard.redact(blob)
                    self.assertFalse(sentinel.channels(cleaned),
                                     f"{style}/{name} survived redaction")
        # ordinary text is untouched
        for benign in ("模型返回了 200，用时 4210 ms",
                       "https://api.example.com/v1/chat/completions",
                       "-webkit-mask-position:center"):
            self.assertEqual(benign, secret_guard.redact(benign))

    # O06 - shareable exports carry no credential and no paper text by default
    def test_shareable_outputs_are_field_whitelisted(self):
        sentinel = Sentinel("o06", "prefixed")
        register(sentinel)
        payload = {"schema_version": 1, "overall": {"title": "x"},
                   "answers": {"question": "q"}, "terms": [], "reader_questions": [],
                   "warnings": [], "secret_field": sentinel.value,
                   "verification": {"verified_quotes": 0}, "input": {"pages_used": [1]}}
        allowed = ("schema_version", "overall", "answers", "terms", "reader_questions",
                   "warnings", "verification", "input")
        picked = secret_guard.pick(payload, allowed)
        self.assertNotIn("secret_field", picked)
        self.assertFalse(sentinel.channels(json.dumps(picked, ensure_ascii=False)))

    # O07 - the startup log has no secrets, and a log call cannot smuggle a field
    def test_log_builder_drops_unknown_fields(self):
        sentinel = Sentinel("o07", "prefixed")
        register(sentinel)
        record = secret_guard.log_record(event="provider_call", paper_id=PAPER,
                                         api_key=sentinel.value, prompt=sentinel.value,
                                         authorization=f"Bearer {sentinel.value}",
                                         provider="stub")
        self.assertEqual({"event", "paper_id", "provider"}, set(record))
        self.assertFalse(sentinel.channels(json.dumps(record, ensure_ascii=False)))

    # the answers themselves must never contain a credential, on any path
    def test_document_and_library_never_hold_a_credential(self):
        sentinel = Sentinel("o-doc", "prefixed")
        register(sentinel)
        register(sentinel)
        self.assertFalse(sentinel.channels(json.dumps(self.store.snapshot(), ensure_ascii=False)))


# ============================================================ transport-side guarantees
class AdapterGuarantees(unittest.TestCase):
    """The request itself must not carry a credential beyond the Authorization header."""

    def test_prompt_never_carries_the_credential(self):
        sentinel = Sentinel("t01", "prefixed")
        with CountingSink(style="openai", reply="prose") as sink:
            adapter = providers.OpenAICompatibleAdapter(
                base_url=f"http://127.0.0.1:{sink.port}/v1", model="m", api_key=sentinel.value)
            adapter.complete_json(providers.CompletionRequest(
                system="system text", user="paper text", model="m"))
        self.assertEqual(1, sink.count)
        entry = sink.requests[0]
        self.assertTrue(entry["auth_header_present"])
        # the sink stores no values, so assert on the field names and sizes we recorded
        self.assertEqual(["max_tokens", "messages", "model", "response_format", "stream",
                          "temperature"], entry["body_field_names"])
        self.assertGreater(entry["message_chars"], 0)

    def test_prompt_containing_the_credential_is_refused_before_sending(self):
        sentinel = Sentinel("t02", "prefixed")
        with CountingSink(style="openai", reply="prose") as sink:
            adapter = providers.OpenAICompatibleAdapter(
                base_url=f"http://127.0.0.1:{sink.port}/v1", model="m", api_key=sentinel.value)
            result = adapter.complete_json(providers.CompletionRequest(
                system="s", user=f"please read {sentinel.value}", model="m"))
        self.assertEqual(0, sink.count, "a prompt carrying a credential must not be sent")
        self.assertEqual(secret_guard.E_SECRET_ECHO_BLOCKED, result.public_code)

    def test_missing_credential_makes_no_request(self):
        sentinel = Sentinel("t03", "opaque")
        with CountingSink(style="openai", reply="ok") as sink:
            with mock.patch.dict(os.environ, {}, clear=True):
                adapter = providers.build_adapter(
                    {"provider": "openai_compatible", "model": "m",
                     "base_url": f"http://127.0.0.1:{sink.port}/v1",
                     "api_key_env": sentinel.name})
                result = adapter.complete_json(providers.CompletionRequest(system="s", user="u",
                                                                          model="m"))
        self.assertEqual(0, sink.count, "no request may be made without a credential")
        self.assertFalse(result.ok)

    def test_oversized_response_is_refused(self):
        with CountingSink(style="openai", reply="huge") as sink:
            adapter = providers.OpenAICompatibleAdapter(
                base_url=f"http://127.0.0.1:{sink.port}/v1", model="m")
            result = adapter.complete_json(providers.CompletionRequest(system="s", user="u",
                                                                      model="m"))
        self.assertFalse(result.ok)
        self.assertEqual(secret_guard.E_INVALID_RESPONSE, result.public_code)

    def test_invalid_json_envelope_is_categorised(self):
        with CountingSink(style="openai", reply="bad_json") as sink:
            adapter = providers.OpenAICompatibleAdapter(
                base_url=f"http://127.0.0.1:{sink.port}/v1", model="m")
            result = adapter.complete_json(providers.CompletionRequest(system="s", user="u",
                                                                      model="m"))
        self.assertFalse(result.ok)
        self.assertEqual(providers.CATEGORY_INVALID_JSON, result.category)
        self.assertEqual(secret_guard.E_INVALID_RESPONSE, result.public_code)


if __name__ == "__main__":
    unittest.main(verbosity=2)
