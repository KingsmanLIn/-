"""Journal cover pages must not become the title.

The reported case: an English article whose page 1 is a publisher cover (masthead, "Contents
lists available at ScienceDirect", volume/issue/page line) had the journal name recorded as the
paper title. These fixtures use the real layout of Elsevier / Wiley covers.

Run:  D:\\harness home\\pr-lab\\runtime\\python.exe D:\\harness home\\pr-lab\\tests\\test_metadata_cover.py
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import metadata  # noqa: E402

ELSEVIER = """Journal of Monetary Economics 152 (2024) 103–126

Contents lists available at ScienceDirect

Journal of Monetary Economics

journal homepage: www.elsevier.com/locate/jme

The Great Accretion and the Natural Rate of Interest

Jane Q. Author a, John B. Smith b

a Department of Economics, University of Somewhere, Somewhere, USA
b Research Department, Some Central Bank, Capital City, Country

ARTICLE INFO

ABSTRACT

Article history:
Received 12 January 2023
Accepted 4 March 2024
Available online 15 March 2024

This paper studies the long-run decline in the natural rate of interest and
attributes part of it to the accumulation of safe assets.

JEL classification: E43, E52

Keywords: Natural rate, safe assets, monetary policy
"""

WILEY = """The Journal of Finance

VOL. LXXIX, NO. 3    JUNE 2024

THE CROSS-SECTION OF STOCK RETURNS REVISITED

By MARIA L. GARCIA and PETER CHEN*

ABSTRACT

We revisit the cross-section of expected stock returns using a new data set.

DOI: 10.1111/jofi.13333
"""

AER_RUNNING_HEAD = """American Economic Review 2019, 109(7): 2345–2380
https://doi.org/10.1257/aer.20170973

Capital Accumulation, Private Property, and Rising Inequality in China, 1978–2015

By Thomas Piketty, Li Yang, and Gabriel Zucman*

This paper combines national accounts, survey, wealth and fiscal data to estimate the evolution
of private wealth in China.
"""


def page(text, number=1):
    return [{"pdf_page": number, "text": text, "char_count": len(text)}]


class CoverPageTitle(unittest.TestCase):
    def parse(self, text, filename=""):
        result = metadata.parse_metadata(page(text), filename)
        print(f"\n  title  = {result['title']!r}\n  authors= {result['authors']!r}"
              f"\n  year   = {result['year']!r}\n  venue  = {result['venue']!r}"
              f"\n  doi    = {result['doi']!r}")
        return result

    def test_elsevier_cover_keeps_the_article_title(self):
        result = self.parse(ELSEVIER, "the-great-accretion.pdf")
        self.assertEqual("The Great Accretion and the Natural Rate of Interest", result["title"])
        self.assertNotIn("Journal of Monetary Economics", result["title"])
        self.assertIn("Author", result["authors"])
        self.assertEqual("2024", result["year"])
        self.assertIn("Journal of Monetary Economics", result["venue"])

    def test_wiley_cover_keeps_the_article_title(self):
        result = self.parse(WILEY, "cross-section-of-stock-returns.pdf")
        # The printed title is all caps; the parser re-cases it, so compare case-insensitively.
        self.assertEqual("the cross-section of stock returns revisited", result["title"].lower())
        self.assertNotIn("Journal of Finance", result["title"])
        self.assertEqual("2024", result["year"])
        self.assertIn("Journal of Finance", result["venue"])
        self.assertTrue(result["doi"].startswith("10.1111/"), result["doi"])
        self.assertEqual("Maria L. Garcia and Peter Chen", result["authors"])

    def test_aer_first_page_still_works(self):
        """Regression: the layout that already parsed correctly must not change."""
        result = self.parse(AER_RUNNING_HEAD, "piketty.pdf")
        self.assertEqual("Capital Accumulation, Private Property, and Rising Inequality in China, 1978–2015",
                         result["title"])
        self.assertIn("Piketty", result["authors"])
        self.assertEqual("2019", result["year"])
        self.assertIn("American Economic Review", result["venue"])

    def test_real_cached_papers_are_unaffected(self):
        """The four caches shipped with the release must keep their known-good results."""
        expected = {
            "93cf2c85-bdc6-484b-9b4d-93029bafe5de.json": (
                "Capital Accumulation, Private Property, and Rising Inequality in China, 1978–2015",
                "Thomas Piketty, Li Yang, and Gabriel Zucman", "2019", "American Economic Review"),
            "9edf1d4b-561b-47a2-860e-9119d865f54b.json": (
                "Global Financial Cycles and Risk Premiums", None, "2018",
                "NBER Working Paper No. 24677"),
        }
        for name, (title, authors, year, venue) in expected.items():
            cache = ROOT / "data" / "extracted" / name
            if not cache.is_file():
                continue
            with self.subTest(cache=name):
                result = metadata.parse_metadata_file(cache)
                self.assertEqual(title, result["title"])
                if authors:
                    self.assertEqual(authors, result["authors"])
                self.assertEqual(year, result["year"])
                self.assertEqual(venue, result["venue"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
