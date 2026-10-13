"""Unit tests for the reply-diagnosis changes (no network, no provider call).

Run:  D:\\harness home\\pr-lab\\runtime\\python.exe D:\\harness home\\pr-lab\\tests\\test_diagnosis.py
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import analysis          # noqa: E402
import providers         # noqa: E402
import reading_card      # noqa: E402
import secret_guard      # noqa: E402


class ReplyDiagnosis(unittest.TestCase):
    def diagnose(self, finish, content, message):
        choice = {"finish_reason": finish, "message": message}
        return providers.HttpAdapter._reply_diagnosis(choice, message, content)

    def test_truncated_reasoning_is_named(self):
        """The exact failure seen in production: all output tokens went to reasoning."""
        result = self.diagnose("length", "", {"role": "assistant", "content": "",
                                              "reasoning_content": "x" * 5000})
        self.assertIsNotNone(result)
        self.assertEqual(secret_guard.E_OUTPUT_TRUNCATED, result.public_code)
        self.assertEqual("empty_content", result.parse_detail)
        self.assertEqual(providers.CATEGORY_INVALID_JSON, result.category)

    def test_reasoning_only_without_length(self):
        result = self.diagnose("stop", "", {"role": "assistant", "content": "",
                                            "reasoning_content": "thinking"})
        self.assertIsNotNone(result)
        self.assertEqual(secret_guard.E_REASONING_ONLY, result.public_code)
        self.assertEqual("reasoning_only", result.parse_detail)

    def test_finish_length_alone_is_enough(self):
        result = self.diagnose("length", "   ", {"role": "assistant", "content": "   "})
        self.assertIsNotNone(result)
        self.assertEqual(secret_guard.E_OUTPUT_TRUNCATED, result.public_code)

    def test_content_present_is_not_diagnosed(self):
        self.assertIsNone(self.diagnose("stop", '{"a": 1}', {"content": '{"a": 1}'}))

    def test_plain_empty_content_falls_through_to_the_parser(self):
        """No reasoning, no truncation: the generic JSON failure path still applies."""
        self.assertIsNone(self.diagnose("stop", "", {"content": ""}))

    def test_unknown_message_shape_is_not_diagnosed(self):
        self.assertIsNone(self.diagnose("length", "", "not-a-dict"))

    def test_new_codes_are_in_the_public_vocabulary(self):
        self.assertIn(secret_guard.E_OUTPUT_TRUNCATED, secret_guard.MESSAGES)
        self.assertIn(secret_guard.E_REASONING_ONLY, secret_guard.MESSAGES)
        self.assertIn(secret_guard.E_OUTPUT_TRUNCATED, secret_guard.CODES)
        # the sentences must point at the output ceiling, not at retrying
        self.assertIn("输出上限", secret_guard.MESSAGES[secret_guard.E_OUTPUT_TRUNCATED])


class LoadJsonDiagnosis(unittest.TestCase):
    def load(self, text):
        request = providers.CompletionRequest(system="s", user="u")
        return providers.HttpAdapter._load_json(text, request)

    def test_truncated_json_reports_the_offset(self):
        payload, failure = self.load('{"overall": {"title": "abc"')
        self.assertIsNone(payload)
        self.assertEqual(providers.CATEGORY_INVALID_JSON, failure.category)
        self.assertTrue(failure.parse_detail.startswith("bad_json@"),
                        failure.parse_detail)
        self.assertTrue(failure.parse_detail.split("@", 1)[1].isdigit(),
                        failure.parse_detail)

    def test_empty_content_is_reported_as_such(self):
        _, failure = self.load("")
        self.assertEqual("empty_content", failure.parse_detail)

    def test_valid_json_has_no_failure(self):
        payload, failure = self.load('{"overall": {}, "answers": {}}')
        self.assertIsNone(failure)
        self.assertIsInstance(payload, dict)

    def test_duplicate_key_is_reported_by_code(self):
        _, failure = self.load('{"a": 1, "a": 2}')
        self.assertEqual("duplicate_key", failure.parse_detail)

    def test_diagnosis_never_carries_content(self):
        """Only the fixed vocabulary plus digits may appear in parse_detail."""
        secret = "sk-live-SECRETVALUE1234567890"
        _, failure = self.load('{"note": "' + secret + '"')
        self.assertNotIn("SECRET", failure.parse_detail)
        self.assertRegex(failure.parse_detail, r"^[a-z_]{1,32}(@\d+)?$")


class LogWhitelist(unittest.TestCase):
    def test_parse_detail_and_public_code_reach_the_log(self):
        record = secret_guard.log_record(event="provider_call", parse_detail="bad_json@42",
                                         public_code=secret_guard.E_OUTPUT_TRUNCATED)
        self.assertEqual("bad_json@42", record["parse_detail"])
        self.assertEqual(secret_guard.E_OUTPUT_TRUNCATED, record["public_code"])

    def test_unlisted_fields_are_still_dropped(self):
        record = secret_guard.log_record(event="provider_call", raw_text="leaked")
        self.assertNotIn("raw_text", record)


class AdviceSelection(unittest.TestCase):
    def test_public_error_keeps_the_specific_code(self):
        result = providers.AdapterResult(False, category=providers.CATEGORY_INVALID_JSON,
                                         public_code=secret_guard.E_OUTPUT_TRUNCATED)
        code, message = analysis._public_error(result)
        self.assertEqual(secret_guard.E_OUTPUT_TRUNCATED, code)
        self.assertIn("输出上限", message)

    def test_advice_is_code_specific(self):
        self.assertIn(secret_guard.E_OUTPUT_TRUNCATED, analysis.ADVICE_BY_CODE)
        self.assertIn(secret_guard.E_REASONING_ONLY, analysis.ADVICE_BY_CODE)
        self.assertIn("提高输出上限",
                      analysis.ADVICE_BY_CODE[secret_guard.E_OUTPUT_TRUNCATED])

    def test_generic_code_still_has_advice(self):
        """A genuine malformed reply must still be reported as a JSON problem."""
        result = providers.AdapterResult(False, category=providers.CATEGORY_INVALID_JSON,
                                         public_code=secret_guard.E_INVALID_RESPONSE)
        code, _ = analysis._public_error(result)
        self.assertEqual(secret_guard.E_INVALID_RESPONSE, code)
        self.assertNotIn(code, analysis.ADVICE_BY_CODE)   # falls back to the category text


if __name__ == "__main__":
    unittest.main(verbosity=2)
