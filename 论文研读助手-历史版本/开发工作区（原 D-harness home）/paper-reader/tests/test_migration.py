"""Phase 1: schema version 1 -> 2 migration.

The migration must be idempotent and must not touch anything the user wrote:
fields, steps, annotations, lastPage, pageOffset/pageLabels, revision, note_name,
Obsidian configuration and timestamps all have to survive byte for byte.

    python -m unittest discover -s tests -p "test_migration.py" -v
"""
from __future__ import annotations

import copy
import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from baseline_helpers import DATA_DIR, FIXTURE_LIBRARY_V1, PROJECT_ROOT, fresh_library_v1

sys.path.insert(0, str(PROJECT_ROOT))
import schema      # noqa: E402
from server import Store, validate_paper  # noqa: E402

READER_KEYS = ("fields", "steps", "annotations", "lastPage", "pageOffset", "pageLabels",
               "revision", "created", "updated", "title", "authors", "year", "venue",
               "doi", "tags", "citation", "type", "language", "archived", "note_name", "guideKey")


def reader_part(state):
    """Everything the reader owns, for before/after comparison."""
    return {p["id"]: {key: p.get(key) for key in READER_KEYS} for p in state["papers"]}


def sync_part(state):
    return {k: state["settings"].get(k) for k in ("vault_path", "sync_enabled", "last_sync", "sync_error")}


class RealLibraryMigration(unittest.TestCase):
    """Uses a copy of the shipped version 1 library; the original is never opened for writing."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="migrate-"))
        fresh_library_v1(self.tmp)
        self.before = json.loads((self.tmp / "library.json").read_text(encoding="utf-8"))
        self.assertEqual(1, self.before["version"], "fixture must be a real version 1 library")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_migration_is_idempotent(self):
        """Requirement: run the migration twice; the second run changes nothing."""
        store = Store(self.tmp)
        self.assertTrue(store.migrated, "first load must report a migration")
        after_first = (self.tmp / "library.json").read_bytes()

        second = Store(self.tmp)
        self.assertFalse(second.migrated, "second load must be a no-op")
        self.assertEqual(after_first, (self.tmp / "library.json").read_bytes(),
                         "a second migration rewrote the library")
        self.assertEqual(2, second.state["version"])

    def test_reader_data_is_preserved_exactly(self):
        store = Store(self.tmp)
        after = store.snapshot()
        self.assertEqual(reader_part(self.before), reader_part(after))
        self.assertEqual(sync_part(self.before), sync_part(after))
        for paper in self.before["papers"]:
            self.assertEqual(paper["fields"], next(p for p in after["papers"] if p["id"] == paper["id"])["fields"])

    def test_settings_ai_block_is_added_with_safe_defaults(self):
        store = Store(self.tmp)
        ai = store.state["settings"]["ai"]
        self.assertEqual(schema.AI_SETTINGS_DEFAULTS, ai)
        self.assertEqual("none", ai["provider"])
        self.assertFalse(ai["auto_analyze"], "migration must not arm the AI")
        self.assertIn("api_key_env", ai)
        self.assertNotIn("api_key", ai)

    def test_every_paper_gains_analysis_metadata(self):
        store = Store(self.tmp)
        for paper in store.state["papers"]:
            meta = paper["analysis"]
            self.assertEqual(set(schema.ANALYSIS_DEFAULTS), set(meta))
            self.assertEqual("none", meta["status"], "migration must not queue work")
            self.assertEqual("", meta["error_code"])
            self.assertEqual("", meta["answer_file"])
            self.assertEqual(64, len(meta["source_sha256"]))
            self.assertTrue(schema.SHA256_RE.match(meta["source_sha256"]))
            self.assertEqual(hashlib.sha256((self.tmp / "papers" / f"{paper['id']}.pdf").read_bytes()).hexdigest(),
                             meta["source_sha256"])

    def test_previous_library_keeps_the_pre_migration_bytes(self):
        """library.previous.json must hold the version 1 file, so a downgrade is possible."""
        Store(self.tmp)
        previous = self.tmp / "library.previous.json"
        self.assertTrue(previous.is_file())
        self.assertEqual(FIXTURE_LIBRARY_V1.read_bytes(), previous.read_bytes())
        self.assertEqual(1, json.loads(previous.read_text(encoding="utf-8"))["version"])
        self.assertEqual(2, json.loads((self.tmp / "library.json").read_text(encoding="utf-8"))["version"])

    def test_no_analysis_or_extracted_directories_are_created_unused(self):
        Store(self.tmp)
        self.assertTrue((self.tmp / "analyses").is_dir())
        self.assertTrue((self.tmp / "extracted").is_dir())
        self.assertEqual([], list((self.tmp / "analyses").iterdir()))
        self.assertEqual([], list((self.tmp / "extracted").iterdir()))


class MigrationEdgeCases(unittest.TestCase):
    def _library(self, papers=(), settings=None, version=1):
        return {"version": version,
                "settings": settings or {"vault_path": "/tmp/vault", "sync_enabled": True,
                                         "last_sync": "2026-10-09T17:59:33.538+00:00", "sync_error": ""},
                "papers": list(papers)}

    def _paper(self, **overrides):
        paper = {"id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "title": "t", "authors": "", "year": "",
                 "venue": "", "doi": "", "citation": "", "tags": "", "language": "zh", "type": "empirical",
                 "fields": {k: "" for k in ("question", "motivation", "claim", "data", "variables", "method",
                                            "evidence", "robustness", "boundaries", "assessment", "summary",
                                            "connections", "next")},
                 "steps": [True, False, False, False], "annotations": [], "pageOffset": 0, "pageLabels": [],
                 "lastPage": 7, "revision": 9, "created": "2026-01-01T00:00:00.000+00:00",
                 "updated": "2026-01-02T00:00:00.000+00:00", "archived": False, "note_name": "n.md"}
        paper.update(overrides)
        return paper

    def test_pure_function_is_idempotent(self):
        state = self._library([self._paper()])
        migrated, changed = schema.migrate_state(copy.deepcopy(state))
        self.assertTrue(changed)
        again, changed_again = schema.migrate_state(copy.deepcopy(migrated))
        self.assertFalse(changed_again)
        self.assertEqual(migrated, again)

    def test_existing_ai_values_survive_and_missing_keys_are_filled(self):
        state = self._library([self._paper()],
                              settings={"vault_path": "", "sync_enabled": False, "last_sync": "", "sync_error": "",
                                        "ai": {"provider": "ollama", "model": "qwen2.5:7b"}})
        migrated, _ = schema.migrate_state(copy.deepcopy(state))
        ai = migrated["settings"]["ai"]
        self.assertEqual("ollama", ai["provider"])
        self.assertEqual("qwen2.5:7b", ai["model"])
        self.assertEqual(1, ai["max_concurrent_jobs"])
        self.assertFalse(ai["auto_analyze"])

    def test_auto_analyze_is_not_switched_on_by_migration(self):
        state = self._library([self._paper()], settings={"vault_path": "", "sync_enabled": False,
                                                         "last_sync": "", "sync_error": "",
                                                         "ai": {"auto_analyze": False, "provider": "none"}})
        migrated, _ = schema.migrate_state(copy.deepcopy(state))
        self.assertFalse(migrated["settings"]["ai"]["auto_analyze"])

    def test_newer_schema_is_refused_with_a_clear_message(self):
        with self.assertRaises(schema.SchemaError):
            schema.migrate_state(self._library(version=99))

    def test_validator_rejects_broken_analysis_and_settings(self):
        for bad in ({"status": "whatever"}, {"source_sha256": "zz"}, {"answer_file": "../escape.json"},
                    {"error_message": "x" * 501}, {"unexpected": 1}):
            with self.assertRaises(schema.SchemaError, msg=bad):
                schema.validate_analysis(bad)
        for bad in ({"provider": "gpt"}, {"max_concurrent_jobs": 0}, {"max_concurrent_jobs": True},
                    {"api_key_env": "sk-live-secret-value-1234"}, {"base_url": "file:///etc/passwd"},
                    {"api_key": "sk-live"}):
            with self.assertRaises(schema.SchemaError, msg=bad):
                schema.validate_ai_settings(bad)


class PaperValidationKeepsWorking(unittest.TestCase):
    def test_v2_paper_validates_and_analysis_is_normalised(self):
        paper = {"id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "title": "t", "authors": "", "year": "",
                 "venue": "", "doi": "", "citation": "", "tags": "", "language": "en", "type": "theory",
                 "fields": {k: "" for k in ("question", "motivation", "claim", "data", "variables", "method",
                                            "evidence", "robustness", "boundaries", "assessment", "summary",
                                            "connections", "next")},
                 "steps": [False] * 4, "annotations": [], "pageOffset": 0, "pageLabels": [], "lastPage": 1,
                 "revision": 0, "created": "2026-01-01T00:00:00.000+00:00",
                 "updated": "2026-01-01T00:00:00.000+00:00", "archived": False, "note_name": "n.md",
                 "analysis": {"status": "ready", "source_sha256": "AB" * 32, "extra": 1}}
        with self.assertRaises(schema.SchemaError):
            validate_paper(copy.deepcopy(paper))
        del paper["analysis"]["extra"]
        candidate = copy.deepcopy(paper)
        validate_paper(candidate)                      # normalises in place
        self.assertEqual("ab" * 32, candidate["analysis"]["source_sha256"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
