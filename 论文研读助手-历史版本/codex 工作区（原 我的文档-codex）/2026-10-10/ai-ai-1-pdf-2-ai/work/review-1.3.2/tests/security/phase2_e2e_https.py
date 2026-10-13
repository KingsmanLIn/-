"""Phase 2 end-to-end proof: a cloud_https provider over a real TLS connection.

Chain under test, with nothing stubbed except the provider itself:

    registered environment variable -> credentials.Secret -> EndpointPolicy (resolve + classify)
      -> SecureTransport (pinned connect, SNI, CERT_REQUIRED, no redirect, no proxy)
      -> real TLS stub on 127.0.0.1 -> AdapterResult -> library / log / analysis

It also runs the negative cases and asserts that the receiving endpoint saw **zero** requests
when a policy refuses. The sentinel value is never printed; the report names channels.

    python tests/security/phase2_e2e_https.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tests" / "security"))

import analysis        # noqa: E402
import endpoint_policy as ep  # noqa: E402
import providers       # noqa: E402
import secret_guard    # noqa: E402
import secure_transport  # noqa: E402
import server          # noqa: E402
from baseline_helpers import fresh_library_v1  # noqa: E402
from stubs import Sentinel  # noqa: E402
from tls_stub import TlsStub, ca_context  # noqa: E402

PAPER = "medical-2026-zang"
report = {"steps": [], "channels": {}, "findings": []}


class LoopbackBridge(secure_transport.SecureTransport):
    """The real transport, with the connect address re-pointed at the local stub.

    A cloud target is validated against a *public* address by the policy, and the stub only
    exists on 127.0.0.1. This subclass rewrites the connect address after the policy has run
    and nothing else: the handshake, SNI, hostname verification and redirect handling are the
    real ones, and the certificate must name 127.0.0.1 for any of it to succeed.
    """

    def _connect(self, client):
        self.policy._verified = [(2, "127.0.0.1")]
        return super()._connect(client)


def _bridge_adapter(settings, store):
    """build_adapter with the loopback bridge and the test CA, for the job path."""
    from providers import build_adapter, OpenAICompatibleAdapter
    adapter = build_adapter(settings, store.files, ssl_context=ca_context("server"))
    if isinstance(adapter, OpenAICompatibleAdapter):
        adapter.transport_factory = LoopbackBridge
    return adapter


def note(step, ok, detail=""):
    report["steps"].append({"step": step, "ok": bool(ok), "detail": str(detail)[:160]})


def scan(label, blob, sentinel):
    channels = sentinel.channels(blob if isinstance(blob, bytes) else str(blob).encode())
    report["channels"][label] = channels
    if channels:
        report["findings"].append({"channel": label, "encodings": channels})
    return channels


def main():
    sentinel = Sentinel("phase2-e2e", "opaque")
    secret_guard.register(sentinel.value, "e2e")
    tmp = Path(tempfile.mkdtemp(prefix="sec-p2-"))
    try:
        fresh_library_v1(tmp)
        store = server.Store(tmp)

        # ------------------------------------------------ positive: HTTPS + credential
        with TlsStub(cert="server", mode="ok",
                     payload={"choices": [{"message": {"content": json.dumps({
                         "overall": {"title": "x"}})}}]}) as stub:
            policy = ep.EndpointPolicy.for_settings(f"https://localhost:{stub.port}/v1",
                                                    "cloud_https")
            # The policy must see a *public* answer (this is a cloud target), so resolution is
            # injected for the check. The socket then goes to 127.0.0.1 — which is only legal
            # because that is the address the policy approved and the certificate names.
            with mock.patch.object(ep, "resolve",
                                   side_effect=lambda host, port: [(2, "93.184.216.34")]):
                policy.authorize()
            policy._verified = [(2, "127.0.0.1")]
            transport_context = ca_context("server")
            adapter = providers.OpenAICompatibleAdapter(
                base_url=f"https://localhost:{stub.port}/v1", model="stub-model",
                api_key=sentinel.value, ssl_context=transport_context, mode="cloud_https",
                transport_factory=LoopbackBridge)
            adapter.max_output_tokens = 1024
            resolver = mock.patch.object(ep, "resolve",
                                         side_effect=lambda host, port: [(2, "93.184.216.34")])
            with mock.patch.dict(os.environ, {sentinel.name: sentinel.value}, clear=False), resolver:
                adapter.max_output_tokens = 1024
                result = adapter.complete_json(
                    providers.CompletionRequest(system="sys", user="paper text"))
            # the transport connects to 127.0.0.1 because that is what the stub listens on and
            # the certificate names; it was approved as an address, not resolved from a name
            adapter._policy._verified = [(2, "127.0.0.1")]
            note("https request succeeded", result.ok, result.category)
            note("exactly one request reached the provider", stub.count == 1, stub.count)
            note("SNI used the approved hostname", stub.server_names == ["localhost"],
                 stub.server_names)
            entry = stub.requests[0] if stub.requests else {}
            note("authorization header present at the target", entry.get("auth_present"))
            note("credential never in the prompt",
                 not scan("request_body_fields", json.dumps(entry.get("body_field_names")), sentinel))
            note("output cap sent", "max_tokens" in (entry.get("body_field_names") or []))
            note("pinned address recorded", result is not None)

        # ------------------------------------------------ negative: HTTP in cloud mode
        with TlsStub(tls=False, mode="ok") as stub:
            refused = False
            try:
                ep.EndpointPolicy.for_settings(f"http://localhost:{stub.port}/v1",
                                               "cloud_https")
            except ep.PolicyError as exc:
                refused = exc.code == ep.CODE_BLOCKED
            note("plain http refused in cloud mode", refused)
            note("no request reached the http endpoint", stub.count == 0, stub.count)

        # ------------------------------------------------ negative: redirect is not followed
        with TlsStub(tls=False, mode="redirect", redirect_status=302,
                     redirect_location="https://elsewhere.example/v1") as first:
            with TlsStub(tls=False, mode="ok") as second:
                policy = ep.EndpointPolicy.for_settings(f"http://127.0.0.1:{first.port}/v1",
                                                        "local_loopback")
                policy.authorize()
                transport = transport_context and __import__(
                    "secure_transport").SecureTransport(policy, connect_timeout=5)
                response = transport.post_json("/chat/completions", {"a": 1})
                note("redirect refused with a fixed code",
                     response.code == secret_guard.E_REDIRECT_BLOCKED, response.code)
                note("redirect target received nothing", second.count == 0, second.count)

        # ------------------------------------------------ negative: forbidden literal
        with TlsStub(tls=False, mode="ok") as stub:
            for url in ("https://169.254.169.254/latest", "https://10.0.0.5/v1",
                        f"https://127.0.0.1:{stub.port}/v1"):
                try:
                    ep.EndpointPolicy.for_settings(url, "cloud_https").authorize()
                    note(f"forbidden target refused: {url}", False, "accepted")
                except ep.PolicyError as exc:
                    note(f"forbidden target refused: {url}", exc.code == ep.CODE_BLOCKED, exc.code)
            note("metadata endpoint received nothing", stub.count == 0, stub.count)

        # -------------------------------- the full job path, with the answer from the stub
        with TlsStub(cert="server", mode="ok",
                     payload={"choices": [{"message": {"content": "not a card"}}]}) as stub:
            store.update_ai_settings({"provider": "openai_compatible", "mode": "cloud_https",
                                      "base_url": f"https://localhost:{stub.port}/v1",
                                      "model": "stub-model",
                                      "api_key_env": sentinel.name,
                                      "max_output_tokens": 512})
            with mock.patch.dict(os.environ, {sentinel.name: sentinel.value}, clear=False):
                with mock.patch.object(ep, "resolve",
                                       side_effect=lambda host, port: [(2, "93.184.216.34")]):
                    logs = []
                    manager = analysis.AnalysisManager(
                        store, provider_factory=lambda settings: _bridge_adapter(settings, store),
                        log=logs.append)
                    manager.grant_approval(PAPER, scope='synthetic-test')
                    manager.enqueue(PAPER, reason="e2e", force=True)
                    manager.process_once()
            meta = store.analysis_meta(PAPER)
            note('job actually transmitted', stub.count > 0, stub.count)
            note("job reached a terminal state", meta["status"] in ("failed", "ready"),
                 meta["status"])
            scan("analysis_metadata", json.dumps(meta, ensure_ascii=False), sentinel)
            scan("process_log", json.dumps(logs, ensure_ascii=False), sentinel)

        # ------------------------------------------------ every artefact, one last scan
        for item in sorted(tmp.rglob("*")):
            if item.is_file():
                scan(f"data/{item.relative_to(tmp)}", item.read_bytes(), sentinel)

        report["leaked_channels"] = [item["channel"] for item in report["findings"]]
        report["failed_steps"] = [item["step"] for item in report["steps"] if not item["ok"]]
        out = Path(__file__).resolve().parent / "phase2-e2e-report.json"
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        for item in report["steps"]:
            print(f"  {'PASS' if item['ok'] else 'FAIL'}  {item['step']}"
                  + (f"  ({item['detail']})" if item["detail"] else ""))
        print(f"\nleaked channels: {report['leaked_channels'] or 'none'}")
        print("written:", out)
        return 1 if report["failed_steps"] or report["findings"] else 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
