"""Phase 1: backup and restore across schema versions.

Covers requirement 6 (backups carry analyses and the extraction cache) and
requirement 7 (restoring an old library or an old backup never arms the AI and
never sends a paper anywhere).

    python -m unittest discover -s tests -p "test_backup_restore.py" -v
"""
from __future__ import annotations

import copy
import io
import json
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from baseline_helpers import PROJECT_ROOT, fresh_library_v1

sys.path.insert(0, str(PROJECT_ROOT))
import schema      # noqa: E402
from server import Store  # noqa: E402

ANALYSIS = {
    "schema": schema.ANALYSIS_FILE_SCHEMA,
    "paper_id": "medical-2026-zang",
    "prompt_version": "p1",
    "fields": {"question": {"page": 1, "quote": "分级诊疗", "text": "作者研究控费机制", "not_found": False},
               "robustness": {"page": None, "quote": "", "text": "", "not_found": True}},
}


class BackupContents(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="backup-"))
        fresh_library_v1(self.tmp)
        self.store = Store(self.tmp)
        self.paper_id = self.store.state["papers"][0]["id"]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _backup(self):
        return zipfile.ZipFile(io.BytesIO(self.store.backup()))

    def test_backup_carries_analyses_and_extracted_cache(self):
        self.store.write_analysis_answer(self.paper_id, ANALYSIS)
        self.store.files.write_extracted(self.paper_id, {"schema": schema.EXTRACTED_FILE_SCHEMA,
                                                         "pdf_sha256": "ab" * 32,
                                                         "pages": [{"page": 1, "text": "摘要"}]})
        with self._backup() as archive:
            names = set(archive.namelist())
            self.assertIn(f"analyses/{self.paper_id}.json", names)
            self.assertIn(f"extracted/{self.paper_id}.json", names)
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            self.assertEqual(schema.SCHEMA_VERSION, manifest["schema_version"])
            self.assertEqual(1, manifest["analyses"])
            self.assertEqual(1, manifest["extracted"])
            self.assertEqual(3, manifest["papers"])
            for paper in json.loads(archive.read("library.json").decode("utf-8"))["papers"]:
                self.assertIn(f"papers/{paper['id']}.pdf", names)
                self.assertIn(f"notes/{paper['note_name']}", names)

    def test_backup_never_carries_sync_state_or_secrets(self):
        self.store.state["settings"].update(vault_path="D:/vault", sync_enabled=True,
                                            ai=dict(self.store.state["settings"]["ai"],
                                                    provider="ollama", api_key_env="OLLAMA_API_KEY"))
        with self._backup() as archive:
            text = archive.read("library.json").decode("utf-8")
        settings = json.loads(text)["settings"]
        self.assertFalse(settings["sync_enabled"], "a restored backup must not re-enable vault writes")
        self.assertEqual("", settings["vault_path"])
        self.assertEqual("OLLAMA_API_KEY", settings["ai"]["api_key_env"])
        self.assertNotIn("sk-", text)

    def test_backup_without_answer_files_is_still_complete(self):
        with self._backup() as archive:
            names = set(archive.namelist())
        self.assertEqual([], [n for n in names if n.startswith("analyses/")])
        self.assertIn("manifest.json", names)


class RestoreAcrossVersions(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="restore-"))
        fresh_library_v1(self.tmp)
        self.source = Store(self.tmp)
        self.paper_id = self.source.state["papers"][0]["id"]
        self.source.write_analysis_answer(self.paper_id, ANALYSIS)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _empty_store(self):
        directory = Path(tempfile.mkdtemp(prefix="restore-target-"))
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        return Store(directory)

    def test_restore_round_trip_keeps_manual_records_and_answers(self):
        blob = self.source.backup()
        target = self._empty_store()
        self.assertEqual(3, target.restore(blob))
        restored = next(p for p in target.state["papers"] if p["id"] == self.paper_id)
        original = next(p for p in self.source.state["papers"] if p["id"] == self.paper_id)
        self.assertEqual(original["fields"], restored["fields"])
        self.assertEqual(original["steps"], restored["steps"])
        self.assertEqual(original["annotations"], restored["annotations"])
        self.assertEqual(original["lastPage"], restored["lastPage"])
        payload, error = target.analysis_answer(self.paper_id)
        self.assertIsNone(error)
        # The answer text is kept: recovery must not discard what the user already had.
        self.assertIn(ANALYSIS["fields"]["question"]["text"],
                      payload["answers"]["question"]["answer"])
        # ... but a record made under another prompt version is never labelled verified.
        self.assertEqual("imported", payload["source"])
        self.assertFalse(payload.get("verified", True))
        self.assertEqual([], [item for item in payload["answers"]["question"]["evidence"]
                              if item.get("verified")])
        self.assertEqual("ready", restored["analysis"]["status"])
        self.assertEqual(f"{self.paper_id}.json", restored["analysis"]["answer_file"])
        self.assertIn("未按当前规则重新核对", restored["analysis"]["message"])

    def test_version_1_backup_without_new_members_still_restores(self):
        """An old ZIP: version 1 library, papers and notes only - no analyses/extracted/manifest."""
        old = io.BytesIO()
        state = copy.deepcopy(self.source.state)
        state["version"] = 1
        for paper in state["papers"]:
            paper.pop("analysis", None)
            paper["fields"]["question"] = "旧备份里的研究问题"
        state["settings"].pop("ai", None)
        with zipfile.ZipFile(old, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("library.json", json.dumps(state, ensure_ascii=False, indent=2))
            for paper in state["papers"]:
                archive.write(self.tmp / "papers" / f"{paper['id']}.pdf", f"papers/{paper['id']}.pdf")
                archive.writestr(f"notes/{paper['note_name']}", "# old note\n")

        target = self._empty_store()
        self.assertEqual(3, target.restore(old.getvalue()))
        restored = next(p for p in target.state["papers"] if p["id"] == self.paper_id)
        self.assertEqual("旧备份里的研究问题", restored["fields"]["question"])
        self.assertEqual(schema.SCHEMA_VERSION, target.state["version"])
        self.assertEqual(set(schema.ANALYSIS_DEFAULTS), set(restored["analysis"]))
        self.assertEqual("none", restored["analysis"]["status"])
        self.assertEqual("", restored["analysis"]["answer_file"])
        self.assertFalse(target.state["settings"]["ai"]["auto_analyze"])
        self.assertEqual("none", target.state["settings"]["ai"]["provider"])

    def test_restore_never_arms_the_ai_or_sync(self):
        blob = self.source.backup()
        target = self._empty_store()
        target.restore(blob)
        settings = target.state["settings"]
        self.assertEqual("none", settings["ai"]["provider"])
        self.assertFalse(settings["ai"]["auto_analyze"])
        self.assertFalse(settings["sync_enabled"])
        self.assertEqual("", settings["vault_path"])
        # nothing is queued: every restored paper keeps status "none" even when an
        # answer file came along, and no provider module is imported by the server
        self.assertEqual([], [p["id"] for p in target.state["papers"] if p["analysis"]["status"] == "pending"])
        self.assertNotIn("ai_provider", sys.modules, "restore must not pull in any provider code")

    def test_restoring_into_a_library_that_already_has_the_paper_keeps_the_local_copy(self):
        blob = self.source.backup()
        self.source.state["papers"][0]["fields"]["question"] = "本地已写的内容"
        self.assertEqual(0, self.source.restore(blob))
        self.assertEqual("本地已写的内容", self.source.state["papers"][0]["fields"]["question"])

    def test_malicious_archive_members_are_ignored(self):
        """A crafted ZIP cannot make the restore write outside data/analyses."""
        blob = self.source.backup()
        rebuilt = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(blob)) as source, zipfile.ZipFile(rebuilt, "w") as target:
            for item in source.infolist():
                target.writestr(item, source.read(item.filename))
            target.writestr("analyses/../../../escape.json", '{"answer": "evil"}')
            target.writestr("analyses/not-a-valid-id.json", '{"answer": "evil"}')
        target_store = self._empty_store()
        target_store.restore(rebuilt.getvalue())
        self.assertFalse((self.tmp / "escape.json").is_file())
        self.assertFalse((target_store.directory / "escape.json").is_file())
        self.assertFalse(target_store.files.has_analysis("not-a-valid-id"))
        for paper in target_store.state["papers"]:
            self.assertTrue(target_store.files.has_pdf(paper["id"]))

    def test_corrupt_answer_in_backup_does_not_break_restore(self):
        blob = self.source.backup()
        rebuilt = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(blob)) as source, zipfile.ZipFile(rebuilt, "w") as target:
            for item in source.infolist():
                if item.filename == f"analyses/{self.paper_id}.json":
                    target.writestr(item, '{"fields": {"question": ')   # truncated JSON
                else:
                    target.writestr(item, source.read(item.filename))
        target_store = self._empty_store()
        self.assertEqual(3, target_store.restore(rebuilt.getvalue()))
        restored = next(p for p in target_store.state["papers"] if p["id"] == self.paper_id)
        self.assertEqual("", restored["analysis"]["answer_file"])
        self.assertFalse(target_store.files.has_analysis(self.paper_id))
        self.assertTrue((target_store.files.pdf_path(self.paper_id)).is_file())


class ReadingCardStillOpens(unittest.TestCase):
    """Requirement: after restoring an old backup the reading card still works."""

    def test_card_fields_and_note_export_after_old_backup_restore(self):
        tmp = Path(tempfile.mkdtemp(prefix="card-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        fresh_library_v1(tmp)
        source = Store(tmp)

        old = io.BytesIO()
        state = copy.deepcopy(source.state)
        state["version"] = 1
        state["papers"] = state["papers"][:1]
        for paper in state["papers"]:
            paper.pop("analysis", None)
            paper["fields"].update(question="旧备份问题", evidence="旧备份证据")
            paper["steps"] = [True, False, False, False]
            paper["annotations"] = [{"id": "a1", "type": "argument", "page": 1, "quote": "引文",
                                     "note": "我的理解", "rects": [], "created": "2026-01-01T00:00:00.000+00:00"}]
            paper["lastPage"] = 3
        state["settings"].pop("ai", None)
        with zipfile.ZipFile(old, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("library.json", json.dumps(state, ensure_ascii=False, indent=2))
            for paper in state["papers"]:
                archive.write(tmp / "papers" / f"{paper['id']}.pdf", f"papers/{paper['id']}.pdf")
                archive.writestr(f"notes/{paper['note_name']}", "旧笔记")

        target_dir = Path(tempfile.mkdtemp(prefix="card-target-"))
        self.addCleanup(shutil.rmtree, target_dir, ignore_errors=True)
        target = Store(target_dir)
        self.assertEqual(1, target.restore(old.getvalue()))
        paper = target.state["papers"][0]
        self.assertEqual("旧备份问题", paper["fields"]["question"])
        self.assertEqual("旧备份证据", paper["fields"]["evidence"])
        self.assertEqual([True, False, False, False], paper["steps"])
        self.assertEqual(3, paper["lastPage"])
        self.assertEqual(1, len(paper["annotations"]))
        self.assertEqual("我的理解", paper["annotations"][0]["note"])
        self.assertTrue(target.files.has_pdf(paper["id"]))
        # /api/export re-renders the note from the same record the card uses
        from server import markdown
        note = markdown(copy.deepcopy(paper), "[[x.pdf|论文原文]]")
        self.assertIn("旧备份问题", note)
        self.assertIn("我的理解", note)
        self.assertIn("PDF 1", note)


if __name__ == "__main__":
    unittest.main(verbosity=2)
