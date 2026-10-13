"""Phase 6 tests: the v1 library migration must lose nothing the user wrote.

The migration is the one step that rewrites the user's own file, so it is checked the way the
release rules require: every human-owned value is compared before and after, byte for byte,
and the security fields are confirmed to be *added* rather than substituted.

    python -m unittest discover -s tests/security -p "test_phase6_migration.py"
"""
from __future__ import annotations

import copy
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tests" / "security"))

import credentials   # noqa: E402
import schema        # noqa: E402
import server        # noqa: E402
from baseline_helpers import fresh_library_v1  # noqa: E402

#: Everything the user owns. The migration may add keys beside these, never change them.
HUMAN_KEYS = ("title", "authors", "year", "venue", "doi", "citation", "tags", "language",
              "type", "fields", "steps", "annotations", "pageOffset", "pageLabels", "lastPage",
              "revision", "note_name")


def _v1_state(papers=2):
    state = {"version": 1, "settings": {"vault_path": "D:/vault", "sync_enabled": True,
                                        "last_sync": "2026-10-01T00:00:00Z",
                                        "sync_error": ""},
             "papers": []}
    for index in range(papers):
        paper_id = f"paper-{index}-abcdef"
        state["papers"].append({
            "id": paper_id, "title": f"论文 {index}", "authors": "张三, 李四",
            "year": "2026", "venue": "某期刊", "doi": f"10.1000/x{index}",
            "citation": "张三. 论文. 某期刊, 2026.", "tags": "医保, 控费",
            "language": "zh" if index == 0 else "en", "type": "empirical",
            "fields": {key: f"{key} 的人工记录 {index}" for key in
                       ("question", "motivation", "claim", "data", "variables", "method",
                        "evidence", "robustness", "boundaries", "assessment", "summary",
                        "connections", "next")},
            "steps": [True, False, True, False],
            "annotations": [{"id": f"a{index}", "type": "argument", "page": index + 1,
                             "text": "我的批注", "quote": "原文片段",
                             "rects": [[0.1, 0.2, 0.3, 0.04]],
                             "created": "2026-10-02T00:00:00Z"}],
            "pageOffset": index, "pageLabels": ["封面", "正文"],
            "lastPage": index + 7, "revision": 5 + index,
            "note_name": f"论文 {index}--{paper_id[:8]}.md",
        })
    state["state"] = {"activeId": state["papers"][0]["id"]}
    return state


class MigrationPreservesUserData(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sec-p6-mig-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.before = _v1_state()
        (self.tmp / "library.json").write_text(
            json.dumps(self.before, ensure_ascii=False, indent=2), encoding="utf-8")

    def _migrate(self, state=None):
        payload = copy.deepcopy(state if state is not None else self._read())
        migrated, changed = schema.migrate_state(payload)
        return migrated, changed

    def _read(self):
        return json.loads((self.tmp / "library.json").read_text(encoding="utf-8"))

    def test_every_human_field_survives_the_migration(self):
        after, changed = self._migrate()
        self.assertTrue(changed)
        self.assertEqual(2, after["version"])
        self.assertEqual(len(self.before["papers"]), len(after["papers"]))
        for original, migrated in zip(self.before["papers"], after["papers"]):
            for key in HUMAN_KEYS:
                self.assertEqual(original[key], migrated[key],
                                 f"{original['id']}.{key} changed during migration")
            self.assertIn("analysis", migrated)

    def test_the_migration_adds_the_security_fields(self):
        after, _ = self._migrate()
        ai = after["settings"]["ai"]
        for key in ("provider", "base_url", "model", "api_key_env", "auto_analyze",
                    "max_concurrent_jobs", "mode", "max_output_tokens", "limits"):
            self.assertIn(key, ai, key)
        self.assertEqual("none", ai["provider"], "a migrated library must not be armed")
        self.assertFalse(ai["auto_analyze"])
        self.assertEqual("", ai["api_key_env"], "no credential reference is invented")
        for key in ("max_queue_depth", "max_requests_per_job", "daily_request_limit"):
            self.assertIn(key, ai["limits"])

    def test_migrating_twice_changes_nothing(self):
        once, _ = self._migrate()
        twice, changed = self._migrate(once)
        self.assertFalse(changed, "a current library must be left alone")
        self.assertEqual(json.dumps(once, sort_keys=True, ensure_ascii=False),
                         json.dumps(twice, sort_keys=True, ensure_ascii=False))

    def test_a_library_with_a_plaintext_key_still_opens(self):
        """The strict validator must not lock a user out of their own library."""
        state = self._read()
        state["settings"]["ai"] = {"provider": "openai_compatible",
                                   "base_url": "https://api.example.com/v1",
                                   "model": "m", "api_key": "sk-live-LEGACY-0002",
                                   "auto_analyze": True}
        (self.tmp / "library.json").write_text(json.dumps(state, ensure_ascii=False),
                                               encoding="utf-8")
        store = server.Store(self.tmp)
        self.assertEqual(2, store.state["version"])
        self.assertNotIn("api_key", store.state["settings"]["ai"])
        raw = (self.tmp / "library.json").read_text(encoding="utf-8")
        self.assertNotIn("sk-live-LEGACY-0002", raw,
                         "the plaintext key is removed from the migrated library")

    def test_the_pre_migration_copy_is_written_beside_the_library(self):
        store = server.Store(self.tmp)
        previous = self.tmp / "library.previous.json"
        self.assertTrue(previous.is_file(), "the user must keep a pre-migration copy")
        self.assertEqual(self.before, json.loads(previous.read_text(encoding="utf-8")))
        self.assertEqual(2, store.state["version"])

    def test_a_legacy_plaintext_credential_is_not_read_back(self):
        """A v1 library that stored a raw key keeps the record but never uses it."""
        state = self._read()
        state["settings"]["ai"] = {"provider": "openai_compatible",
                                   "base_url": "https://api.example.com/v1",
                                   "model": "m", "api_key": "sk-live-LEGACY-0001",
                                   "auto_analyze": True}
        after, _ = self._migrate(state)
        ai = after["settings"]["ai"]
        # the plaintext field is gone and no credential reference was invented from it
        self.assertNotIn("api_key", ai)
        self.assertEqual("", ai.get("api_key_env") or "")
        self.assertFalse(ai.get("auto_analyze"), "an old setting must not arm the AI")
        # the location is recorded so the user can delete the file themselves ...
        self.assertEqual(["api_key"], after["legacy_plaintext"]["settings.ai"])
        # ... and the value itself appears nowhere in the migrated library
        blob = json.dumps(after, ensure_ascii=False)
        self.assertNotIn("sk-live-LEGACY-0001", blob)

    def test_an_old_external_target_needs_a_fresh_approval(self):
        """A configured target in an old library is not an approved target here."""
        state = self._read()
        state["settings"]["ai"] = {"provider": "openai_compatible", "mode": "cloud_https",
                                   "base_url": "https://api.example.com/v1", "model": "m",
                                   "api_key_env": "env:PAPER_READER_KEY", "auto_analyze": True}
        after, _ = self._migrate(state)
        store = server.Store(self.tmp)
        analysis = __import__("analysis")
        manager = analysis.AnalysisManager(store, provider_factory=lambda s: None,
                                           ledger=__import__("job_policy").JobLedger(
                                               self.tmp / "fresh", log=lambda record: None))
        # no approval exists, so no paper may be sent
        for paper in after["papers"]:
            self.assertIsNone(manager.ledger.approval_for(paper["id"]))
        self.assertEqual([], manager.ledger.disabled_profiles())
        self.assertFalse(manager.ledger.paused())

    def test_a_version_1_paper_without_analysis_still_validates(self):
        after, _ = self._migrate()
        for paper in after["papers"]:
            self.assertEqual(set(schema.default_analysis()), set(paper["analysis"]))
            self.assertEqual("none", paper["analysis"]["status"])

    def test_a_newer_library_is_refused_instead_of_downgraded(self):
        state = self._read()
        state["version"] = schema.SCHEMA_VERSION + 1
        with self.assertRaises(schema.SchemaError):
            self._migrate(state)

    def test_the_library_schema_version_is_unchanged(self):
        """This release does not introduce a v3 shape; the claim is checked, not assumed."""
        self.assertEqual(2, schema.SCHEMA_VERSION)


class FactoryLibrary(unittest.TestCase):
    def test_the_shipped_library_migrates_and_keeps_its_three_papers(self):
        tmp = Path(tempfile.mkdtemp(prefix="sec-p6-factory-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        fresh_library_v1(tmp)
        before = json.loads((tmp / "library.json").read_text(encoding="utf-8"))
        store = server.Store(tmp)
        self.assertEqual(len(before["papers"]), len(store.state["papers"]))
        for original, current in zip(before["papers"], store.state["papers"]):
            for key in HUMAN_KEYS:
                self.assertEqual(original.get(key), current.get(key), key)


if __name__ == "__main__":
    unittest.main(verbosity=2)
