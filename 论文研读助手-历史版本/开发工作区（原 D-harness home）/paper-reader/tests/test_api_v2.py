"""Phase 1: HTTP surface of the new data layer.

Covers the read/write paths added for schema version 2 that a browser will use,
plus the guarantee that /api/save cannot touch AI metadata.

    python -m unittest discover -s tests -p "test_api_v2.py" -v
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from baseline_helpers import auth_headers, PROJECT_ROOT, fresh_library_v1, get_json, local_service

sys.path.insert(0, str(PROJECT_ROOT))
import schema      # noqa: E402

PAPER_MARK = "api-v2-marker"


class ApiV2(unittest.TestCase):
    def setUp(self):
        import contextlib
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.tmp = Path(tempfile.mkdtemp(prefix="api-v2-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        fresh_library_v1(self.tmp)
        self.svc = self.stack.enter_context(local_service(data_dir=self.tmp))
        self.base, self.token = self.svc["base"], self.svc["token"]
        self.svc = self.svc

    def _post(self, path, body):
        """POST as the page does: cookie session plus the CSRF header."""
        req = Request(self.base + path, data=json.dumps(body).encode(), method="POST",
                      headers=dict(auth_headers(self.svc),
                                   **{"Content-Type": "application/json"}))
        try:
            return json.loads(urlopen(req, timeout=10).read().decode("utf-8"))
        except HTTPError as exc:
            raise AssertionError(f"POST {path} -> {exc.code} {exc.read().decode('utf-8', 'replace')}") from exc

    def _post_raw(self, path, body):
        """POST without any credential; returns the HTTP status instead of raising."""
        req = Request(self.base + path, data=json.dumps(body).encode(), method="POST",
                      headers={"Content-Type": "application/json"})
        try:
            with urlopen(req, timeout=10) as response:
                return response.status, response.read().decode("utf-8")
        except HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")

    def _get(self, path):
        return get_json(self.base + path, self.svc)

    def test_state_reports_schema_version_and_defaults(self):
        state = self._get("/api/state")
        self.assertEqual(2, state["version"])
        self.assertEqual(schema.AI_SETTINGS_DEFAULTS, state["settings"]["ai"])
        for paper in state["papers"]:
            self.assertEqual(set(schema.ANALYSIS_DEFAULTS), set(paper["analysis"]))
            self.assertEqual("none", paper["analysis"]["status"])

    def test_ai_settings_round_trip_without_storing_a_key(self):
        self.assertEqual(schema.AI_SETTINGS_DEFAULTS, self._get("/api/ai-settings")["ai"])
        updated = self._post("/api/ai-settings", {"ai": {"provider": "ollama", "base_url": "http://127.0.0.1:11434",
                                                        "model": "qwen2.5:7b", "api_key_env": "OLLAMA_API_KEY",
                                                        "auto_analyze": True, "max_concurrent_jobs": 2}})["ai"]
        self.assertEqual("ollama", updated["provider"])
        self.assertTrue(updated["auto_analyze"])
        self.assertEqual(2, updated["max_concurrent_jobs"])
        self.assertEqual(updated, self._get("/api/ai-settings")["ai"])

        stored = json.loads((self.tmp / "library.json").read_text(encoding="utf-8"))
        self.assertEqual("env:OLLAMA_API_KEY", stored["settings"]["ai"]["api_key_env"])
        self.assertNotIn("sk-", json.dumps(stored))
        per_paper = json.dumps(stored["papers"])
        self.assertNotIn(PAPER_MARK, per_paper)

    def test_rejected_ai_settings(self):
        """Validation errors are 400; the credential is present, so it is not an auth refusal."""
        for bad in ({"provider": "openai"}, {"max_concurrent_jobs": 9}, {"auto_analyze": "yes"},
                    {"api_key": "sk-secret"}, {"api_key_env": "sk-secret-value"}):
            request = Request(self.base + "/api/ai-settings",
                              data=json.dumps({"ai": bad}).encode(), method="POST",
                              headers=dict(auth_headers(self.svc),
                                           **{"Content-Type": "application/json"}))
            try:
                urlopen(request, timeout=10)
                status, text = 200, ""
            except HTTPError as exc:
                status, text = exc.code, exc.read().decode("utf-8", "replace")
            self.assertEqual(400, status, f"{bad} -> {text}")
        self.assertEqual(schema.AI_SETTINGS_DEFAULTS, self._get("/api/ai-settings")["ai"],
                         "a rejected update must not change the stored settings")

    def test_missing_token_is_refused(self):
        """No cookie and no header token: the write is refused."""
        status, text = self._post_raw("/api/ai-settings", {"ai": {"provider": "ollama"}})
        self.assertEqual(403, status, text)
        self.assertIn("失效", text)

    def test_cookie_without_csrf_is_refused(self):
        """A session cookie alone is not enough for a state-changing request."""
        request = Request(self.base + "/api/ai-settings",
                          data=json.dumps({"ai": {"provider": "ollama"}}).encode(),
                          method="POST",
                          headers={"Cookie": self.svc["cookie"],
                                   "Content-Type": "application/json"})
        with self.assertRaises(HTTPError) as caught:
            urlopen(request, timeout=10)
        self.assertEqual(403, caught.exception.code)
        self.assertIn("csrf", caught.exception.read().decode("utf-8", "replace"))

    def test_analysis_endpoint_is_read_only_metadata(self):
        paper_id = self._get("/api/state")["papers"][0]["id"]
        payload = self._get(f"/api/analysis/{paper_id}")
        self.assertEqual(paper_id, payload["paper_id"])
        self.assertEqual("none", payload["analysis"]["status"])
        self.assertIsNone(payload["answer"])
        self.assertIn("不存在", payload["answer_error"])

    def test_analysis_endpoint_rejects_bad_ids(self):
        for bad in ("../library", "UPPER", "short"):
            with self.assertRaises(HTTPError, msg=bad) as caught:
                urlopen(Request(self.base + f"/api/analysis/{bad}",
                            headers=auth_headers(self.svc, csrf=False)), timeout=10)
            self.assertEqual(400, caught.exception.code)

    def test_save_cannot_write_analysis_metadata(self):
        paper = self._get("/api/state")["papers"][0]
        before = json.dumps(paper["analysis"], sort_keys=True)
        self._post("/api/save", {"token": self.token, "id": paper["id"], "revision": paper["revision"],
                                 "patch": {"analysis": {"status": "ready", "provider": "ollama"},
                                           "fields": dict(paper["fields"], question=PAPER_MARK)}})
        after = self._get("/api/state")["papers"][0]
        self.assertEqual(PAPER_MARK, after["fields"]["question"])
        self.assertEqual(before, json.dumps(after["analysis"], sort_keys=True),
                         "browser saves must never write AI metadata")

    def test_backup_includes_manifest_and_restores_into_a_fresh_library(self):
        raw = urlopen(Request(self.base + "/api/backup",
                              headers=auth_headers(self.svc, csrf=False)), timeout=60).read()
        import io
        import zipfile
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            names = set(archive.namelist())
            self.assertIn("manifest.json", names)
            self.assertEqual(3, json.loads(archive.read("manifest.json").decode("utf-8"))["papers"])

        target = Path(tempfile.mkdtemp(prefix="api-v2-restore-"))
        self.addCleanup(shutil.rmtree, target, ignore_errors=True)
        with local_service(data_dir=target) as fresh:
            request = Request(fresh["base"] + "/api/restore", data=raw, method="POST",
                              headers=auth_headers(fresh))
            result = json.loads(urlopen(request, timeout=60).read().decode("utf-8"))
            self.assertEqual(3, result["imported"])
            self.assertEqual(2, result["state"]["version"])
            self.assertEqual("none", result["state"]["settings"]["ai"]["provider"])
            self.assertFalse(result["state"]["settings"]["ai"]["auto_analyze"])
            self.assertFalse(result["state"]["settings"]["sync_enabled"])

    def test_health_reports_the_new_app_version(self):
        # the改造 release version; the shipped package is built from this same constant
        self.assertEqual("1.3.1", self._get("/api/health")["version"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
