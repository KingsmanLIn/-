"""Tests for metadata.py (bibliographic metadata from extracted PDF text).

Run either way - no package, no pip, stdlib only:

    D:\\harness home\\pr-lab\\runtime\\python.exe tests\\test_metadata.py
    D:\\harness home\\pr-lab\\runtime\\python.exe -m unittest discover -s tests

The tests touch ``data/extracted/*.json`` read-only; anything they write goes to a
temporary directory.
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import metadata                                                    # noqa: E402
from metadata import RESULT_KEYS, parse_metadata, parse_metadata_file  # noqa: E402

CACHE_DIR = ROOT / "data" / "extracted"


def page(text, number=1):
    return {"pdf_page": number, "text": text, "char_count": len(text),
            "width": 612, "height": 792, "has_text": bool(text.strip()),
            "has_images": False, "warnings": []}


def pages(*texts):
    return [page(text, index + 1) for index, text in enumerate(texts)]


def find_cache(*markers):
    """Locate a real cache by content, so a renamed paper id cannot break the test."""
    if not CACHE_DIR.is_dir():
        return None
    for path in sorted(CACHE_DIR.glob("*.json")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if all(marker in text for marker in markers):
            return path
    return None


NBER = find_cache("NBER WORKING PAPER SERIES", "GLOBAL FINANCIAL CYCLES")
PIKETTY = find_cache("10.1257/aer.20170973", "Capital Accumulation")
HAHN = find_cache("10.1257/aer.20250166", "Welfare Analysis")
CHINESE = find_cache("学术财经", "学术笔记")


class TestContract(unittest.TestCase):
    def test_returns_exactly_six_keys(self):
        for source in ([], {}, pages(""), "no such file.json", None):
            result = parse_metadata(source)
            self.assertEqual(set(result), set(RESULT_KEYS))
            self.assertTrue(all(isinstance(value, str) for value in result.values()))

    def test_empty_input_is_all_empty(self):
        self.assertEqual(parse_metadata([]), {key: "" for key in RESULT_KEYS})
        self.assertEqual(parse_metadata(None), {key: "" for key in RESULT_KEYS})

    def test_accepts_dict_pages_list_path_and_json_text(self):
        text = "\n".join([
            "Journal of Testing 2020, 5(1): 1-9",
            "https://doi.org/10.1234/jt.2020.0001",
            "A Study of Everything",
            "By Ada Lovelace and Alan Turing"])
        expected = parse_metadata(pages(text))
        self.assertEqual(expected["authors"], "Ada Lovelace and Alan Turing")
        self.assertEqual(parse_metadata({"pages": pages(text)}), expected)
        self.assertEqual(parse_metadata(json.dumps({"pages": pages(text)})), expected)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "cache.json"
            path.write_text(json.dumps({"pages": pages(text)}), encoding="utf-8")
            self.assertEqual(parse_metadata(str(path)), expected)
            self.assertEqual(parse_metadata(path), expected)
            self.assertEqual(parse_metadata_file(path), expected)

    def test_parse_metadata_file_on_broken_cache_is_safe(self):
        report = {}
        with tempfile.TemporaryDirectory() as folder:
            missing = Path(folder) / "missing.json"
            self.assertEqual(parse_metadata_file(missing, report=report),
                             {key: "" for key in RESULT_KEYS})
            self.assertTrue(report["warnings"])
            broken = Path(folder) / "broken.json"
            broken.write_text("{not json", encoding="utf-8")
            self.assertEqual(parse_metadata_file(broken),
                             {key: "" for key in RESULT_KEYS})
            with self.assertRaises(ValueError):
                parse_metadata_file(broken, strict=True)

    def test_caps_are_enforced(self):
        long_title = " ".join(["Titleword"] * 200)
        long_author = " ".join(["Jane"] * 200)
        report = {}
        result = parse_metadata(pages("\n".join([
            "Journal of Testing 2020, 5(1): 1-9",
            long_title,
            "By " + long_author])), report=report)
        self.assertLessEqual(len(result["title"]), metadata.CAPS["title"])
        self.assertLessEqual(len(result["authors"]), metadata.CAPS["authors"])
        self.assertLessEqual(len(result["venue"]), metadata.CAPS["venue"])
        self.assertLessEqual(len(result["doi"]), metadata.CAPS["doi"])

    def test_year_is_four_digits_or_empty(self):
        for source in ([], pages("nonsense"), pages("Copyright 12345 by x")):
            year = parse_metadata(source)["year"]
            self.assertTrue(year == "" or (len(year) == 4 and year.isdigit()))

    def test_module_is_stdlib_only_and_never_uses_the_network(self):
        source = (Path(metadata.__file__)).read_text(encoding="utf-8")
        for banned in ("import requests", "urlopen", "import socket", "http.client",
                       "urllib.request", "api.deepseek"):
            self.assertNotIn(banned, source)
        roots = set()
        for line in source.splitlines():
            line = line.strip()
            if line.startswith("from ") and " import " in line:
                roots.add(line[5:].split(" import ")[0].split(".")[0])
            elif line.startswith("import "):
                roots.update(part.strip().split(".")[0].split(" ")[0]
                             for part in line[7:].split(","))
        self.assertTrue(roots.issubset(set(sys.stdlib_module_names) | {"metadata"}),
                        f"non-stdlib imports: {roots - set(sys.stdlib_module_names)}")


class TestRealCaches(unittest.TestCase):
    """Behaviour on the four real extractions (skipped when a cache is absent)."""

    def test_nber_working_paper(self):
        if NBER is None:
            self.skipTest("NBER cache not present")
        report = {}
        result = parse_metadata_file(NBER, report=report)
        self.assertEqual(result["year"], "2018")
        self.assertEqual(result["venue"], "NBER Working Paper No. 24677")
        self.assertEqual(result["doi"], "")            # NBER working papers carry none
        self.assertEqual(result["title"], "Global Financial Cycles and Risk Premiums")
        for surname in ("Jord", "Schularick", "Taylor", "Ward"):
            self.assertIn(surname, result["authors"])
        # the extractor dropped the accents of "Oscar Jorda"; the module keeps the
        # damaged token verbatim instead of inventing a name, and says so
        self.assertIn("scar", result["authors"])
        self.assertEqual(report["fields"]["authors"]["confidence"], "medium")
        self.assertIn("scar", report["fields"]["authors"]["suspicious"])
        self.assertIn("24677", result["citation"])

    def test_aer_article_with_doi(self):
        if PIKETTY is None:
            self.skipTest("Piketty cache not present")
        result = parse_metadata_file(PIKETTY)
        self.assertEqual(result["doi"], "10.1257/aer.20170973")
        self.assertEqual(result["year"], "2019")
        self.assertEqual(result["venue"], "American Economic Review")
        self.assertTrue(result["title"].startswith("Capital Accumulation"))
        self.assertIn("1978", result["title"])         # en dash preserved in the title
        self.assertEqual(result["authors"], "Thomas Piketty, Li Yang, and Gabriel Zucman")
        self.assertIn("10.1257/aer.20170973", result["citation"])

    def test_aer_article_with_corrupted_author_token(self):
        if HAHN is None:
            self.skipTest("Hahn cache not present")
        result = parse_metadata_file(HAHN)
        self.assertEqual(result["doi"], "10.1257/aer.20250166")
        self.assertEqual(result["year"], "2026")
        self.assertEqual(result["title"], "A Welfare Analysis of Policies Impacting "
                                           "Climate Change")
        # four authors survive, including the hyphenated one and the damaged "Befln"
        self.assertIn("Robert W. Hahn", result["authors"])
        self.assertIn("Sprung-Keyser", result["authors"])
        self.assertIn("Befln", result["authors"])      # never silently repaired

    def test_chinese_notes_collection(self):
        if CHINESE is None:
            self.skipTest("Chinese cache not present")
        report = {}
        result = parse_metadata_file(CHINESE, report=report)
        self.assertEqual(result["title"], "学术财经˙学术笔记全汇总")
        self.assertEqual(result["year"], "2023")
        self.assertEqual(result["authors"], "")
        self.assertEqual(result["venue"], "")
        self.assertEqual(result["doi"], "")
        self.assertIn("没有找到可信的署名信息", " ".join(report["warnings"]))

    def test_all_real_caches_are_fast_and_consistent(self):
        paths = [path for path in (NBER, PIKETTY, HAHN, CHINESE) if path]
        if not paths:
            self.skipTest("no caches present")
        for path in paths:
            start = time.perf_counter()
            for _ in range(5):
                first = parse_metadata_file(path)
            elapsed = (time.perf_counter() - start) / 5
            self.assertLess(elapsed, 0.050, f"{path.name}: {elapsed * 1000:.1f} ms")
            self.assertEqual(first, parse_metadata_file(path))   # deterministic


class TestSynthetic(unittest.TestCase):
    def test_clean_journal_article(self):
        report = {}
        result = parse_metadata(pages("\n".join([
            "Journal of Environmental Economics and Management 2021, 108(2): 45-67",
            "https://doi.org/10.1016/j.jeem.2021.102468",
            "Carbon Taxation and Household Behaviour:",
            "Evidence from a Natural Experiment",
            "By Jane Q. Doe, Wei Zhang, and Andrei Popescu",
            "Abstract",
            "We estimate how households respond to a carbon tax using administrative",
            "data covering 1.2 million households between 2015 and 2019."])),
            filename="doe-et-al-2021-carbon-taxation.pdf", report=report)
        self.assertEqual(result["title"], "Carbon Taxation and Household Behaviour: "
                                          "Evidence from a Natural Experiment")
        self.assertEqual(result["authors"], "Jane Q. Doe, Wei Zhang, and Andrei Popescu")
        self.assertEqual(result["year"], "2021")
        self.assertEqual(result["venue"], "Journal of Environmental Economics and Management")
        self.assertEqual(result["doi"], "10.1016/j.jeem.2021.102468")
        self.assertEqual(result["citation"],
                         "Jane Q. Doe, Wei Zhang, and Andrei Popescu (2021). Carbon Taxation "
                         "and Household Behaviour: Evidence from a Natural Experiment. "
                         "Journal of Environmental Economics and Management. "
                         "https://doi.org/10.1016/j.jeem.2021.102468")
        self.assertEqual(report["extras"]["volume"], "108")
        self.assertEqual(report["extras"]["issue"], "2")

    def test_arxiv_preprint(self):
        result = parse_metadata(pages("\n".join([
            "arXiv:2401.01234v2  [cs.CL]  3 Jan 2024",
            "Sparse Attention Is All You Need for Long Documents",
            "Maria Lopez, Chen Wei, and Rahul Kumar",
            "Abstract",
            "We propose a sparse attention mechanism that scales to 128k tokens."])))
        self.assertEqual(result["title"],
                         "Sparse Attention Is All You Need for Long Documents")
        self.assertEqual(result["venue"], "arXiv preprint arXiv:2401.01234")
        self.assertEqual(result["year"], "2024")       # from the arXiv id
        self.assertEqual(result["doi"], "")            # an arXiv id is not a DOI

    def test_nber_cover_page(self):
        result = parse_metadata(pages("\n".join([
            "NBER WORKING PAPER SERIES",
            "MONETARY POLICY AND THE GLOBAL FINANCIAL CYCLE",
            "scar Jord",
            "Moritz Schularick",
            "Alan M. Taylor",
            "Felix Ward",
            "Working Paper 24677",
            "http://www.nber.org/papers/w24677",
            "NATIONAL BUREAU OF ECONOMIC RESEARCH",
            "1050 Massachusetts Avenue",
            "Cambridge, MA 02138",
            "June 2018",
            "© 2018 by scar Jord, Moritz Schularick, and Alan M. Taylor."])))
        self.assertEqual(result["title"], "Monetary Policy and the Global Financial Cycle")
        self.assertEqual(result["authors"],
                         "scar Jord, Moritz Schularick, Alan M. Taylor, and Felix Ward")
        self.assertEqual(result["year"], "2018")
        self.assertEqual(result["venue"], "NBER Working Paper No. 24677")
        self.assertEqual(result["doi"], "")

    def test_two_column_body_with_footnote_block(self):
        result = parse_metadata(pages("\n".join([
            "Review of Economic Studies 2022, 89(4): 1801-1840",
            "https://doi.org/10.1093/restud/rdab099",
            "Bank Competition and Credit Supply",
            "By Elena Petrova and Kenji Watanabe*",
            "ABSTRACT",
            "* Petrova: Department of Economics, University of Warwick "
            "(email: e.petrova@warwick.ac.uk).",
            "Watanabe: Bank of Japan, 2-1-1 Nihonbashi-Hongokucho, Tokyo 103-8660.",
            "We study how bank competition affects credit supply in two columns of text",
            "that the extractor reads left to right, line by line, mixing both columns",
            "so that a naive parser sees prose where the byline used to be."])))
        self.assertEqual(result["title"], "Bank Competition and Credit Supply")
        self.assertEqual(result["authors"], "Elena Petrova and Kenji Watanabe")
        self.assertNotIn("University", result["authors"])     # affiliation noise
        self.assertNotIn("email", result["authors"])
        self.assertEqual(result["doi"], "10.1093/restud/rdab099")
        self.assertEqual(result["year"], "2022")

    def test_scanned_pages_without_text_layer(self):
        blank = [page("", number) for number in (1, 2, 3)]
        report = {}
        result = parse_metadata(blank, filename="smith-2019.pdf", report=report)
        self.assertEqual(result["title"], "")
        self.assertEqual(result["authors"], "")
        self.assertEqual(result["venue"], "")
        self.assertEqual(result["doi"], "")
        self.assertEqual(result["citation"], "")
        self.assertEqual(result["year"], "2019")       # file name only, flagged low
        self.assertEqual(report["fields"]["year"]["confidence"], "low")

    def test_clean_filename_title_is_used_when_text_is_unusable(self):
        report = {}
        result = parse_metadata([page("")], filename="hahn-et-al-2026-a-welfare-analysis-"
                                                     "of-policies-impacting-climate-change.pdf",
                                report=report)
        self.assertEqual(result["title"],
                         "A Welfare Analysis of Policies Impacting Climate Change")
        self.assertEqual(report["fields"]["title"]["confidence"], "low")
        self.assertEqual(result["doi"], "")            # never derived from a file name
        self.assertTrue(any("文件名" in warning for warning in report["warnings"]))


class TestDoiAndYear(unittest.TestCase):
    def test_doi_trailing_punctuation_is_stripped(self):
        result = parse_metadata(pages("See doi:10.1000/xyz123. for details."))
        self.assertEqual(result["doi"], "10.1000/xyz123")

    def test_doi_prefix_forms(self):
        for text in ("https://doi.org/10.1257/aer.20170973",
                     "doi:10.1257/aer.20170973",
                     "DOI 10.1257/aer.20170973",
                     "http://dx.doi.org/10.1257/aer.20170973"):
            self.assertEqual(parse_metadata(pages(text))["doi"], "10.1257/aer.20170973")

    def test_doi_keeps_balanced_parentheses(self):
        # trailing punctuation outside the DOI goes, parentheses inside stay
        self.assertEqual(parse_metadata(pages("See 10.1002/abc(2020)123."))["doi"],
                         "10.1002/abc(2020)123")
        self.assertEqual(parse_metadata(pages("See 10.5555/x(1)2)."))["doi"],
                         "10.5555/x(1)2")
        # the documented character class stops at < and > (as specified in the brief)
        result = parse_metadata(pages("10.1002/(SICI)1099-1255(199901)14:1<1::AID-JAE1>3.0."))
        self.assertTrue(result["doi"].startswith("10.1002/(SICI)1099-1255(199901)14:1"))
        self.assertFalse(result["doi"].endswith("."))

    def test_no_doi_invented(self):
        result = parse_metadata(pages("arXiv:2401.01234", "A title about something"),
                                filename="10.9999/fake-doi-2019.pdf")
        self.assertEqual(result["doi"], "")

    def test_year_is_not_taken_from_title_period_or_page_number(self):
        result = parse_metadata(pages("\n".join([
            "A Study of Chinese Inequality, 1978-2015",
            "By Jane Doe",
            "2019",
            "We study the period 1978 to 2015 in detail."])))
        self.assertEqual(result["title"], "A Study of Chinese Inequality, 1978-2015")
        self.assertEqual(result["year"], "")

    def test_year_from_copyright_and_masthead(self):
        self.assertEqual(parse_metadata(pages("© 2018 by Jane Doe. All rights reserved."))["year"],
                         "2018")
        self.assertEqual(parse_metadata(pages("American Economic Review 2019, 109(7): 2469-2496"))["year"],
                         "2019")
        self.assertEqual(parse_metadata(pages("Journal of X, Vol. 12, No. 3 (2019), pp. 45-67"))["year"],
                         "2019")

    def test_year_rejects_page_and_volume_numbers(self):
        result = parse_metadata(pages("Journal of Testing 5(1): 2469-2496", "1119"))
        self.assertEqual(result["year"], "")


class TestAuthors(unittest.TestCase):
    def test_byline_without_by_prefix_is_found(self):
        """The parent's fixture: no "By", ALL-CAPS title, list byline on one line."""
        report = {}
        result = parse_metadata(pages("\n".join([
            "GLOBAL SYNTHETIC CYCLES AND RISK PREMIUMS",
            "Jane Q. Author, John Doe",
            "NBER Working Paper No. 24677",
            "June 2018",
            "doi:10.1234/synthetic.2018.1"])), report=report)
        self.assertEqual(result["authors"], "Jane Q. Author and John Doe")
        self.assertEqual(result["title"], "Global Synthetic Cycles and Risk Premiums")
        self.assertEqual(result["year"], "2018")
        self.assertEqual(result["venue"], "NBER Working Paper No. 24677")
        self.assertEqual(result["doi"], "10.1234/synthetic.2018.1")
        self.assertEqual(result["citation"],
                         "Jane Q. Author and John Doe (2018). Global Synthetic Cycles and "
                         "Risk Premiums. NBER Working Paper No. 24677. "
                         "https://doi.org/10.1234/synthetic.2018.1")
        self.assertEqual(report["fields"]["authors"]["confidence"], "high")

    def test_byline_without_by_prefix_followed_by_abstract_heading(self):
        result = parse_metadata(pages("\n".join([
            "Quarterly Journal of Testing 2020, 3(1): 1-9",
            "Firm Dynamics and Credit Supply",
            "Jane Q. Author, John Doe",
            "Abstract",
            "We study firm dynamics."])))
        self.assertEqual(result["authors"], "Jane Q. Author and John Doe")
        self.assertEqual(result["title"], "Firm Dynamics and Credit Supply")

    def test_title_continuation_is_not_a_byline(self):
        """Precision guard: a title line that parses as two names must stay a title."""
        result = parse_metadata(pages("\n".join([
            "Journal of Testing 2020, 3(1): 1-9",
            "Firm Heterogeneity, Credit Constraints",
            "and the Credit Channel",
            "Abstract",
            "We study firm heterogeneity and the credit channel."])))
        self.assertEqual(result["authors"], "")
        self.assertEqual(result["title"], "Firm Heterogeneity, Credit Constraints and "
                                          "the Credit Channel")

    def test_affiliation_phrases_never_pass_the_name_gate(self):
        for line in ("Trade Policy, Firm Heterogeneity",
                     "Economic Growth, Climate Impacts",
                     "Department of Economics, University of Warwick",
                     "National Bureau of Economic Research, Cambridge"):
            self.assertEqual(parse_metadata(pages(line + "\nAbstract\nWe study."))["authors"],
                             "", line)

    def test_byline_continuation_and_hyphenated_surname(self):
        result = parse_metadata(pages("\n".join([
            "Some Journal 2020, 1(1): 1-2",
            "The Title of the Paper",
            "By Robert W. Hahn, Nathaniel Hendren,",
            "Robert D. Metcalfe, and Befln Sprung-Keyser*"])))
        self.assertEqual(result["authors"],
                         "Robert W. Hahn, Nathaniel Hendren, Robert D. Metcalfe, "
                         "and Befln Sprung-Keyser")

    def test_trailing_ligature_artifact_is_removed_and_reported(self):
        report = {}
        result = parse_metadata(pages("\n".join([
            "AER 2019, 109(7): 1-9",
            "A Title",
            "By Thomas Piketty, Li Yang, and Gabriel Zucmanfi*"])), report=report)
        self.assertEqual(result["authors"], "Thomas Piketty, Li Yang, and Gabriel Zucman")
        self.assertTrue(report["fields"]["authors"]["artifact_stripped"])

    def test_prose_and_affiliations_never_become_authors(self):
        result = parse_metadata(pages("\n".join([
            "A Title of a Paper",
            "We thank the Department of Economics at the University of Somewhere",
            "for generous support, and the editor for comments."])))
        self.assertEqual(result["authors"], "")

    def test_all_caps_heading_is_not_a_byline(self):
        result = parse_metadata(pages("\n".join([
            "AN IMPORTANT TITLE GOES HERE",
            "JOHN SMITH",
            "We study things in this paper."])))
        self.assertEqual(result["authors"], "")

    def test_authors_from_copyright_line_are_flagged(self):
        report = {}
        result = parse_metadata(pages("\n".join([
            "Working Paper 1234",
            "A Title About Cycles",
            "© 2018 by Moritz Schularick, Alan M. Taylor, and Felix Ward. "
            "All rights reserved."])), report=report)
        self.assertIn("Schularick", result["authors"])
        self.assertEqual(report["fields"]["authors"]["confidence"], "medium")

    def test_cjk_spacing_and_date(self):
        result = parse_metadata(pages("学术财经 ˙ 学术笔记 全 汇总\n2023 年 12 月 14 日"))
        self.assertEqual(result["title"], "学术财经˙学术笔记全汇总")
        self.assertEqual(result["year"], "2023")


class TestTitle(unittest.TestCase):
    def test_masthead_is_never_the_title(self):
        result = parse_metadata(pages("\n".join([
            "American Economic Review 2019, 109(7): 2469-2496",
            "https://doi.org/10.1257/aer.20170973",
            "Real Title Here",
            "By Jane Doe"])))
        self.assertEqual(result["title"], "Real Title Here")

    def test_all_caps_title_prefers_mixed_case_twin(self):
        result = parse_metadata(pages(
            "CAPS JOURNAL 2020, 1(1): 1-9\nGLOBAL RISK AND THE DOLLAR\nJohn Smith\n"
            "Working Paper 12\nJune 2020",
            "Global Risk and the Dollar\nJohn Smith\nNBER Working Paper No. 12"))
        self.assertEqual(result["title"], "Global Risk and the Dollar")
        self.assertEqual(result["authors"], "John Smith")

    def test_all_caps_title_is_recased_conservatively(self):
        result = parse_metadata(pages("\n".join([
            "NBER WORKING PAPER SERIES",
            "MONETARY POLICY AND THE GLOBAL FINANCIAL CYCLE",
            "scar Jord",
            "Working Paper 24677",
            "June 2018"])))
        self.assertEqual(result["title"], "Monetary Policy and the Global Financial Cycle")
        self.assertEqual(result["authors"], "scar Jord")

    def test_acronyms_survive_recasing(self):
        self.assertEqual(metadata._smart_title_case("THE IMPACT OF COVID-19 ON US GDP"),
                         "The Impact of COVID-19 on US GDP")
        self.assertEqual(metadata._smart_title_case("A GARCH MODEL OF THE LASSO"),
                         "A GARCH Model of the LASSO")

    def test_uppercase_word_is_not_kept_just_for_being_short(self):
        """RISK is a word; GDP is an acronym *because the paper uses it that way*."""
        result = parse_metadata(pages("\n".join([
            "NBER WORKING PAPER SERIES",
            "GLOBAL RISK AND THE DOLLAR",
            "Jane Q. Author, John Doe",
            "Abstract",
            "We study GDP growth and the dollar."])))
        self.assertEqual(result["title"], "Global Risk and the Dollar")
        result = parse_metadata(pages("\n".join([
            "NBER WORKING PAPER SERIES",
            "THE IMPACT OF GDP ON GLOBAL RISK",
            "Jane Q. Author, John Doe",
            "Abstract",
            "We study GDP growth."])))
        self.assertEqual(result["title"], "The Impact of GDP on Global Risk")

    def test_title_from_filename_is_reported_not_silent(self):
        report = {}
        result = parse_metadata([page("")],
                                filename="piketty-et-al-2019-capital-accumulation-"
                                         "private-property-and-rising-inequality.pdf",
                                report=report)
        self.assertEqual(result["title"], "Capital Accumulation Private Property and "
                                          "Rising Inequality")
        self.assertEqual(report["fields"]["title"]["source"], "文件名")

    def test_footnote_dagger_is_stripped_from_title(self):
        result = parse_metadata(pages("\n".join([
            "AER 2019, 109(7): 1-9",
            "A Welfare Analysis of Policies",
            "Impacting Climate Change †",
            "By Jane Doe and John Smith"])))
        self.assertEqual(result["title"],
                         "A Welfare Analysis of Policies Impacting Climate Change")


class TestPerformance(unittest.TestCase):
    def test_parse_is_milliseconds(self):
        path = HAHN or PIKETTY or NBER or CHINESE
        if path is None:
            self.skipTest("no cache present")
        payload = json.loads(path.read_text(encoding="utf-8"))
        start = time.perf_counter()
        for _ in range(20):
            parse_metadata(payload)
        elapsed = (time.perf_counter() - start) / 20
        self.assertLess(elapsed, 0.050, f"{elapsed * 1000:.2f} ms per parse")

    def test_only_front_matter_is_read(self):
        """A huge document must cost the same as a short one (no full scan)."""
        front = "Journal of Testing 2020, 5(1): 1-9\nA Title Here\nBy Jane Doe"
        huge = pages(front, *["body " * 2000] * 400)
        start = time.perf_counter()
        result = parse_metadata(huge)
        elapsed = time.perf_counter() - start
        self.assertEqual(result["authors"], "Jane Doe")
        self.assertLess(elapsed, 0.200)


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass
    unittest.main(verbosity=2)
