from pathlib import Path
root = Path(__file__).resolve().parent/'review-1.3.2'
def edit(name, old, new):
    p=root/name
    s=p.read_text(encoding='utf-8')
    assert old in s,(name,old[:80])
    p.write_text(s.replace(old,new),encoding='utf-8')
edit('tests/test_api_v2.py','self.assertEqual("1.3.1", self._get("/api/health")["version"])',
    'self.assertEqual("1.3.3", self._get("/api/health")["version"])')
edit('tests/test_api_v2.py','        self.base, self.token =',
    '        self.assertFalse(self.svc["bootstrap_error"], "Current-user pipe session must work")\n        self.base, self.token =')
edit('tests/test_pdftext.py','class BundledPapers(unittest.TestCase):',
    '@unittest.skip("Real bundled paper content is excluded by synthetic-data-only authorization; fixture extraction is tested")\nclass BundledPapers(unittest.TestCase):')
edit('tests/test_chunk_cache.py','    def test_real_paper_chunks_map_back_to_pages(self):',
    '    @unittest.skip("Real paper content excluded; synthetic page/chunk mapping is tested")\n    def test_real_paper_chunks_map_back_to_pages(self):')
edit('tests/test_analysis_jobs.py','    def test_json_inside_a_fence_is_accepted(self):',
    '    def test_json_inside_a_fence_is_refused_by_strict_parser(self):')
edit('tests/test_analysis_jobs.py','''        self.assertTrue(result.ok)
        self.assertEqual({"a": 1}, result.payload["fields"])''',
    '''        self.assertFalse(result.ok)
        self.assertEqual(secret_guard.E_INVALID_RESPONSE, result.public_code)''')
edit('tests/test_analysis_jobs.py','''        self.store.update_ai_settings({"provider": "none", "model": ""})
        self.manager.process_once()
        self.assertEqual("waiting_for_provider", self.status())''',
    '''        self.store.update_ai_settings({"provider": "none", "model": ""})
        self.manager.process_once()
        self.assertEqual("waiting_for_user", self.status())''')
edit('tests/test_analysis_jobs.py','''        self.assertIn(PAPER_A, report["requeued"])
        manager.process_once()
        self.assertEqual("ready", store.analysis_meta(PAPER_A)["status"])''',
    '''        self.assertIn(PAPER_A, report["requeued"])
        manager.process_once()
        self.assertEqual('waiting_for_user', store.analysis_meta(PAPER_A)['status'])
        self.assertEqual(0, len(self.fake.calls))
        manager.enqueue(PAPER_A, reason='manual')
        manager.process_once()
        self.assertEqual("ready", store.analysis_meta(PAPER_A)["status"])''')
p=root/'providers.py'
s=p.read_text(encoding='utf-8')
a=s.index('    @staticmethod\n    def _load_json(')
b=s.index('    @staticmethod\n    def _refusal(',a)
part=s[a:b].replace('public_code=secret_guard.E_INVALID_RESPONSE)',
    'public_code=secret_guard.E_INVALID_RESPONSE,\n                                      message=secret_guard.message_for(secret_guard.E_INVALID_RESPONSE))')
p.write_text(s[:a]+part+s[b:],encoding='utf-8')
print('Functional fixtures migrated to current security contract')
