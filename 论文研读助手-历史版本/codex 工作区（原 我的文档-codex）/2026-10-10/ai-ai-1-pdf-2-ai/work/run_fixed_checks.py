"""Run isolated security, TLS scanners and selected functional regressions."""
from pathlib import Path
import io
import json
import os
import sys
import tempfile
import time
import unittest
import uuid

HERE = Path(__file__).resolve().parent
CODE = HERE / 'review-1.3.2'
TMP = HERE / 'fixed-check-scratch'
TMP.mkdir(exist_ok=True)
def mkdtemp(suffix=None, prefix=None, dir=None):
    dest = Path(dir or TMP) / ((prefix or 'tmp') + uuid.uuid4().hex + (suffix or ''))
    dest.mkdir(mode=0o777)
    return str(dest)
tempfile.mkdtemp = mkdtemp
tempfile.tempdir = str(TMP)
os.environ.update(PAPER_READER_NO_DIALOG='1', PAPER_READER_NO_BROWSER='1',
                  TEMP=str(TMP), TMP=str(TMP))
sys.path[:0] = [str(CODE), str(CODE/'tests'), str(CODE/'tests/security')]
os.chdir(CODE)
mode = sys.argv[1] if len(sys.argv)>1 else 'security'
if mode == 'scanners':
    import phase1_channel_scan, phase2_e2e_https
    codes = [phase1_channel_scan.main(), phase2_e2e_https.main()]
    sys.exit(max(codes))
patterns = ['test_phase*.py'] if mode == 'security' else [
    'test_pdftext.py', 'test_extraction.py', 'test_chunk_cache.py',
    'test_reading_card.py', 'test_reading_card_jobs.py', 'test_analysis_jobs.py',
    'test_storage.py', 'test_backup_restore.py', 'test_api_v2.py', 'test_startup.py']
folder = CODE/'tests/security' if mode == 'security' else CODE/'tests'
suite = unittest.TestSuite()
for pattern in patterns:
    suite.addTests(unittest.defaultTestLoader.discover(str(folder), pattern=pattern))
stream = io.StringIO()
start = time.monotonic()
result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
summary = {'tests': result.testsRun, 'failures':len(result.failures), 'errors':len(result.errors),
           'skipped':len(result.skipped), 'seconds':round(time.monotonic()-start,2),
           'problems':[{'test':str(t), 'trace':trace} for t,trace in result.errors+result.failures]}
(HERE/f'fixed-{mode}.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
(HERE/f'fixed-{mode}.txt').write_text(stream.getvalue(),encoding='utf-8')
print(json.dumps({k:v for k,v in summary.items() if k!='problems'},indent=2))
for problem in summary['problems']:
    print(problem['test'], problem['trace'].splitlines()[-1])
sys.exit(0 if result.wasSuccessful() else 1)
