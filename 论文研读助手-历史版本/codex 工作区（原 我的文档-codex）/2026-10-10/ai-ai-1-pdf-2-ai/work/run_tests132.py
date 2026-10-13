from pathlib import Path
import os, sys, tempfile, unittest, json, io, time
import uuid
ROOT = Path(__file__).resolve().parent
CODE = ROOT / "review-1.3.2"
TMP = ROOT / "tmp132"
TMP.mkdir(exist_ok=True)
tempfile.tempdir = str(TMP)
def sandbox_mkdtemp(suffix=None, prefix=None, dir=None):
    # Python 3.12.14 Windows mode=0700 creates an ACL that excludes the sandbox token.
    # Only review scratch fixtures use this override; application code is unchanged.
    parent = Path(dir or TMP)
    dest = parent / ((prefix or "tmp") + uuid.uuid4().hex + (suffix or ""))
    dest.mkdir(mode=0o777)
    return str(dest)
tempfile.mkdtemp = sandbox_mkdtemp
os.environ["PAPER_READER_NO_DIALOG"] = "1"
os.environ["TEMP"] = os.environ["TMP"] = str(TMP)
sys.path.insert(0, str(CODE))
sys.path.insert(0, str(CODE / "tests"))
import pdf_fixtures
papers = CODE / "data/papers"
papers.mkdir(parents=True, exist_ok=True)
for paper_id in ("medical-2026-zang", "fertility-2026-coffey", "finance-2021-zhang"):
    fixture = papers / (paper_id + ".pdf")
    pdf_fixtures.write_pdf(fixture, pdf_fixtures.zh_document())
    fixture.write_bytes(fixture.read_bytes() + b"\n% synthetic padding\n" + b" " * 70000)
os.chdir(CODE)
suite = unittest.defaultTestLoader.discover(str(CODE / "tests/security"), pattern="test_phase*.py")
stream = io.StringIO()
start=time.time()
result = unittest.TextTestRunner(stream=stream, verbosity=1).run(suite)
text = stream.getvalue()
(ROOT / "security132-tests.txt").write_text(text, encoding="utf-8")
summary={"tests": result.testsRun,"failures":len(result.failures),"errors":len(result.errors),
         "skipped":len(result.skipped),"seconds":round(time.time()-start,2),
         "problems":[{"test":str(t),"tail":"\n".join(trace.splitlines()[-10:])} for t,trace in result.errors+result.failures]}
(ROOT / "security132-tests.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding="utf-8")
print(json.dumps({k:v for k,v in summary.items() if k!="problems"},ensure_ascii=True,indent=2))
for issue in summary["problems"]:
    print(issue["test"], issue["tail"].splitlines()[-1])

sys.exit(0 if result.wasSuccessful() else 1)
