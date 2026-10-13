"""Phase 1 API tests for the credential surface.

Drives the real HTTP handler with a fictional sentinel and asserts on what the browser
receives. No value is ever printed; failures name the channel.

    python -m unittest discover -s tests/security -p "test_phase1_api.py"
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tests" / "security"))

import credentials     # noqa: E402
import providers       # noqa: E402
import secret_guard    # noqa: E402
import server          # noqa: E402
from baseline_helpers import fresh_library_v1  # noqa: E402
from stubs import Sentinel  # noqa: E402

TOKEN = "phase1-test-token"


class CredentialApiCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sec-api-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        fresh_library_v1(self.tmp)
        self.store = server.Store(self.tmp)
        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0),
                                                server.handler_for(self.store, TOKEN, None))
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self._stop)

    def _stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def call(self, path, body=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            url, data=data, method="POST" if data else "GET",
            headers={"X-Reader-Token": TOKEN, "Host": f"127.0.0.1:{self.port}",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))
        except urllib.error.URLError as exc:                      # pragma: no cover
            raise AssertionError(f"request failed: {exc.reason}")

    def test_saving_an_unregistered_environment_name_is_refused(self):
        status, payload = self.call("/api/settings", {"api_key_env": "AWS_SECRET_ACCESS_KEY"})
        self.assertEqual(400, status, payload)
        self.assertIn("没有登记", payload["error"])
        self.assertEqual("", self.store.ai_settings()["api_key_env"],
                         "a refused reference must not be stored")

    def test_saving_a_registered_name_stores_only_the_reference(self):
        sentinel = Sentinel("api-save", "prefixed")
        status, payload = self.call("/api/settings", {
            "provider": "openai_compatible", "model": "m",
            "base_url": "https://api.example.invalid/v1", "api_key_env": sentinel.name})
        self.assertEqual(200, status, payload)
        self.assertEqual(f"env:{sentinel.name}", payload["ai"]["api_key_env"])
        blob = json.dumps(payload, ensure_ascii=False)
        self.assertFalse(sentinel.channels(blob), "the response must not carry a value")
        # the reference is registered but the environment does not hold it (yet)
        self.assertEqual("missing", payload["credential"]["credential_state"])
        self.assertEqual(credentials.CODE_MISSING, payload["credential"]["credential_code"])
        self.assertNotIn("api_key\"", json.dumps(payload["ai"]))
        # the library on disk holds the reference, never a value
        stored = (self.tmp / "library.json").read_text(encoding="utf-8")
        self.assertIn(sentinel.name, stored)

    def test_bound_credential_is_never_echoed_by_any_endpoint(self):
        sentinel = Sentinel("api-bound", "prefixed")
        with mock.patch.dict(os.environ, {sentinel.name: sentinel.value}, clear=False):
            self.call("/api/settings", {
                "provider": "openai_compatible", "model": "m",
                "base_url": "https://api.example.invalid/v1", "api_key_env": sentinel.name})
            for path in ("/api/ai-settings", "/api/state", "/api/health", "/api/analysis"):
                status, payload = self.call(path)
                self.assertEqual(200, status, path)
                blob = json.dumps(payload, ensure_ascii=False)
                self.assertFalse(sentinel.channels(blob),
                                 f"the sentinel reached {path}")
        # and not in the file on disk either
        for item in self.tmp.rglob("*"):
            if item.is_file():
                self.assertFalse(sentinel.channels(item.read_bytes()),
                                 f"sentinel found in {item.relative_to(self.tmp)}")

    def test_services_report_marks_the_legacy_file_without_reading_it(self):
        sentinel = Sentinel("api-legacy", "opaque")
        (self.tmp / "credentials.json").write_text(
            json.dumps({"PAPER_READER_LEGACY_KEY": sentinel.value}), encoding="utf-8")
        status, payload = self.call("/api/ai-settings")
        self.assertEqual(200, status)
        blob = json.dumps(payload, ensure_ascii=False)
        self.assertFalse(sentinel.channels(blob), "the report must not read the file")
        self.assertTrue(payload["credential"]["legacy_plaintext"]["present"])
        self.assertIn("不再读取", payload["credential"]["legacy_plaintext"]["action"])

    def test_registry_hint_lists_examples_without_values(self):
        status, payload = self.call("/api/ai-settings")
        self.assertEqual(200, status)
        hint = payload["registry"]
        self.assertEqual("env:", hint["prefix"])
        self.assertIn("PAPER_READER_", hint["owned_prefixes"])
        self.assertTrue(all(name.isupper() for name in hint["known_names"]))

    def test_key_value_cannot_be_posted_as_a_setting(self):
        status, payload = self.call("/api/settings", {"api_key": "sk-test-should-be-refused"})
        self.assertEqual(400, status, payload)
        self.assertIn("环境变量名", payload["error"])
        self.assertNotIn("sk-test-should-be-refused", json.dumps(payload))


if __name__ == "__main__":
    unittest.main(verbosity=2)
