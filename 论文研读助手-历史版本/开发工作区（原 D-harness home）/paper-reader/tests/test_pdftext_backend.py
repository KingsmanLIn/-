"""The isolated extraction child must still find pdftotext (regression test).

Before the fix the child's trimmed PATH hid a user-installed pdftotext, so every paper was
extracted with the builtin extractor: fewer characters and mangled diacritics. The parent now
resolves the command and passes the absolute path through an environment variable.

Run:  D:\\harness home\\pr-lab\\runtime\\python.exe D:\\harness home\\pr-lab\\tests\\test_pdftext_backend.py
"""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import extraction      # noqa: E402
import pdf_worker      # noqa: E402
import pdftext         # noqa: E402
import server          # noqa: E402


class EnvironmentHandover(unittest.TestCase):
    def test_the_parent_passes_the_resolved_command(self):
        fake = r"D:\tools\poppler\pdftotext.exe"
        with mock.patch.object(pdf_worker.shutil, "which", lambda name: fake if name == "pdftotext" else None):
            environment = pdf_worker.scrub_environment()
        self.assertEqual(fake, environment.get(pdftext.PDFTOTEXT_ENV))

    def test_no_command_means_no_variable(self):
        with mock.patch.object(pdf_worker.shutil, "which", lambda name: None):
            environment = pdf_worker.scrub_environment()
        self.assertNotIn(pdftext.PDFTOTEXT_ENV, environment)

    def test_the_child_side_prefers_the_handed_over_path(self):
        handed = r"D:\tools\poppler\pdftotext.exe"
        with mock.patch.dict(os.environ, {pdftext.PDFTOTEXT_ENV: handed}):
            with mock.patch.object(pdftext.shutil, "which", lambda name: None):
                backend = pdftext.PdftotextBackend()
        self.assertEqual(handed, backend.executable)
        self.assertTrue(backend.available())

    def test_an_explicit_argument_still_wins(self):
        with mock.patch.dict(os.environ, {pdftext.PDFTOTEXT_ENV: r"D:\other\pdftotext.exe"}):
            backend = pdftext.PdftotextBackend(executable=r"D:\explicit\pdftotext.exe")
        self.assertEqual(r"D:\explicit\pdftotext.exe", backend.executable)


class RealExtraction(unittest.TestCase):
    """End to end through the isolated child, on a real 58-page PDF when one is present."""

    def test_the_isolated_child_uses_the_better_backend(self):
        pdf = sorted((ROOT / "data" / "papers").glob("*.pdf"))
        if not pdf:
            self.skipTest("no PDF in this copy")
        if not shutil.which("pdftotext"):
            self.skipTest("pdftotext is not installed on this machine")
        directory = tempfile.mkdtemp(prefix="pr-pdftext-")
        store = server.Store(directory)
        extractor = extraction.IsolatedPdfTextExtractor(files=store.files)
        result = extractor.extract(pdf[0], paper_id="pdftext-backend-check")
        print("backend:", result.backend, "pages:", result.page_count,
              "chars:", sum(page.char_count for page in result.pages))
        self.assertTrue(result.ok, result.message)
        self.assertEqual("pdftotext", result.backend,
                         "the isolated child must be able to use pdftotext")
        self.assertGreater(result.page_count, 0)
        self.assertGreater(sum(page.char_count for page in result.pages), 100000)

    def test_a_cache_from_the_builtin_backend_is_not_reused(self):
        """The cache key includes the backend version, so better text replaces old text."""
        pdf = sorted((ROOT / "data" / "papers").glob("*.pdf"))
        if not pdf or not shutil.which("pdftotext"):
            self.skipTest("needs a PDF and pdftotext")
        directory = tempfile.mkdtemp(prefix="pr-pdftext-")
        store = server.Store(directory)
        extractor = extraction.PdfTextExtractor(files=store.files)
        first = extractor.extract(pdf[0], paper_id="backend-cache-check")
        self.assertTrue(first.ok, first.message)
        # A cache written under the builtin backend version must be rejected once the preferred
        # backend is pdftotext; the payload below imitates the old entry.
        payload, error = store.files.read_extracted("backend-cache-check")
        self.assertIsNone(error, error)
        payload["extractor"] = dict(payload.get("extractor") or {},
                                    version="builtin-0.0.0", backend="builtin")
        store.files.write_extracted("backend-cache-check", payload)
        again = extractor.extract(pdf[0], paper_id="backend-cache-check")
        self.assertEqual("miss", extractor.last_diagnostics.get("cache"),
                         "an older backend's cache must not be served")
        self.assertEqual("pdftotext", again.backend)


if __name__ == "__main__":
    unittest.main(verbosity=2)
