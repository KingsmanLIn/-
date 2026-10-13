"""Phase 6 tests: one-piece regression, provider compatibility, capability adaptation.

These tests drive a real loopback server with a stub provider, so they exercise the same
paths the product uses - HTTPS for a cloud-shaped target, plain HTTP for a local one - without
any real credential or any real API call. Compatible-provider claims in the report come from
here: the stub speaks the wire protocol, not the vendor's service.

    python -m unittest discover -s tests/security -p "test_phase6_*.py"
"""
from __future__ import annotations

import json
import shutil
import socket
import ssl
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tests" / "security"))

import credentials     # noqa: E402
import endpoint_policy  # noqa: E402
import providers       # noqa: E402
import secret_guard    # noqa: E402
import secure_transport  # noqa: E402
import server          # noqa: E402

TEST_KEY = "sk-test-PHASE6-not-a-real-key-0001"
ENV_NAME = "PAPER_READER_PHASE6_KEY"


class StubHandler(BaseHTTPRequestHandler):
    """A scriptable OpenAI-compatible / Ollama-shaped endpoint."""

    protocol_version = "HTTP/1.1"
    seen = []
    script = {}
    refuse_parameters = ()

    def log_message(self, *args):
        pass

    def _reply(self, status, payload=None, body=None):
        data = body if body is not None else json.dumps(payload or {}).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        type(self).seen.append({"method": "GET", "path": self.path,
                                "headers": dict(self.headers)})
        if self.path.endswith("/models"):
            self._reply(200, self.script.get("models", {"data": [{"id": "stub-model"}]}))
            return
        if self.path.endswith("/api/tags"):
            self._reply(200, self.script.get("tags", {"models": [{"name": "stub-model"}]}))
            return
        self._reply(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except ValueError:
            payload = {}
        type(self).seen.append({"method": "POST", "path": self.path, "payload": payload,
                                "headers": dict(self.headers)})
        for name in self.refuse_parameters:
            if name in payload:
                # One name per reply, as a real server does: it stops on the first parameter
                # it does not recognise and says which one, rather than listing every
                # parameter it would also have rejected.
                self._reply(400, {"error": {"message": f"unknown parameter {name}",
                                            "type": "invalid_request_error"}})
                return

        status = self.script.get("status", 200)
        if status != 200:
            self._reply(status, body=self.script.get("body", b"{}"))
            return
        if self.path.endswith("/api/chat"):
            content = self.script.get("content", "{}")
            self._reply(200, {"message": {"role": "assistant", "content": content}})
            return
        content = self.script.get("content", "{}")
        self._reply(200, {"choices": [{"message": {"role": "assistant", "content": content},
                                       "finish_reason": "stop"}],
                          "usage": {"prompt_tokens": 10, "completion_tokens": 5}})

    def do_HEAD(self):
        self._reply(200, {})


class StubServer:
    def __init__(self, tls=False, ca=None):
        self.socket = socket.socket()
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(("127.0.0.1", 0))
        self.port = self.socket.getsockname()[1]
        self.socket.listen(8)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), StubHandler,
                                         bind_and_activate=False)
        self.httpd.socket.close()
        self.httpd.socket = self.socket
        self.httpd.server_address = ("127.0.0.1", self.port)
        if tls:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(str(ca["cert"]), str(ca["key"]))
            self.httpd.socket = context.wrap_socket(self.httpd.socket, server_side=True)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self):
        return f"http://127.0.0.1:{self.port}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class ProviderCompat(unittest.TestCase):
    """Item 2: an HTTPS stub for a cloud-shaped target, a local stub for the local ones."""

    def setUp(self):
        self.stub = StubServer()
        self.addCleanup(self.stub.close)
        StubHandler.seen = []
        StubHandler.script = {"content": json.dumps({"schema_version": 1, "answers": {},
                                                     "terms": [], "reader_questions": [],
                                                     "warnings": [], "overall": {}})}
        StubHandler.refuse_parameters = ()
        self.addCleanup(setattr, StubHandler, "refuse_parameters", ())

    def _request(self):
        return providers.CompletionRequest(system="system rules", user="paper text",
                                           model="stub-model", timeout_seconds=10)

    # -- a plain-text answer is parsed, not executed ------------------------------------
    def test_a_local_provider_returns_json(self):
        adapter = providers.OllamaAdapter(base_url=self.stub.base, model="stub-model")
        result = adapter.complete_json(self._request())
        self.assertTrue(result.ok, result.message)
        self.assertEqual(1, result.payload["schema_version"])

    def test_an_openai_shaped_local_provider_returns_json(self):
        adapter = providers.OpenAICompatibleAdapter(base_url=self.stub.base, model="stub-model",
                                                    mode=endpoint_policy.MODE_LOCAL)
        result = adapter.complete_json(self._request())
        self.assertTrue(result.ok, result.message)
        self.assertEqual(1, result.payload["schema_version"])
        # the token cap really is in the request, not only in the validator
        posted = [item for item in StubHandler.seen if item["method"] == "POST"][-1]
        self.assertEqual(adapter.max_output_tokens, posted["payload"]["max_tokens"])

    def test_a_401_gives_a_fixed_code_and_manual_reading_continues(self):
        StubHandler.script = {"status": 401, "body": b'{"error":"invalid api key"}'}
        adapter = providers.OpenAICompatibleAdapter(base_url=self.stub.base, model="stub-model",
                                                    api_key=TEST_KEY,
                                                    mode=endpoint_policy.MODE_LOCAL)
        result = adapter.complete_json(self._request())
        self.assertFalse(result.ok)
        self.assertEqual(secret_guard.E_AUTH_FAILED, result.public_code)
        self.assertNotIn("invalid api key", json.dumps(result.as_dict() if hasattr(
            result, "as_dict") else {"m": result.message}, ensure_ascii=False))

    def test_a_429_carries_retry_after(self):
        StubHandler.script = {"status": 429, "body": b'{"error":"slow down"}'}

        class Handler(StubHandler):
            def _reply(self, status, payload=None, body=None):
                data = body or b"{}"
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Retry-After", "7")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.stub.httpd.RequestHandlerClass = Handler
        adapter = providers.OpenAICompatibleAdapter(base_url=self.stub.base, model="stub-model",
                                                    mode=endpoint_policy.MODE_LOCAL)
        result = adapter.complete_json(self._request())
        self.assertFalse(result.ok)
        self.assertEqual(7.0, result.retry_after_seconds)

    def test_a_secret_in_the_prompt_blocks_the_send(self):
        credential = credentials.Secret(TEST_KEY)
        secret_guard.register(credential, "test")
        adapter = providers.OpenAICompatibleAdapter(base_url=self.stub.base, model="stub-model",
                                                    api_key=TEST_KEY,
                                                    mode=endpoint_policy.MODE_LOCAL)
        request = providers.CompletionRequest(system="rules", user=f"here is the key {TEST_KEY}",
                                              model="stub-model", timeout_seconds=10)
        before = len([item for item in StubHandler.seen if item["method"] == "POST"])
        result = adapter.complete_json(request)
        after = len([item for item in StubHandler.seen if item["method"] == "POST"])
        self.assertFalse(result.ok)
        self.assertEqual(secret_guard.E_SECRET_ECHO_BLOCKED, result.public_code)
        self.assertEqual(before, after, "nothing may reach the wire")


class CapabilityAdaptation(unittest.TestCase):
    """Item 3: drop an optional parameter by name; never drop the token cap."""

    def setUp(self):
        self.stub = StubServer()
        self.addCleanup(self.stub.close)
        StubHandler.seen = []
        StubHandler.script = {"content": json.dumps({"schema_version": 1, "answers": {},
                                                     "terms": [], "reader_questions": [],
                                                     "warnings": [], "overall": {}})}
        self.addCleanup(setattr, StubHandler, "refuse_parameters", ())
        self.logs = []

    def _reset(self):
        """Count only the requests this test makes: the log is shared across tests.

        The handler appends to the class attribute, so the log is cleared in place and read
        back through the same name. Keeping a second reference would let the two drift apart.
        """
        StubHandler.seen.clear()
        return StubHandler.seen

    @property
    def posts(self):
        return StubHandler.seen

    def _adapter(self):
        adapter = providers.OpenAICompatibleAdapter(base_url=self.stub.base, model="stub-model",
                                                    mode=endpoint_policy.MODE_LOCAL)
        adapter.log = self.logs.append
        return adapter

    def _request(self):
        return providers.CompletionRequest(system="s", user="u", model="stub-model",
                                           timeout_seconds=10)

    def test_a_rejected_optional_parameter_is_dropped_and_the_call_succeeds(self):
        StubHandler.refuse_parameters = ("response_format",)
        self._reset()
        adapter = self._adapter()
        # The caller accounts for every transmission. Without a hook the adapter must not
        # re-send, because a re-send it cannot charge for is a free request.
        adapter.on_retransmit = lambda parameter: True
        result = adapter.complete_json(self._request())
        self.assertTrue(result.ok, result.message)
        posts = [item for item in self.posts if item["method"] == "POST"]
        self.assertEqual(2, len(posts), "one refusal, then one adapted retry")
        self.assertIn("response_format", posts[0]["payload"])
        self.assertNotIn("response_format", posts[-1]["payload"])
        # the essentials survive the adaptation
        self.assertIn("max_tokens", posts[-1]["payload"])
        self.assertIn("messages", posts[-1]["payload"])
        self.assertTrue(any(item.get("event") == "parameter_dropped" for item in self.logs
                            if isinstance(item, dict)), self.logs)

    def test_a_re_send_the_caller_cannot_charge_for_is_refused(self):
        """No accounting hook means no second transmission."""
        StubHandler.refuse_parameters = ("response_format",)
        self._reset()
        adapter = self._adapter()
        result = adapter.complete_json(self._request())
        self.assertFalse(result.ok)
        self.assertEqual(secret_guard.E_CAPABILITY_UNSUPPORTED, result.public_code)
        self.assertEqual(1, len([item for item in self.posts if item["method"] == "POST"]))

    def test_a_budget_stop_refuses_the_re_send(self):
        """When the hook says the extra call is not affordable, nothing more is sent."""
        StubHandler.refuse_parameters = ("response_format",)
        self._reset()
        adapter = self._adapter()
        adapter.on_retransmit = lambda parameter: False
        result = adapter.complete_json(self._request())
        self.assertFalse(result.ok)
        self.assertEqual(secret_guard.E_CAPABILITY_UNSUPPORTED, result.public_code)
        self.assertEqual(1, len([item for item in self.posts if item["method"] == "POST"]))

    def test_several_rejected_parameters_are_dropped_one_at_a_time(self):
        """A server may stop on each unknown parameter in turn: each round drops one."""
        StubHandler.refuse_parameters = ("response_format", "temperature")
        self._reset()
        adapter = self._adapter()
        adapter.on_retransmit = lambda parameter: True
        result = adapter.complete_json(self._request())
        self.assertTrue(result.ok, result.message)
        posts = [item for item in self.posts if item["method"] == "POST"]
        self.assertEqual(3, len(posts), "two refusals, then a clean request")
        for name in ("response_format", "temperature"):
            self.assertNotIn(name, posts[-1]["payload"])
        self.assertIn("max_tokens", posts[-1]["payload"])
        self.assertIn("messages", posts[-1]["payload"])

    def test_a_rejected_token_cap_is_fatal_instead_of_unbounded(self):
        StubHandler.refuse_parameters = ("max_tokens",)
        self._reset()
        adapter = self._adapter()
        adapter.on_retransmit = lambda parameter: True
        result = adapter.complete_json(self._request())
        self.assertFalse(result.ok)
        self.assertEqual(secret_guard.E_CAPABILITY_UNSUPPORTED, result.public_code)
        posts = [item for item in self.posts if item["method"] == "POST"]
        self.assertEqual(1, len(posts), "no retry without the cap")
        self.assertIn("max_tokens", posts[0]["payload"])

    def test_an_unrecognised_400_is_reported_not_adapted(self):
        StubHandler.script = {"status": 400, "body": b'{"error":"model does not exist"}'}
        self._reset()
        adapter = self._adapter()
        adapter.on_retransmit = lambda parameter: True
        result = adapter.complete_json(self._request())
        self.assertFalse(result.ok)
        self.assertEqual(1, len([item for item in self.posts if item["method"] == "POST"]))
        self.assertEqual(secret_guard.E_UPSTREAM_ERROR, result.public_code)

    def test_the_adaptation_never_touches_transport_settings(self):
        adapter = self._adapter()
        policy = endpoint_policy.EndpointPolicy.for_settings(self.stub.base,
                                                            endpoint_policy.MODE_LOCAL)
        transport = secure_transport.SecureTransport(policy)
        self.assertFalse(getattr(transport, "insecure", False))
        self.assertIsNone(getattr(transport, "extra_headers", None))
        self.assertFalse(hasattr(adapter, "allow_insecure"))
        self.assertFalse(hasattr(adapter, "insecure_skip_verify"))

    def test_parameter_matching_uses_word_boundaries(self):
        self.assertEqual(("response_format",),
                         secure_transport._named_parameters(b"unknown parameter response_format"))
        self.assertEqual(("format",), secure_transport._named_parameters(b"bad format"))
        self.assertEqual((), secure_transport._named_parameters(b"model not found"))


class ManualReadingUnaffected(unittest.TestCase):
    """Item 4/5 and the project rule: AI failures never take the reader away."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sec-p6-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        from baseline_helpers import fresh_library_v1
        fresh_library_v1(self.tmp)
        self.store = server.Store(self.tmp)

    def test_every_ai_failure_code_still_leaves_the_reader_usable(self):
        paper_id = self.store.state["papers"][0]["id"]
        for code in (secret_guard.E_AUTH_FAILED, secret_guard.E_TLS_FAILED,
                     secret_guard.E_ENDPOINT_BLOCKED, secret_guard.E_DAY_LIMIT,
                     secret_guard.E_PAUSED, secret_guard.E_TOOL_CALL_BLOCKED,
                     secret_guard.E_CAPABILITY_UNSUPPORTED, secret_guard.E_OUTCOME_UNKNOWN):
            with self.subTest(code=code):
                message = secret_guard.message_for(code)
                self.assertTrue(message)
                # the message is fixed text: no paths, no keys, no upstream sentence
                self.assertNotIn("sk-", message)
                self.assertNotIn("C:\\", message)
        # human fields are untouched by any of this
        before = json.dumps(self.store.paper(paper_id)["fields"], ensure_ascii=False)
        self.assertIn(before, json.dumps(self.store.paper(paper_id), ensure_ascii=False))

    def test_the_model_cannot_reach_settings_or_credentials(self):
        paper_id = self.store.state["papers"][0]["id"]
        paper = self.store.paper(paper_id)
        self.assertNotIn("api_key", json.dumps(paper, ensure_ascii=False))
        self.assertNotIn("credential", json.dumps(paper, ensure_ascii=False))

    def test_a_paused_service_still_serves_the_library(self):
        snapshot = self.store.snapshot()
        self.assertIn("papers", snapshot)
        self.assertIn("fields", snapshot["papers"][0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
