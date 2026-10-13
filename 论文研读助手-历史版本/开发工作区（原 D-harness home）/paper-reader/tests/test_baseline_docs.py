"""Phase 0 acceptance tests: the code facts this project is built on.

Pure reads. Nothing here writes to the library, the PDFs or the source tree.
Run from the project root:

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import sys
import unittest

from baseline_helpers import DATA_DIR, FIXTURE_LIBRARY_V1, PROJECT_ROOT, locate, read_source


class ProjectLayout(unittest.TestCase):
    """Step 1 of phase 0: every file the plan depends on is where it is claimed."""

    REQUIRED = [
        "server.py", "launch.py", "static/app.js", "static/index.html",
        "static/styles.css", "static/readability.css", "static/guides.js",
        "vendor/pdfjs/pdf.min.mjs", "vendor/pdfjs/pdf.worker.min.mjs",
        "vendor/pdfjs/pdf_viewer.css", "data/library.json", "使用说明.md",
    ]

    def test_required_files_exist(self):
        missing = [name for name in self.REQUIRED if not (PROJECT_ROOT / name).is_file()]
        self.assertEqual([], missing)

    def test_pdfjs_assets_and_license(self):
        for name in ("cmaps", "standard_fonts", "wasm", "iccs"):
            self.assertTrue((PROJECT_ROOT / "vendor" / "pdfjs" / name).is_dir(), name)
        self.assertTrue((PROJECT_ROOT / "vendor" / "pdfjs" / "LICENSE").is_file())

    def test_every_paper_has_a_pdf(self):
        """Uses the frozen fixture, not the live library, so migrations cannot break it."""
        state = json.loads(FIXTURE_LIBRARY_V1.read_text(encoding="utf-8"))
        self.assertEqual(1, state["version"])
        for paper in state["papers"]:
            pdf = DATA_DIR / "papers" / f"{paper['id']}.pdf"
            self.assertTrue(pdf.is_file(), pdf)
            self.assertIn(b"%PDF-", pdf.read_bytes()[:1024])


class ImplementationLocations(unittest.TestCase):
    """Step 2: where each module the plan touches actually lives."""

    def test_store_and_endpoints(self):
        for needle in (
            "class Store", "def snapshot(self)", "def save_paper(self, body)", "def import_pdf(self",
            "def sync_paper(self", "def sync_index(self", "def sync_all(self", "def backup(self)",
            "def restore(self, data)",
            "def handler_for(store, token, manager=None, sessions=None, limits=None)",
        ):
            self.assertTrue(locate(needle, "server.py"), needle)
        for endpoint in ('"/api/import"', '"/api/save"', '"/api/backup"', '"/api/restore"',
                         '"/api/state"', '"/api/pdf/"', '"/api/export/"', '"/api/settings"',
                         '"/api/sync"', '"/api/shutdown"'):
            self.assertTrue(locate(endpoint, "server.py"), endpoint)
        # the session endpoints the page uses instead of a page token
        for endpoint in ('"/api/session"', '"/api/session/bootstrap"', '"/api/beacon/"'):
            self.assertTrue(locate(endpoint, "server.py"), endpoint)

    def test_frontend_functions(self):
        for name in ("function flush(", "async function selectPaper(", "async function renderPage(",
                     "async function goPage(", "function renderCard(", "function renderSteps(",
                     "function renderAnnotations(", "async function loadPdf("):
            self.assertTrue(locate(name, "static/app.js"), name)

    def test_fit_page_implementation(self):
        """Phase 7: the default scale fits the whole page into the visible area.

        Stage 0 fitted width only (app.js:145); now the scale is
        min(availableWidth / pageWidth, availableHeight / pageHeight) * userZoom.
        """
        source = read_source("static/app.js")
        line_no, text_line = locate("const scale=fitScale(initial)", "static/app.js")
        self.assertIn("fitScale", text_line)
        self.assertGreater(line_no, 200)
        self.assertIn("zoomMode = 'page'", source, "whole-page fit is still the starting mode")
        self.assertIn("return Math.min(availableWidth/viewport.width,availableHeight/viewport.height);",
                      source, "fit-page must consider both dimensions")
        self.assertIn("return fitted*zoom;", source,
                      "the user zoom multiplies the fitted scale")
        self.assertIn("box.classList.toggle('page-fits'", source,
                      "a fully visible page must not scroll inside itself")
        # review fix B1: below the legibility threshold the reader switches to fit-width
        self.assertIn("MIN_READABLE_SCALE", source)
        self.assertIn("mirrorZoomSelect('width')", source)

    def test_render_card_field_source(self):
        """Acceptance item: renderCard renders manual fields only (app.js:123-124)."""
        line_no, _ = locate("function renderCard(", "static/app.js")
        lines = read_source("static/app.js").splitlines()
        body = "\n".join(lines[line_no - 1:line_no + 6])
        self.assertIn("Object.entries(FIELDS)", body)
        self.assertIn("p.fields[k]", body)
        self.assertIn("definition(p,k)", body)
        # The manual fields are still the primary content of every card entry; the AI
        # reference (phase 6) is an extra element next to them, never a replacement.
        # one manual paragraph and one AI switch per field, inside the same section
        self.assertEqual(1, body.count("${esc(p.fields[k]||"))
        self.assertEqual(1, body.count('data-ai-field="${k}"'))
        self.assertIn('<section class="card-field">', body)
        # Anchored on code, not on a line number: phases 4-6 legitimately edited app.js
        # (the stage 0 baseline recorded line 123).
        self.assertGreater(line_no, 100)

    def test_guides_expose_13_fields(self):
        text = read_source("static/guides.js")
        for key in ("question", "motivation", "claim", "data", "variables", "method", "evidence",
                    "robustness", "boundaries", "assessment", "summary", "connections", "next"):
            self.assertIn(f"{key}:", text)


class DataContract(unittest.TestCase):
    """The contract the AI work must extend without breaking."""

    def test_frozen_v1_fixture_shape(self):
        state = json.loads(FIXTURE_LIBRARY_V1.read_text(encoding="utf-8"))
        self.assertEqual({"version", "settings", "papers"}, set(state))
        self.assertEqual(1, state["version"], "the frozen fixture stays at version 1")
        self.assertEqual({"vault_path", "sync_enabled", "last_sync", "sync_error"}, set(state["settings"]))
        for paper in state["papers"]:
            self.assertEqual(13, len(paper["fields"]))
            self.assertEqual(4, len(paper["steps"]))
            self.assertIn("revision", paper)
            self.assertIsInstance(paper["annotations"], list)

    def test_live_library_is_a_current_schema_library(self):
        """The live data/ may be migrated; it must still satisfy the version 2 contract."""
        sys.path.insert(0, str(PROJECT_ROOT))
        import schema
        state = json.loads((DATA_DIR / "library.json").read_text(encoding="utf-8"))
        self.assertEqual(schema.SCHEMA_VERSION, state["version"])
        # A key added to settings.ai is filled in by the validator on load, so a library
        # written by an earlier build is still valid: it may be missing new keys, never
        # carrying unknown ones, and validating it must produce exactly the current shape.
        stored_ai = state["settings"]["ai"]
        self.assertEqual(set(), set(stored_ai) - set(schema.AI_SETTINGS_DEFAULTS),
                         "the live library carries an unknown settings.ai key")
        self.assertEqual(set(schema.AI_SETTINGS_DEFAULTS),
                         set(schema.validate_ai_settings(stored_ai)))
        for paper in state["papers"]:
            self.assertEqual(set(schema.ANALYSIS_DEFAULTS), set(paper["analysis"]))
            self.assertIn(paper["analysis"]["status"], schema.ANALYSIS_STATUSES)
            self.assertEqual(13, len(paper["fields"]))

    def test_live_library_has_no_ai_run_state(self):
        """Schema only: no answer file, no queued job, no auto analysis."""
        state = json.loads((DATA_DIR / "library.json").read_text(encoding="utf-8"))
        self.assertFalse(state["settings"]["ai"]["auto_analyze"])
        self.assertEqual("none", state["settings"]["ai"]["provider"])
        for paper in state["papers"]:
            self.assertIn(paper["analysis"]["status"],
                          ("none", "queued", "waiting_for_provider"))
            self.assertEqual("", paper["analysis"]["answer_file"])
            self.assertFalse([key for key in paper["fields"] if key.startswith("ai_")])

    def test_validator_whitelists_only_manual_fields(self):
        text = read_source("server.py")
        self.assertIn('FIELDS = {', text)
        for key in ("question", "motivation", "claim", "data", "variables", "method", "evidence",
                    "robustness", "boundaries", "assessment", "summary", "connections", "next"):
            self.assertIn(f'"{key}"', text)

    def test_analysis_metadata_is_not_writable_from_the_browser(self):
        """AI metadata must stay out of the /api/save whitelist."""
        text = read_source("server.py")
        self.assertIn("EDITABLE_KEYS = (", text)
        whitelist = text.split("EDITABLE_KEYS = (", 1)[1].split(")", 1)[0]
        self.assertNotIn("analysis", whitelist)


if __name__ == "__main__":
    unittest.main(verbosity=2)
