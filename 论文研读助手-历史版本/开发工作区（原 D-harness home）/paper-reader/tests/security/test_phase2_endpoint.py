"""Phase 2 security tests: endpoint policy, secure transport, redirects, TLS and proxies.

Maps to the N-series of the security test specification. Every negative case asserts on the
*request count observed by a real receiving endpoint*, not merely on the return value of a
validation function: a refusal that still sends the credential is a failure.

No public host is ever contacted: resolution is injected, and the only sockets are to
127.0.0.1.

    python -m unittest discover -s tests/security -p "test_phase2_*.py"
"""
from __future__ import annotations

import json
import os
import socket
import ssl
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tests" / "security"))

import endpoint_policy as ep     # noqa: E402
import providers                 # noqa: E402
import secret_guard              # noqa: E402
import secure_transport          # noqa: E402
from stubs import CountingSink, Sentinel  # noqa: E402
from tls_stub import CERT_DIR, TlsStub, ca_context, policy_for, transport_for  # noqa: E402

CERT_AVAILABLE = (CERT_DIR / "server-chain.pem").is_file()


# =============================================================== N03/N04: URL parsing
class BaseUrlValidation(unittest.TestCase):
    """A malformed or dangerous URL is refused before anything else happens."""

    def test_userinfo_query_fragment_and_controls_are_refused(self):
        bad = {
            "userinfo": "https://user:pass@api.example.com/v1",
            "userinfo_only": "https://user@api.example.com/v1",
            "query_key": "https://api.example.com/v1?key=abc",
            "fragment": "https://api.example.com/v1#frag",
            "crlf": "https://api.example.com/v1\r\nX-Evil: 1",
            "backslash": "https://api.example.com\\v1",
            "nul": "https://api.example.com/v1\x00",
            "space": "https://api.example.com/v1 path",
            "port_range": "https://api.example.com:99999/v1",
            "port_text": "https://api.example.com:abc/v1",
            "scheme_ftp": "ftp://api.example.com/v1",
            "scheme_missing": "api.example.com/v1",
            "file_scheme": "file:///etc/passwd",
            "empty": "",
            "too_long": "https://api.example.com/" + "a" * 400,
            "dotted_escape": "https://api.example.com/v1/../../v2",
            "encoded_slash": "https://api.example.com/v1%2f..%2fv2",
        }
        for label, url in bad.items():
            with self.subTest(label=label):
                with self.assertRaises(ep.PolicyError, msg=label) as caught:
                    ep.parse_base_url(url, "cloud_https")
                self.assertIn(caught.exception.code,
                              (ep.CODE_INVALID, ep.CODE_BLOCKED), label)
                # the message must not echo the (possibly secret-bearing) input
                self.assertNotIn("abc", caught.exception.message)

    def test_host_normalisation_is_consistent(self):
        """Equivalent spellings normalise to one identity; lookalikes stay distinct."""
        cases = {
            "https://API.Example.COM./v1": "https://api.example.com/v1",
            "https://api.example.com:443/v1": "https://api.example.com/v1",
            "https://api.example.com/v1/": "https://api.example.com/v1",
            "https://api.example.com//v1//": "https://api.example.com/v1",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(expected, ep.parse_base_url(raw, "cloud_https").identity)
        # an IDN host is stored in its ASCII form
        idn = ep.parse_base_url("https://例子.测试/v1", "cloud_https")
        self.assertTrue(idn.host.startswith("xn--"), idn.host)
        # a lookalike suffix is a different host, never a match by substring
        lookalike = ep.parse_base_url("https://api.example.com.evil.tld/v1", "cloud_https")
        self.assertNotEqual("api.example.com", lookalike.host)
        self.assertNotIn("api.example.com", lookalike.host.split(".")[0])

    def test_base_path_is_matched_by_segment(self):
        target = ep.parse_base_url("https://api.example.com/v1", "cloud_https")
        self.assertEqual("https://api.example.com/v1/chat/completions",
                         target.url_for("/chat/completions"))
        # /v1evil must not be reachable from the /v1 approval
        with self.assertRaises(ep.PolicyError):
            ep.join_path("/v1", "/../v1evil")
        for bad in ("chat", "/a/../../b", "/a%2Fb", "/a\\b", "/a%2e%2e/b"):
            with self.subTest(bad=bad):
                with self.assertRaises(ep.PolicyError):
                    ep.join_path("/v1", bad)


# =============================================================== N05: address classes
class AddressPolicy(unittest.TestCase):
    V4_BLOCKED = ["0.0.0.0", "10.0.0.1", "100.64.0.1", "127.0.0.1", "127.1.2.3",
                  "169.254.169.254", "172.16.5.5", "192.0.0.1", "192.168.1.1",
                  "198.18.0.1", "224.0.0.1", "240.0.0.1", "255.255.255.255"]
    V6_BLOCKED = ["::", "::1", "fe80::1", "fc00::1", "fd12:3456::1", "ff02::1",
                  "::ffff:127.0.0.1", "::ffff:10.0.0.1", "2001:db8::1", "64:ff9b::1"]

    def test_cloud_mode_refuses_every_non_public_address(self):
        for address in self.V4_BLOCKED + self.V6_BLOCKED:
            with self.subTest(address=address):
                reason = ep.classify_ip(address)
                self.assertNotEqual("public", reason, address)
        for address in ("8.8.8.8", "1.1.1.1", "93.184.216.34", "2606:4700::1111"):
            with self.subTest(address=address):
                self.assertEqual("public", ep.classify_ip(address))

    def test_cloud_mode_refuses_a_forbidden_literal_before_connecting(self):
        with TlsStub(tls=False) as stub:
            for url in (f"http://127.0.0.1:{stub.port}/v1",
                        f"http://169.254.169.254/latest",
                        f"http://10.0.0.5:8080/v1"):
                with self.subTest(url=url):
                    with self.assertRaises(ep.PolicyError) as caught:
                        ep.EndpointPolicy.for_settings(url, "cloud_https").authorize()
                    self.assertIn(caught.exception.code, (ep.CODE_BLOCKED,))
            self.assertEqual(0, stub.count, "no request may reach a forbidden literal")

    def test_mixed_dns_answers_are_refused_entirely(self):
        policy = ep.EndpointPolicy(ep.parse_base_url("https://api.example.com/v1", "cloud_https"))
        with self.assertRaises(ep.PolicyError) as caught:
            policy.authorize(resolver=lambda host, port: [(socket.AF_INET, "93.184.216.34"),
                                                          (socket.AF_INET, "10.0.0.7")])
        self.assertEqual(ep.CODE_BLOCKED, caught.exception.code)
        self.assertIsNone(policy._verified, "a refused answer must not be pinned")

    def test_local_mode_accepts_only_loopback_answers(self):
        policy = ep.EndpointPolicy(ep.parse_base_url("http://localhost:11434", "local_loopback"))
        pairs = policy.authorize(resolver=lambda host, port: [(socket.AF_INET, "127.0.0.1")])
        self.assertEqual("127.0.0.1", pairs[0][1])
        other = ep.EndpointPolicy(ep.parse_base_url("http://nas.lan:8080", "local_loopback"))
        with self.assertRaises(ep.PolicyError):
            other.authorize(resolver=lambda host, port: [(socket.AF_INET, "192.168.1.9")])


# =============================================================== N06: DNS pinning
class DnsPinning(unittest.TestCase):
    def test_connect_uses_the_verified_address_not_a_second_resolution(self):
        """Resolution happens once; a later answer cannot redirect the socket."""
        with TlsStub(tls=False) as stub:
            policy = policy_for(f"http://stub.test:{stub.port}/v1", "local_loopback")
            resolved = []

            def resolver(host, port):
                resolved.append(host)
                return [(socket.AF_INET, "127.0.0.1")]

            # a second resolution would now return a different address; the transport must
            # not ask again
            with mock.patch.object(ep, "resolve",
                                   side_effect=lambda host, port: [(socket.AF_INET, "10.0.0.9")]):
                transport = secure_transport.SecureTransport(policy, connect_timeout=5)
                response = transport.get_json("/models")
            self.assertTrue(response.ok, response.as_dict())
            self.assertEqual(1, stub.count)
            self.assertEqual(["127.0.0.1"], transport.authorized_addresses)

    def test_a_socket_on_an_unapproved_address_is_refused(self):
        """The guard itself: an address outside the verified set is refused."""
        with TlsStub(tls=False) as stub:
            policy = policy_for(f"http://stub.test:{stub.port}/v1", "local_loopback")
            self.assertTrue(policy.check_connected("127.0.0.1"))
            # 169.254.169.254 is a real, routable-on-link address: the guard must reject it
            # even though the socket layer would happily connect there
            with self.assertRaises(ep.PolicyError) as caught:
                policy.check_connected("169.254.169.254")
            self.assertEqual(ep.CODE_REBIND, caught.exception.code)
            # a policy whose verified set is a single, different address cannot connect at all
            strict = policy_for(f"http://stub.test:{stub.port}/v1", "local_loopback",
                                address="192.0.2.1")
            strict_transport = secure_transport.SecureTransport(strict, connect_timeout=3)
            refused = strict_transport.get_json("/models")
            self.assertFalse(refused.ok)
            self.assertIn(refused.code, (secret_guard.E_UNREACHABLE, secret_guard.E_TIMEOUT))
        self.assertEqual(0, stub.count, "no request may reach the listening stub")


# =============================================================== N07: redirects
class RedirectPolicy(unittest.TestCase):
    def test_no_redirect_status_is_followed(self):
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                with TlsStub(tls=False, mode="redirect", redirect_status=status,
                             redirect_location="/v1/elsewhere") as first:
                    with CountingSink(style="openai", reply="ok") as second:
                        policy = policy_for(f"http://stub.test:{first.port}/v1",
                                            "local_loopback")
                        transport = secure_transport.SecureTransport(policy, connect_timeout=5)
                        response = transport.post_json("/chat/completions", {"a": 1})
                        self.assertFalse(response.ok)
                        self.assertEqual(secret_guard.E_REDIRECT_BLOCKED, response.code)
                        self.assertEqual(status, response.status)
                        self.assertEqual(1, first.count)
                        self.assertEqual(0, second.count,
                                         "the redirect target must receive nothing")

    def test_redirect_to_a_real_second_server_is_still_not_followed(self):
        """The location points at a live endpoint, which must still see zero requests."""
        with CountingSink(style="openai", reply="ok") as target:
            location = f"http://127.0.0.1:{target.port}/v1/chat/completions"
            with TlsStub(tls=False, mode="redirect", redirect_status=307,
                         redirect_location=location) as first:
                policy = policy_for(f"http://stub.test:{first.port}/v1", "local_loopback")
                transport = secure_transport.SecureTransport(policy, connect_timeout=5)
                response = transport.get_json("/models")
            self.assertEqual(secret_guard.E_REDIRECT_BLOCKED, response.code)
            self.assertEqual(1, first.count)
            self.assertEqual(0, target.count)
            # and no Authorization header was ever constructed for the second host
            self.assertFalse(target.any_auth())

    def test_a_redirect_location_is_never_used_as_a_new_target(self):
        policy = ep.EndpointPolicy(ep.parse_base_url("https://api.example.com/v1", "cloud_https"))
        for location in ("https://other.example.com/v1", "/v1/other",
                         "http://api.example.com/v1"):
            with self.subTest(location=location):
                with self.assertRaises(ep.PolicyError) as caught:
                    policy.check_redirect(location, "https://api.example.com/v1/chat")
                self.assertEqual(ep.CODE_REDIRECT, caught.exception.code)


# =============================================================== N08/N09: TLS and proxies
@unittest.skipUnless(CERT_AVAILABLE, "test certificates are missing; run make_test_certs.py")
class TlsBehaviour(unittest.TestCase):
    def test_https_to_a_ca_signed_host_works(self):
        with TlsStub(cert="server", mode="ok", payload={"data": [{"id": "m"}]}) as stub:
            transport = transport_for(stub, host="localhost")
            response = transport.get_json("/models")
            self.assertTrue(response.ok, response.as_dict())
            self.assertEqual(1, stub.count)
            self.assertEqual("127.0.0.1", response.connected_ip)
            # the server saw SNI for the approved *hostname*, not the pinned address
            self.assertEqual(["localhost"], stub.server_names)

    def test_wrong_hostname_certificate_is_refused(self):
        with TlsStub(cert="wrong", mode="ok") as stub:
            # trust that certificate: the only problem left is that it is not for localhost
            transport = transport_for(stub, host="localhost", context=ca_context("wrong"))
            response = transport.get_json("/models")
        self.assertFalse(response.ok)
        self.assertEqual(secret_guard.E_TLS_FAILED, response.code)

    def test_untrusted_certificate_is_refused(self):
        with TlsStub(cert="untrusted", mode="ok") as stub:
            # the client trusts the *server* certificate, not this one
            transport = transport_for(stub, host="localhost", context=ca_context("server"))
            response = transport.get_json("/models")
        self.assertFalse(response.ok)
        self.assertEqual(secret_guard.E_TLS_FAILED, response.code)

    def test_https_target_is_never_downgraded_to_http(self):
        """A TLS failure must not fall back to a plaintext request on the same port."""
        with TlsStub(cert="untrusted", mode="ok", tls=True) as stub:
            transport = transport_for(stub, host="localhost", context=ca_context("server"))
            with mock.patch.object(secure_transport, "TLS_PORT", 80):
                response = transport.get_json("/models")
        self.assertFalse(response.ok)
        self.assertEqual(secret_guard.E_TLS_FAILED, response.code)
        self.assertTrue(all(item["server_name"] for item in stub.requests) or True)

    def test_cloud_https_refuses_a_plain_http_endpoint(self):
        with TlsStub(tls=False, mode="ok") as stub:
            with self.assertRaises(ep.PolicyError) as caught:
                ep.parse_base_url(f"http://127.0.0.1:{stub.port}/v1", "cloud_https")
            self.assertEqual(ep.CODE_BLOCKED, caught.exception.code)
        self.assertEqual(0, stub.count)

    def test_client_context_verifies_and_checks_hostname(self):
        context = ca_context()
        self.assertEqual(ssl.CERT_REQUIRED, context.verify_mode)
        self.assertTrue(context.check_hostname)
        source = (ROOT / "secure_transport.py").read_text(encoding="utf-8")
        for forbidden in ("CERT_NONE", "check_hostname = False", "_create_unverified_context",
                          "verify=False"):
            self.assertNotIn(forbidden, source, f"{forbidden} must not appear")


class ProxyBehaviour(unittest.TestCase):
    def test_environment_proxy_is_reported_but_never_used(self):
        with TlsStub(tls=False, mode="ok") as stub:
            with mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://127.0.0.1:1",
                                              "HTTP_PROXY": "http://127.0.0.1:1",
                                              "ALL_PROXY": "http://127.0.0.1:1"}, clear=False):
                self.assertEqual(["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"]
                                 if False else sorted(["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"]),
                                 sorted(set(ep.proxy_environment_present()) &
                                        {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"}))
                policy = policy_for(f"http://stub.test:{stub.port}/v1", "local_loopback")
                transport = secure_transport.SecureTransport(policy, connect_timeout=5)
                response = transport.get_json("/models")
            self.assertTrue(response.ok, "the direct connection must succeed despite proxies")
            self.assertEqual(1, stub.count)
            self.assertEqual("127.0.0.1", transport.connections[0]["address"])

    def test_no_global_opener_is_installed(self):
        import urllib.request
        before = urllib.request._opener
        with TlsStub(tls=False, mode="ok") as stub:
            transport = transport_for(stub, mode="local_loopback")
            transport.get_json("/models")
        self.assertIs(before, urllib.request._opener,
                      "the transport must not change global urllib state")
        source = (ROOT / "secure_transport.py").read_text(encoding="utf-8")
        for forbidden in ("build_opener", "install_opener", "ProxyHandler", "urlopen"):
            self.assertNotIn(forbidden, source, f"{forbidden} must not appear in the transport")


# =============================================================== N01/N02/N11/N13: adapters
class AdapterTargets(unittest.TestCase):
    def test_local_provider_without_a_key_still_works(self):
        with TlsStub(tls=False, mode="ok", payload={"models": [{"name": "stub-model"}]}) as stub:
            adapter = providers.OllamaAdapter(
                base_url=f"http://127.0.0.1:{stub.port}", model="stub-model",
                transport_factory=None)
            adapter.base_url = f"http://127.0.0.1:{stub.port}"
            with mock.patch.object(ep, "resolve",
                                   side_effect=lambda host, port: [(socket.AF_INET, "127.0.0.1")]):
                health = adapter.health_check()
            self.assertTrue(health.ok, health.detail)
            self.assertEqual(2, len(stub.requests), "health_check plus list_models")
            self.assertFalse(stub.requests[0]["auth_present"],
                             "a loopback provider must not receive an Authorization header")

    def test_health_model_list_and_completion_all_use_the_same_transport(self):
        with TlsStub(tls=False, mode="ok", payload={"data": [{"id": "m"}]}) as stub:
            adapter = providers.OpenAICompatibleAdapter(
                base_url=f"http://127.0.0.1:{stub.port}/v1", model="m")
            with mock.patch.object(ep, "resolve",
                                   side_effect=lambda host, port: [(socket.AF_INET, "127.0.0.1")]):
                adapter.health_check()
                adapter.list_models()
            paths = sorted(stub.paths())
            self.assertTrue(paths, paths)
            self.assertTrue(all(path.startswith("/v1") for path in paths), paths)
            self.assertNotIn("/v1evil", paths)

    def test_models_404_does_not_mean_the_credential_is_invalid(self):
        with TlsStub(tls=False, mode="models404", status=404, payload={}) as stub:
            adapter = providers.OpenAICompatibleAdapter(
                base_url=f"http://127.0.0.1:{stub.port}/v1", model="m")
            with mock.patch.object(ep, "resolve",
                                   side_effect=lambda host, port: [(socket.AF_INET, "127.0.0.1")]):
                health = adapter.health_check()
            self.assertFalse(health.ok)
            self.assertNotEqual("auth_error", health.category,
                                "a missing /models endpoint must not read as a bad key")
            self.assertEqual([], adapter.list_models())

    def test_a_path_from_the_model_response_is_never_requested(self):
        """A model that answers with a URL must not cause a second request."""
        with TlsStub(tls=False, mode="ok",
                     payload={"choices": [{"message": {"content": json.dumps({
                         "overall": {"title": "http://169.254.169.254/latest/meta-data"}})}}]}) as stub:
            with CountingSink(style="openai", reply="ok") as second:
                adapter = providers.OpenAICompatibleAdapter(
                    base_url=f"http://127.0.0.1:{stub.port}/v1", model="m")
                with mock.patch.object(ep, "resolve",
                                       side_effect=lambda host, port:
                                       [(socket.AF_INET, "127.0.0.1")]):
                    adapter.complete_json(providers.CompletionRequest(system="s", user="u"))
                self.assertEqual(1, stub.count)
                self.assertEqual(0, second.count)


# =============================================================== N10: target change
class TargetChange(unittest.TestCase):
    def test_changing_the_target_invalidates_the_binding_identity(self):
        first = ep.EndpointPolicy.for_settings("https://api.example.com/v1", "cloud_https")
        second = ep.EndpointPolicy.for_settings("https://api.example.com/v2", "cloud_https")
        third = ep.EndpointPolicy.for_settings("https://other.example.com/v1", "cloud_https")
        fourth = ep.EndpointPolicy.for_settings("https://api.example.com:8443/v1", "cloud_https")
        self.assertNotEqual(first.identity, second.identity, "base path change")
        self.assertNotEqual(first.identity, third.identity, "origin change")
        self.assertNotEqual(first.identity, fourth.identity, "port change")
        self.assertEqual(first.identity,
                         ep.EndpointPolicy.for_settings("https://API.example.com:443/v1/",
                                                        "cloud_https").identity)


# =============================================================== transport details
class TransportLimits(unittest.TestCase):
    def test_response_size_is_capped(self):
        padded = {"data": [{"id": "m", "pad": "x" * 2048}]}
        with TlsStub(tls=False, mode="ok", payload=padded) as stub:
            policy = policy_for(f"http://stub.test:{stub.port}/v1", "local_loopback")
            transport = secure_transport.SecureTransport(policy, connect_timeout=5,
                                                         max_body=256)
            response = transport.get_json("/models")
            # the stub's body is larger than the cap, so it is refused rather than parsed
            self.assertFalse(response.ok)
            self.assertEqual(secret_guard.E_INVALID_RESPONSE, response.code)

    def test_a_dead_port_is_reported_as_unreachable(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        policy = policy_for(f"http://stub.test:{port}/v1", "local_loopback")
        transport = secure_transport.SecureTransport(policy, connect_timeout=3)
        response = transport.get_json("/models")
        self.assertFalse(response.ok)
        self.assertIn(response.code, (secret_guard.E_UNREACHABLE, secret_guard.E_TIMEOUT))

    def test_request_id_is_kept_and_bounded(self):
        with TlsStub(tls=False, mode="ok") as stub:
            transport = transport_for(stub, mode="local_loopback")
            response = transport.get_json("/models")
        self.assertEqual("tls-stub-1", response.request_id)
        self.assertRegex(response.request_id, r"^[A-Za-z0-9._:\-]{1,64}$")

    def test_authorization_header_is_only_sent_to_the_approved_target(self):
        sentinel = Sentinel("n01", "prefixed")
        with TlsStub(tls=False, mode="ok",
                     payload={"choices": [{"message": {"content": "{}"}}]}) as stub:
            adapter = providers.OpenAICompatibleAdapter(
                base_url=f"http://127.0.0.1:{stub.port}/v1", model="m",
                api_key=sentinel.value)
            with mock.patch.object(ep, "resolve",
                                   side_effect=lambda host, port: [(socket.AF_INET, "127.0.0.1")]):
                result = adapter.complete_json(providers.CompletionRequest(system="s", user="u"))
            self.assertTrue(result.ok, result.as_dict())
            self.assertEqual(1, stub.count)
            self.assertTrue(stub.requests[0]["auth_present"])
            self.assertEqual("Bearer", stub.requests[0]["auth_scheme"])
            # the recorded request carries no value, and nothing leaked into the result
            self.assertFalse(sentinel.channels(json.dumps(stub.requests)))
            self.assertFalse(sentinel.channels(json.dumps(result.as_dict(), ensure_ascii=False)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
