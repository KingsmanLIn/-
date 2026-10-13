"""Independent regressions for the 1.3.2 review. All data and keys are synthetic."""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import zlib
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT/'tests'), str(ROOT/'tests/security')]
import analysis, credentials, endpoint_policy, extraction, job_policy, pdf_worker
import pdftext, providers, secret_guard, secure_transport, server
from baseline_helpers import fresh_library_v1
from card_fixtures import build_card, pages_from_prompt
from stubs import CountingSink

PAPER = 'medical-2026-zang'

@contextmanager
def web_server(handler):
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        httpd.server_close()

class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='review-fixed-'))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        fresh_library_v1(self.tmp)
        self.store = server.Store(self.tmp)

    def settings(self, port=1, **extra):
        self.store.update_ai_settings(dict(provider='openai_compatible', mode='local_loopback',
            base_url=f'http://127.0.0.1:{port}/v1', model='stub-model', **extra))

    def fake(self):
        fake = providers.FakeProvider(script=['success'], payload={})
        fake.payload_for = lambda request, _: build_card(pages_from_prompt(request.user))
        return fake

class Jobs(Case):
    def test_configuration_rebind_never_sends_old_task_to_new_sink(self):
        with CountingSink() as old, CountingSink() as new:
            self.settings(old.port, api_key_env='PAPER_READER_REGRESSION_KEY')
            with mock.patch.dict(os.environ, {'PAPER_READER_REGRESSION_KEY': 'synthetic-credential-A'}):
                manager = analysis.AnalysisManager(self.store)
                manager.enqueue(PAPER, reason='manual')
                frozen = manager.ledger.frozen_job(PAPER)
                self.store.update_ai_settings({'base_url': f'http://127.0.0.1:{new.port}/v1'})
                manager.process_once()
                self.assertEqual((0, 0), (old.count, new.count))
                self.assertFalse(manager._snapshot_valid(frozen))
                self.assertEqual('waiting_for_user', self.store.analysis_meta(PAPER)['status'])

    def test_pause_after_connection_before_write_blocks_real_http_bytes(self):
        with CountingSink() as sink:
            self.settings(sink.port)
            manager = analysis.AnalysisManager(self.store)
            manager.enqueue(PAPER, reason='manual')
            original = secure_transport.SecureTransport._connect
            def connected(transport, client):
                sock = original(transport, client)
                manager.pause(True)
                return sock
            with mock.patch.object(secure_transport.SecureTransport, '_connect', connected):
                manager.process_once()
            self.assertEqual(0, sink.count)
            self.assertEqual(0, manager.ledger.spend_today())

    def test_same_environment_name_key_rotation_invalidates_queued_job(self):
        with CountingSink() as sink:
            self.settings(sink.port, api_key_env='PAPER_READER_ROTATION_TEST')
            manager = analysis.AnalysisManager(self.store)
            with mock.patch.dict(os.environ, {'PAPER_READER_ROTATION_TEST': 'synthetic-first-token'}):
                manager.enqueue(PAPER, reason='manual')
                frozen = manager.ledger.frozen_job(PAPER)
            with mock.patch.dict(os.environ, {'PAPER_READER_ROTATION_TEST': 'synthetic-second-token'}):
                self.assertFalse(manager._snapshot_valid(frozen))
                manager.process_once()
            self.assertEqual(0, sink.count)

    def test_late_failure_cannot_overwrite_new_ready_answer(self):
        self.settings()
        class Late(providers.ProviderAdapter):
            name = 'synthetic-late'
            request_sent = True
            def __init__(self):
                self.entered, self.release = threading.Event(), threading.Event()
            def complete_json(self, request, on_start=None):
                self.entered.set()
                self.release.wait(10)
                return providers.AdapterResult(False, category=providers.CATEGORY_AUTH,
                    public_code=secret_guard.E_AUTH_FAILED)
        late = Late()
        manager = analysis.AnalysisManager(self.store, provider_factory=lambda _: late)
        manager.enqueue(PAPER, reason='manual')
        runner = threading.Thread(target=manager.process, args=(PAPER,))
        runner.start()
        try:
            self.assertTrue(late.entered.wait(8))
            fake = self.fake()
            manager.provider_factory = lambda _: fake
            manager.enqueue(PAPER, reason='force', force=True)
            manager.process(PAPER)
            self.assertEqual('ready', self.store.analysis_meta(PAPER)['status'])
            before = self.store.files.analysis_path(PAPER).read_bytes()
        finally:
            late.release.set()
            runner.join(12)
        self.assertEqual('ready', self.store.analysis_meta(PAPER)['status'])
        self.assertEqual(before, self.store.files.analysis_path(PAPER).read_bytes())

    def test_two_managers_share_budget_and_consent(self):
        first = analysis.AnalysisManager(self.store)
        second = analysis.AnalysisManager(self.store)
        self.assertIs(first.ledger, second.ledger)

    def test_cancelled_unsent_adaptation_reservation_is_refunded(self):
        self.settings()
        manager = analysis.AnalysisManager(self.store)
        manager.enqueue(PAPER, reason='manual')
        context = manager.build_context(PAPER)
        class CancelAfterReservation(providers.ProviderAdapter):
            name = 'synthetic-adaptation-accounting'
            request_sent = True
            transmissions = 1
            def complete_json(self, request, on_start=None):
                self.on_retransmit('response_format')
                return providers.AdapterResult(False, category=providers.CATEGORY_CANCELLED,
                                               public_code=secret_guard.E_CANCELLED)
        manager._dispatch(PAPER, context, CancelAfterReservation(),
            providers.CompletionRequest(system='s',user='u',model='stub-model'))
        self.assertEqual(1, manager.ledger.spend_today())
        self.assertEqual(2, len(manager.ledger.entries_for(context['job'].job_id)))

    def test_budget_above_1000_is_exact_after_restart(self):
        ledger = job_policy.JobLedger(self.tmp/'budget')
        limits = {'daily_request_limit': 1001}
        for iteration in range(1001):
            reservation, code = ledger.reserve(None, limits)
            self.assertIsNotNone(reservation, f'iteration {iteration}: {code}, {ledger.write_error}')
        self.assertEqual(1001, ledger.spend_today())
        again = job_policy.JobLedger(self.tmp/'budget')
        self.assertEqual(1001, again.spend_today())
        self.assertEqual(job_policy.DENY_DAY_LIMIT, again.reserve(None, limits)[1])

    def test_reservation_write_failure_refuses_send(self):
        ledger = job_policy.JobLedger(self.tmp/'bad-budget')
        with mock.patch.object(ledger, '_save_locked', return_value=False):
            reservation, _ = ledger.reserve(None, {'daily_request_limit': 10})
        self.assertIsNone(reservation)

    def test_injected_budget_clock_is_used(self):
        ledger = job_policy.JobLedger(self.tmp/'clock', clock=lambda: 10)
        ledger.reserve(None, {'daily_request_limit': 1})
        self.assertEqual(1, ledger.spend_today(now=10))
        self.assertEqual(0, ledger.spend_today(now=86411))

    def test_corrupt_ledger_is_preserved_and_cannot_reset_budget_on_restart(self):
        folder = self.tmp/'corrupt-ledger'
        folder.mkdir()
        path = folder/job_policy.LEDGER_FILE
        path.write_bytes(b'{broken budget')
        ledger = job_policy.JobLedger(folder)
        ledger.set_paused(False)
        self.assertEqual(b'{broken budget', path.read_bytes())
        restarted = job_policy.JobLedger(folder)
        self.assertTrue(restarted.load_error)
        self.assertIsNone(restarted.reserve(None, {'daily_request_limit':100})[0])

class ApiAndOutput(Case):
    def call(self, httpd, path, body=None):
        import urllib.request, urllib.error
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f'http://127.0.0.1:{httpd.server_port}{path}', data=data,
            headers={'X-Reader-Token': 'regression-token', 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as response:
            return response.code, response.read()

    def test_gui_settings_route_preserves_cloud_mode_and_limits(self):
        handler = server.handler_for(self.store, 'regression-token', None)
        with web_server(handler) as httpd:
            status, _ = self.call(httpd, '/api/settings', {
                'provider': 'openai_compatible', 'mode': 'cloud_https',
                'base_url': 'https://api.example.invalid/v1', 'model': 'synthetic-model',
                'max_output_tokens': 777, 'limits': {'daily_request_limit': 1001}})
        self.assertEqual(200, status)
        saved = self.store.ai_settings()
        self.assertEqual('cloud_https', saved['mode'])
        self.assertEqual(777, saved['max_output_tokens'])
        self.assertEqual(1001, saved['limits']['daily_request_limit'])

    def test_ai_settings_route_refuses_unregistered_environment_reference(self):
        with web_server(server.handler_for(self.store, 'regression-token', None)) as httpd:
            status, _ = self.call(httpd, '/api/ai-settings', {'api_key_env': 'AWS_SECRET_ACCESS_KEY'})
        self.assertEqual(400, status)
        self.assertEqual('', self.store.ai_settings()['api_key_env'])

    def test_echoed_key_in_real_models_reply_never_reaches_analysis_http(self):
        marker = 'synthetic-model-list-credential'
        class Echo(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                body = json.dumps({'data': [{'id': marker}]}).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        with web_server(Echo) as upstream:
            self.settings(upstream.server_port, api_key_env='PAPER_READER_MODELS_ECHO')
            manager = analysis.AnalysisManager(self.store)
            manager.approve_target()
            with mock.patch.dict(os.environ, {'PAPER_READER_MODELS_ECHO': marker}):
                with web_server(server.handler_for(self.store, 'regression-token', manager)) as httpd:
                    status, blob = self.call(httpd, '/api/analysis')
            self.assertEqual(200, status)
            self.assertNotIn(marker.encode(), blob)
            self.assertIn(b'secret_echo', blob)

    def test_health_dto_backstop_blocks_provider_models_echo(self):
        marker = 'synthetic-direct-health-token'
        secret_guard.register(marker)
        health = providers.Health(True, models=[marker]).as_dict()
        self.assertFalse(health['ok'])
        self.assertEqual([], health['models'])
        self.assertNotIn(marker, json.dumps(health))

    def test_five_character_key_and_more_than_32_rotations_are_guarded(self):
        short = 'qZ7!x'
        self.assertTrue(credentials.validate_value(short)[0])
        secret_guard.register(short)
        old = 'synthetic-first-retained-key'
        secret_guard.register(old)
        for n in range(40):
            secret_guard.register(f'synthetic-rotation-key-{n:03}')
        self.assertTrue(secret_guard.contains_secret(short))
        self.assertTrue(secret_guard.contains_secret(old))
        self.assertFalse(credentials.validate_value('abcd')[0])

class ParserAndTransport(Case):
    def test_duplicate_key_and_nonfinite_content_rejected_by_production_adapter(self):
        request = providers.CompletionRequest(system='s', user='u', model='m')
        for content in ('{"overall":1,"overall":2}', '{"overall":NaN}', '{"overall":Infinity}'):
            _, error = providers.HttpAdapter._load_json(content, request)
            self.assertIsNotNone(error)
            self.assertEqual(secret_guard.E_INVALID_RESPONSE, error.public_code)

    def test_cancel_on_optional_parameter_error_prevents_adaptation_post(self):
        class CancelOnReply(providers.OpenAICompatibleAdapter):
            calls = 0
            def _post_json(self, path, payload, timeout):
                self.calls += 1
                self.cancel()
                self.last_transport = secure_transport.TransportResponse(False, status=400,
                    rejected_parameters=('response_format',))
                return None, providers.AdapterResult(False, category=providers.CATEGORY_HTTP,
                    http_status=400)
        adapter = CancelOnReply(model='m')
        adapter.on_retransmit = lambda _: True
        result = adapter.complete_json(providers.CompletionRequest(system='s', user='u', model='m'))
        self.assertEqual(1, adapter.calls)
        self.assertEqual(providers.CATEGORY_CANCELLED, result.category)

    def test_total_deadline_stops_drip_body(self):
        class Drip(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Length', '20')
                self.end_headers()
                try:
                    for _ in range(20):
                        self.wfile.write(b'x')
                        self.wfile.flush()
                        time.sleep(.03)
                except OSError: pass
        with web_server(Drip) as upstream:
            policy = endpoint_policy.EndpointPolicy.for_settings(f'http://127.0.0.1:{upstream.server_port}')
            policy.authorize()
            transport = secure_transport.SecureTransport(policy, read_timeout=.1)
            start = time.monotonic()
            response = transport.get_json('/models')
            elapsed = time.monotonic()-start
        self.assertFalse(response.ok)
        self.assertEqual(secret_guard.E_TIMEOUT, response.code)
        self.assertLess(elapsed, .4)

    def test_dns_resolution_obeys_deadline(self):
        with mock.patch.object(endpoint_policy, 'resolve', side_effect=lambda *args: time.sleep(.2)):
            start = time.monotonic()
            with self.assertRaises(endpoint_policy.PolicyError):
                endpoint_policy.bounded_resolve('example.invalid', 443, timeout=.03)
            self.assertLess(time.monotonic()-start, .15)

class PdfIsolation(Case):
    def test_normal_analysis_extractor_is_child_and_extracts_fixture(self):
        manager = analysis.AnalysisManager(self.store)
        extractor = manager.extractor_factory()
        self.assertIsInstance(extractor, extraction.IsolatedPdfTextExtractor)
        result = extractor.extract(self.store.files.pdf_path(PAPER), PAPER)
        self.assertTrue(result.ok, result.code)
        self.assertGreater(result.total_chars, 100)

    def test_compressed_pdf_stream_is_bounded_during_expansion(self):
        data = zlib.compress(b'x' * (pdftext.MAX_STREAM_BYTES+1))
        with self.assertRaises(pdftext.ExtractionError):
            pdftext.bounded_inflate(data)

    def test_child_environment_has_no_key_or_python_injection(self):
        with mock.patch.dict(os.environ, {'PAPER_READER_ENV_SENTINEL': 'synthetic-env-token',
            'PYTHONPATH': 'unsafe', 'PYTHONSTARTUP': 'unsafe', 'HTTPS_PROXY': 'unsafe'}):
            clean = pdf_worker.scrub_environment()
        self.assertNotIn('PAPER_READER_ENV_SENTINEL', clean)
        self.assertNotIn('PYTHONPATH', clean)
        self.assertNotIn('PYTHONSTARTUP', clean)
        self.assertNotIn('HTTPS_PROXY', clean)

    def test_timeout_kills_child_and_cleans_scratch(self):
        before = set(Path(tempfile.gettempdir()).glob('extract-*'))
        with mock.patch.object(pdf_worker, 'CHILD_SCRIPT', 'import time\ntime.sleep(30)\n'):
            _, info, _ = pdf_worker.extract_in_subprocess(self.store.files.pdf_path(PAPER), PAPER, timeout=.08)
        self.assertEqual('extract_timeout', info['code'])
        self.assertEqual(before, set(Path(tempfile.gettempdir()).glob('extract-*')))

    @unittest.skipUnless(os.name == 'nt', 'Windows Job Object enforcement')
    def test_child_memory_limit_terminates_large_allocation(self):
        script = '''import sys, time
from pathlib import Path
while not Path(sys.argv[5]).exists(): time.sleep(.01)
data = bytearray(700*1024*1024)
Path(sys.argv[4]).write_text('{}')
'''
        with mock.patch.object(pdf_worker, 'CHILD_SCRIPT', script):
            cache, info, _ = pdf_worker.extract_in_subprocess(self.store.files.pdf_path(PAPER), PAPER, timeout=5)
        self.assertIsNone(cache)
        self.assertEqual('extract_failed', info['code'])

if __name__ == '__main__':
    unittest.main()
