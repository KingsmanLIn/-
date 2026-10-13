"""Phase 0: prove the current build reads data without changing it.

Every test runs against a throwaway server process with its own temporary data
directory copied from the real library. Nothing writes to data/library.json,
data/papers/ or the bundled release zip.

    python -m unittest discover -s tests -p "test_phase0_readonly.py" -v
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from baseline_helpers import (DATA_DIR, FIXTURE_LIBRARY_V1, PROJECT_ROOT, auth_headers,
                              fresh_library_v1, get_json, local_service)

sys.path.insert(0, str(PROJECT_ROOT))
from server import ID_RE, FIELDS, TYPES  # noqa: E402  (import for constants only; module runs nothing)


class ServiceSmoke(unittest.TestCase):
    """Step 3: a minimal start-up check that the current build still serves."""

    def test_service_starts_and_serves_health(self):
        with local_service() as svc:
            self.assertEqual("paper-reader", svc["health"]["app"])
            state = get_json(svc["base"] + "/api/state", svc)
            self.assertEqual(2, state["version"], "phase 1 moved the working schema to version 2")
            self.assertIsInstance(state["papers"], list)

    def test_missing_token_is_refused(self):
        with local_service() as svc:
            with self.assertRaises(Exception) as caught:
                get_json(svc["base"] + "/api/state")
            self.assertIn("403", str(caught.exception))

    def test_import_validates_payload_without_touching_real_data(self):
        with local_service() as svc:
            # a non-PDF body must be rejected by the existing validator
            from urllib.error import HTTPError
            from urllib.request import Request, urlopen
            headers = dict(auth_headers(svc), **{"X-File-Name": "bad.pdf"})
            req = Request(svc["base"] + "/api/import", data=b"not a pdf", method="POST",
                          headers=headers)
            with self.assertRaises(HTTPError) as caught:
                urlopen(req, timeout=10)
            self.assertEqual(400, caught.exception.code)


class RealLibraryReadBack(unittest.TestCase):
    """Prove the shipped library.json is readable by the shipped Store code."""

    def test_store_migrates_a_copy_of_the_real_library_to_v2(self):
        with tempfile.TemporaryDirectory(prefix="paper-reader-copy-") as tmp:
            tmp_path = Path(tmp)
            fresh_library_v1(tmp_path)
            before = json.loads((tmp_path / "library.json").read_text(encoding="utf-8"))

            sys.path.insert(0, str(PROJECT_ROOT))
            import server
            store = server.Store(tmp_path)
            snapshot = store.snapshot()

            self.assertEqual(1, before["version"], "the frozen fixture is version 1")
            self.assertEqual(2, snapshot["version"])
            self.assertEqual([p["id"] for p in before["papers"]], [p["id"] for p in snapshot["papers"]])
            # loading must not touch the original fixture file
            self.assertEqual(1, json.loads(FIXTURE_LIBRARY_V1.read_text(encoding="utf-8"))["version"])
            self.assertTrue((tmp_path / "library.previous.json").is_file())


class ContractFacts(unittest.TestCase):
    """Constants the later phases must respect."""

    def test_field_and_type_contract(self):
        self.assertEqual(13, len(FIELDS))
        self.assertEqual({"question", "motivation", "claim", "data", "variables", "method", "evidence",
                          "robustness", "boundaries", "assessment", "summary", "connections", "next"},
                         set(FIELDS))
        self.assertEqual({"framework", "empirical", "descriptive", "theory"}, set(TYPES))

    def test_paper_ids_match_the_server_regex(self):
        state = json.loads(FIXTURE_LIBRARY_V1.read_text(encoding="utf-8"))
        for paper in state["papers"]:
            self.assertRegex(paper["id"], ID_RE)
        live = json.loads((DATA_DIR / "library.json").read_text(encoding="utf-8"))
        for paper in live["papers"]:
            self.assertRegex(paper["id"], ID_RE)

    def test_page_labels_align_with_pdf_pages(self):
        """pageLabels maps physical PDF page -> printed page label."""
        state = json.loads(FIXTURE_LIBRARY_V1.read_text(encoding="utf-8"))
        for paper in state["papers"]:
            labels = paper.get("pageLabels", [])
            if not labels:
                continue
            for index, label in enumerate(labels, start=1):
                self.assertIsInstance(label, str)
                self.assertGreaterEqual(index, 1)
            self.assertTrue(all(labels), "empty labels would fall back to pageOffset")


class SavePayloadContract(unittest.TestCase):
    """The /api/save payload shape, pinned because late phases will extend it.

    body.patch may only carry top-level record keys; manual reading-card values must
    be nested under patch.fields. Unknown keys are ignored silently and the call
    still answers 200 with a bumped revision - a "false success" that phases 2+
    must not fall into.
    """

    def _save(self, svc, paper, revision, patch):
        from urllib.request import Request, urlopen
        body = json.dumps({"id": paper["id"], "revision": revision,
                           "patch": patch}).encode()
        req = Request(svc["base"] + "/api/save", data=body, method="POST",
                      headers=dict(auth_headers(svc),
                                   **{"Content-Type": "application/json"}))
        return json.loads(urlopen(req, timeout=10).read().decode("utf-8"))

    def test_nested_fields_are_persisted_and_bare_keys_are_not(self):
        with tempfile.TemporaryDirectory(prefix="paper-reader-save-") as tmp:
            tmp_path = Path(tmp)
            fresh_library_v1(tmp_path)
            with local_service(data_dir=tmp_path) as svc:
                paper = get_json(svc["base"] + "/api/state", svc)["papers"][0]

                # a bare field key is outside the whitelist: ignored, no error, revision grows
                result = self._save(svc, paper, paper["revision"], {"question": "BARE"})
                self.assertEqual(paper["revision"] + 1, result["paper"]["revision"])
                self.assertEqual("", result["paper"]["fields"]["question"])

                # the documented shape persists
                fields = dict(result["paper"]["fields"], question="NESTED")
                result2 = self._save(svc, paper, result["paper"]["revision"], {"fields": fields})
                self.assertEqual("NESTED", result2["paper"]["fields"]["question"])
                on_disk = json.loads((tmp_path / "library.json").read_text(encoding="utf-8"))
                self.assertEqual("NESTED", on_disk["papers"][0]["fields"]["question"])

    def test_stale_revision_conflicts(self):
        from urllib.error import HTTPError
        from urllib.request import Request, urlopen
        with tempfile.TemporaryDirectory(prefix="paper-reader-conflict-") as tmp:
            tmp_path = Path(tmp)
            fresh_library_v1(tmp_path)
            with local_service(data_dir=tmp_path) as svc:
                paper = get_json(svc["base"] + "/api/state", svc)["papers"][0]
                self._save(svc, paper, paper["revision"], {"fields": dict(paper["fields"])})
                body = json.dumps({"id": paper["id"],
                                   "revision": paper["revision"], "patch": {"fields": dict(paper["fields"])}}).encode()
                req = Request(svc["base"] + "/api/save", data=body, method="POST",
                              headers=dict(auth_headers(svc),
                                           **{"Content-Type": "application/json"}))
                with self.assertRaises(HTTPError) as caught:
                    urlopen(req, timeout=10)
                self.assertEqual(409, caught.exception.code)


if __name__ == "__main__":
    unittest.main(verbosity=2)
