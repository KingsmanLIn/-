from pathlib import Path
import sys,tempfile,uuid,os,json,unittest,io,importlib
ROOT=Path(__file__).resolve().parent
CODE=ROOT/"review-1.3.2"
TMP=ROOT/"more132-data"
TMP.mkdir(exist_ok=True)
def mkdtemp(suffix=None,prefix=None,dir=None):
    p=Path(dir or TMP)/((prefix or "tmp")+uuid.uuid4().hex+(suffix or ""))
    p.mkdir(mode=0o777)
    return str(p)
tempfile.tempdir=str(TMP);tempfile.mkdtemp=mkdtemp
sys.path[:0]=[str(CODE),str(CODE/"tests"),str(CODE/"tests/security")]
os.environ["PAPER_READER_NO_DIALOG"]="1"
os.environ["TEMP"]=os.environ["TMP"]=str(TMP)
os.chdir(CODE)
mode=sys.argv[1]
if mode=="limit_test":
    # Ensure the fixture actually meets the test's >64KiB premise.
    for p in (CODE/"data/papers").glob("*.pdf"):
        if p.stat().st_size<=65536:
            with p.open("ab") as out:out.write(b"\n% "+b"synthetic-padding "*4200+b"\n")
    import test_phase3_session
    suite=unittest.defaultTestLoader.loadTestsFromName(
        "RequestLimits.test_import_keeps_its_own_large_limit",test_phase3_session)
    text=io.StringIO();result=unittest.TextTestRunner(stream=text).run(suite)
    print(text.getvalue())
    (ROOT/"limit132-test.txt").write_text(text.getvalue(),encoding="utf-8")
else:
    module=importlib.import_module({"channel":"phase1_channel_scan","https":"phase2_e2e_https"}[mode])
    code=module.main()
    print("scanner_exit",code)
