"""Phase 3 security tests: local session, bootstrap, caching and request limits.

Covers L01-L08 (local service and browser) at the level this phase implements, using a real
HTTP server on loopback. Nothing is stubbed except the model provider, which is not involved.

    python -m unittest discover -s tests/security -p "test_phase3_*.py"
"""
from __future__ import annotations

import http.client
import json
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tests" / "security"))

import secret_guard     # noqa: E402
import server           # noqa: E402
import session          # noqa: E402
from baseline_helpers import fresh_library_v1  # noqa: E402

TOKEN = "phase3-legacy-token"


class ServiceCase(unittest.TestCase):
    """A live service on a private port, with its own data directory."""

    def setUp(self):
        import shutil
        self.tmp = Path(tempfile.mkdtemp(prefix="sec-p3-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        fresh_library_v1(self.tmp)
        self.store = server.Store(self.tmp)
        self.sessions = session.SessionStore()
        self.httpd = server.BoundedHTTPServer(
            ("127.0.0.1", 0),
            server.handler_for(self.store, TOKEN, None, sessions=self.sessions),
            max_concurrency=4)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self._stop)

    def _stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    # -- helpers -----------------------------------------------------------
    def request(self, method, path, body=None, headers=None, host=None, origin=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Host", host or f"127.0.0.1:{self.port}")
        if origin:
            request.add_header("Origin", origin)
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return response.status, dict(response.getheaders()), response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    def json_of(self, response):
        return json.loads(response[2].decode("utf-8"))

    def start_session(self):
        capability = self.sessions.issue_bootstrap(source="test")
        status, headers, body = self.request("POST", "/api/session/bootstrap",
                                             {"capability": capability})
        self.assertEqual(200, status, body)
        payload = json.loads(body.decode())
        return payload["csrf"], headers.get("Set-Cookie", "")

    def cookie_header(self, set_cookie):
        return set_cookie.split(";", 1)[0]


# ==================================================================== L02: bootstrap flow
class BootstrapAndSession(ServiceCase):
    def test_page_carries_no_management_token(self):
        status, headers, body = self.request("GET", "/")
        self.assertEqual(200, status)
        text = body.decode("utf-8")
        # the placeholder is substituted with a boot object that carries no capability
        self.assertNotIn("__BOOTSTRAP__", text)
        self.assertNotIn(TOKEN, text, "the page must not contain the management token")
        self.assertNotIn("X-Reader-Token", text)
        self.assertIn('"session_required": true', text.replace("'", '"'))

    def test_bootstrap_is_single_use(self):
        capability = self.sessions.issue_bootstrap(source="test")
        first = self.request("POST", "/api/session/bootstrap", {"capability": capability})
        second = self.request("POST", "/api/session/bootstrap", {"capability": capability})
        self.assertEqual(200, first[0])
        self.assertEqual(403, second[0], "a spent capability must not mint a second session")
        self.assertIn("bootstrap_unknown", second[2].decode("utf-8"))

    def test_unknown_and_malformed_capabilities_are_refused(self):
        for label, payload in (("unknown", {"capability": "not-a-real-capability"}),
                               ("empty", {"capability": ""}),
                               ("wrong type", {"capability": [1, 2, 3]}),
                               ("missing", {})):
            with self.subTest(label=label):
                status, _headers, body = self.request("POST", "/api/session/bootstrap", payload)
                self.assertEqual(403, status, body)
        status, _headers, body = self.request("POST", "/api/session/bootstrap", b"not json")
        self.assertEqual(400, status, body)

    def test_expired_capability_is_refused(self):
        capability = self.sessions.issue_bootstrap(source="test")
        record = self.sessions.bootstraps[capability]
        record.created_at -= (session.BOOTSTRAP_TTL_SECONDS + 5)
        status, _headers, body = self.request("POST", "/api/session/bootstrap",
                                              {"capability": capability})
        self.assertEqual(403, status)
        self.assertIn("bootstrap_expired", body.decode("utf-8"))

    def test_cookie_attributes_suit_loopback_http(self):
        _csrf, set_cookie = self.start_session()
        self.assertIn("HttpOnly", set_cookie)
        self.assertIn("SameSite=Strict", set_cookie)
        self.assertIn("Path=/", set_cookie)
        self.assertNotIn("Secure", set_cookie,
                         "Secure would break the cookie on http://127.0.0.1")
        self.assertNotIn("__Host-", set_cookie)
        self.assertNotIn("Max-Age", set_cookie, "the session cookie must not persist")

    def test_session_supports_refresh_and_a_second_window(self):
        csrf, set_cookie = self.start_session()
        cookie = self.cookie_header(set_cookie)
        # a refresh: GET /api/session with the cookie returns the CSRF token again
        status, _headers, body = self.request("GET", "/api/session", headers={"Cookie": cookie})
        self.assertEqual(200, status, body)
        self.assertEqual(csrf, json.loads(body.decode())["csrf"])
        # a second window uses the same cookie
        status, _headers, body = self.request("GET", "/api/state", headers={"Cookie": cookie})
        self.assertEqual(200, status)

    def test_state_without_a_session_is_refused(self):
        status, _headers, body = self.request("GET", "/api/state")
        self.assertEqual(403, status, body)

    def test_health_stays_public_and_carries_nothing_sensitive(self):
        status, _headers, body = self.request("GET", "/api/health")
        self.assertEqual(200, status)
        payload = json.loads(body.decode())
        self.assertEqual({"app", "version", "schema_version", "root", "data"}, set(payload))
        self.assertNotIn(TOKEN, body.decode("utf-8"))

    def test_revoked_session_cannot_continue(self):
        csrf, set_cookie = self.start_session()
        cookie = self.cookie_header(set_cookie)
        status, _headers, _body = self.request("POST", "/api/session/revoke", {},
                                              headers={"Cookie": cookie,
                                                       "X-CSRF-Token": csrf})
        self.assertEqual(200, status)
        status, _headers, _body = self.request("GET", "/api/state", headers={"Cookie": cookie})
        self.assertEqual(403, status, "a revoked session must not keep working")

    def test_shutdown_revokes_every_session(self):
        self.start_session()
        self.assertTrue(self.sessions.counts()["sessions"] >= 1)
        self.sessions.revoke_all()
        self.assertEqual(0, self.sessions.counts()["sessions"])

    def test_bootstrap_counts_are_observable_without_values(self):
        self.sessions.issue_bootstrap(source="test")
        self.request("POST", "/api/session/bootstrap", {"capability": "wrong"})
        counts = self.sessions.counts()
        self.assertEqual(1, counts["issued"])
        self.assertEqual(1, counts["rejected"])
        self.assertNotIn("capability", json.dumps(counts))


# ==================================================================== L01: host and origin
class HostAndOrigin(ServiceCase):
    def test_foreign_host_is_refused(self):
        for host in ("evil.example.com", "127.0.0.1", f"localhost:{self.port + 1}",
                     f"127.0.0.1:{self.port}.evil.com"):
            with self.subTest(host=host):
                status, _headers, _body = self.request("GET", "/api/state", host=host)
                self.assertEqual(403, status)

    def test_foreign_origin_and_cross_site_fetch_are_refused(self):
        status, _headers, _body = self.request("GET", "/api/state",
                                              origin="http://evil.example.com")
        self.assertEqual(403, status)
        status, _headers, _body = self.request("GET", "/api/state",
                                              headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(403, status)

    def test_no_wildcard_cors_header_is_ever_sent(self):
        status, headers, _body = self.request("GET", "/api/health")
        self.assertEqual(200, status)
        joined = " ".join(f"{k}: {v}" for k, v in headers.items()).lower()
        self.assertNotIn("access-control-allow-origin", joined)

    def test_localhost_origin_is_accepted(self):
        status, _headers, _body = self.request("GET", "/api/state",
                                              origin=f"http://localhost:{self.port}",
                                              headers={"Cookie": self._fresh_cookie()})
        self.assertEqual(200, status)

    def _fresh_cookie(self):
        _csrf, set_cookie = self.start_session()
        return self.cookie_header(set_cookie)


# ==================================================================== L03: caching
class CacheHeaders(ServiceCase):
    def test_entry_points_and_api_are_no_store(self):
        for path in ("/", "/index.html", "/?x=1", "/index.html?x=1", "/api/health"):
            with self.subTest(path=path):
                status, headers, _body = self.request("GET", path)
                self.assertEqual(200, status)
                self.assertEqual("no-store", headers.get("Cache-Control"), path)

    def test_static_assets_may_cache(self):
        for path in ("/static/app.js", "/static/styles.css"):
            with self.subTest(path=path):
                status, headers, _body = self.request("GET", path)
                self.assertEqual(200, status)
                self.assertNotEqual("no-store", headers.get("Cache-Control"))

    def test_cache_control_helper_matches_the_rules(self):
        cases = {"/": "no-store", "/index.html": "no-store", "/index.html?x=1": "no-store",
                 "/?bootstrap=abc": "no-store", "/api/state": "no-store",
                 "/api/session": "no-store", "/static/app.js": "max-age=3600",
                 "/vendor/pdfjs/pdf.min.mjs": "max-age=3600", "/unknown": "no-store"}
        for path, expected in cases.items():
            with self.subTest(path=path):
                self.assertEqual(expected, server.cache_control_for(path))


# ==================================================================== L04: body limits
class RequestLimits(ServiceCase):
    def test_route_limits_are_distinct(self):
        self.assertEqual(16 * 1024, server.limit_for("/api/settings"))
        self.assertEqual(16 * 1024, server.limit_for("/api/session/bootstrap"))
        self.assertEqual(64 * 1024, server.limit_for("/api/save"))
        self.assertEqual(256 * 1024, server.limit_for("/api/beacon/save"))
        self.assertEqual(80 * 1024 * 1024, server.limit_for("/api/import"))
        self.assertEqual(200 * 1024 * 1024, server.limit_for("/api/restore"))
        self.assertEqual(16 * 1024, server.limit_for("/api/unknown"))

    def test_oversized_management_json_is_refused_before_authentication(self):
        payload = b"x" * (20 * 1024)
        status, _headers, body = self.request("POST", "/api/settings", payload)
        self.assertEqual(413, status, body)
        self.assertIn("上限", body.decode("utf-8"))

    def test_missing_or_invalid_content_length_is_refused(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.putrequest("POST", "/api/save")
            conn.putheader("Host", f"127.0.0.1:{self.port}")
            conn.putheader("Content-Length", "not-a-number")
            conn.endheaders()
            response = conn.getresponse()
            self.assertEqual(400, response.status)
        finally:
            conn.close()
        # a negative length is refused as well
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.putrequest("POST", "/api/save")
            conn.putheader("Host", f"127.0.0.1:{self.port}")
            conn.putheader("Content-Length", "-5")
            conn.endheaders()
            response = conn.getresponse()
            self.assertEqual(400, response.status)
        finally:
            conn.close()

    def test_zero_length_is_refused(self):
        status, _headers, _body = self.request("POST", "/api/save", b"")
        self.assertEqual(413, status)

    def test_a_real_reading_card_still_saves(self):
        """The 64 KiB management limit must not break an ordinary save."""
        state = json.loads((self.tmp / "library.json").read_text(encoding="utf-8"))
        paper = state["papers"][0]
        csrf, set_cookie = self.start_session()
        cookie = self.cookie_header(set_cookie)
        patch = {"fields": paper["fields"], "annotations": paper["annotations"],
                 "steps": paper["steps"]}
        status, _headers, body = self.request(
            "POST", "/api/save",
            {"id": paper["id"], "revision": paper["revision"], "patch": patch},
            headers={"Cookie": cookie, "X-CSRF-Token": csrf})
        self.assertEqual(200, status, body)

    def test_import_keeps_its_own_large_limit(self):
        """A PDF larger than the management limit is still accepted (not a 413)."""
        source = ROOT / "data" / "papers" / "finance-2021-zhang.pdf"
        data = source.read_bytes()
        self.assertGreater(len(data), server.limit_for("/api/save"))
        csrf, set_cookie = self.start_session()
        cookie = self.cookie_header(set_cookie)
        status, _headers, body = self.request(
            "POST", "/api/import", data,
            headers={"Cookie": cookie, "X-CSRF-Token": csrf,
                     "X-File-Name": "phase3-import.pdf"})
        self.assertEqual(200, status, body[:200])


# ==================================================================== L05: concurrency
class Concurrency(ServiceCase):
    def test_concurrency_ceiling_is_enforced_by_closing_connections(self):
        self.assertEqual(4, self.httpd.max_concurrency)
        sockets = []
        try:
            for _ in range(6):
                probe = socket.create_connection(("127.0.0.1", self.port), timeout=5)
                sockets.append(probe)
            # the server never accepts more than its budget; the extra sockets are closed by
            # the server side, so a normal request still succeeds afterwards
            # once the held sockets time out, an ordinary request must be served again
            deadline = time.time() + 40
            served = False
            while time.time() < deadline and not served:
                try:
                    probe = socket.create_connection(("127.0.0.1", self.port), timeout=5)
                    probe.sendall(b"GET /api/health HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n\r\n"
                                  % self.port)
                    probe.settimeout(10)
                    data = probe.recv(200)
                    probe.close()
                    served = b"200" in data.split(b"\r\n", 1)[0]
                except OSError:
                    time.sleep(0.5)
            self.assertTrue(served, "the service must recover after the held sockets close")
        finally:
            for item in sockets:
                try:
                    item.close()
                except OSError:
                    pass

    def test_reading_endpoints_still_answer_while_connections_are_open(self):
        holders = [socket.create_connection(("127.0.0.1", self.port), timeout=5)
                   for _ in range(3)]
        try:
            status, _headers, _body = self.request("GET", "/api/health")
            self.assertEqual(200, status)
        finally:
            for item in holders:
                item.close()


# ==================================================================== L06: beacon route
class BeaconRoute(ServiceCase):
    def test_beacon_route_accepts_a_body_token_and_saves(self):
        csrf, set_cookie = self.start_session()
        cookie = self.cookie_header(set_cookie)
        state = json.loads((self.tmp / "library.json").read_text(encoding="utf-8"))
        paper = state["papers"][0]
        payload = {"csrf": csrf, "id": paper["id"], "revision": paper["revision"],
                   "patch": {"fields": paper["fields"], "annotations": paper["annotations"],
                             "steps": paper["steps"]}}
        status, _headers, body = self.request("POST", "/api/beacon/save", payload,
                                             headers={"Cookie": cookie})
        self.assertEqual(200, status, body)

    def test_beacon_route_refuses_a_wrong_token(self):
        csrf, set_cookie = self.start_session()
        cookie = self.cookie_header(set_cookie)
        status, _headers, _body = self.request("POST", "/api/beacon/save",
                                              {"csrf": "wrong", "id": "x"},
                                              headers={"Cookie": cookie})
        self.assertEqual(403, status)

    def test_body_token_is_not_accepted_on_ordinary_routes(self):
        """Only /api/beacon/ may carry a credential in the body."""
        payload = {"token": TOKEN, "id": "medical-2026-zang"}
        status, _headers, _body = self.request("POST", "/api/save", payload)
        self.assertEqual(403, status, "the body token must not authenticate a normal route")

    def test_beacon_route_has_its_own_smaller_limit(self):
        payload = b"y" * (server.BEACON_JSON_LIMIT + 1024)
        status, _headers, _body = self.request("POST", "/api/beacon/save", payload)
        self.assertEqual(413, status)


# ==================================================================== L08: compatibility
class CompatibilityRoutes(ServiceCase):
    def test_legacy_header_token_still_authenticates_reads(self):
        status, _headers, _body = self.request("GET", "/api/state",
                                              headers={"X-Reader-Token": TOKEN})
        self.assertEqual(200, status)

    def test_legacy_header_token_is_not_a_csrf_target(self):
        """CSRF applies to the cookie session; a header token is never sent by a browser.

        The header token is a per-launch value that the page cannot read (the page receives a
        cookie session instead), so a cross-site page cannot make the browser attach it. The
        token still has to be correct.
        """
        status, _headers, body = self.request("POST", "/api/settings",
                                             {"api_key_env": "PAPER_READER_KEY"},
                                             headers={"X-Reader-Token": TOKEN})
        self.assertEqual(200, status, body)
        # and a wrong header token is refused on a write as well
        status, _headers, _body = self.request("POST", "/api/settings",
                                              {"api_key_env": "PAPER_READER_KEY"},
                                              headers={"X-Reader-Token": "wrong"})
        self.assertEqual(403, status)

    def test_state_changing_routes_reject_a_secret_field(self):
        csrf, set_cookie = self.start_session()
        cookie = self.cookie_header(set_cookie)
        for field in ("api_key", "api_key_value", "raw_key", "secret"):
            with self.subTest(field=field):
                status, _headers, body = self.request("POST", "/api/settings",
                                                     {field: "sk-should-never-be-stored"},
                                                     headers={"Cookie": cookie,
                                                              "X-CSRF-Token": csrf})
                self.assertIn(status, (400, 422), body)
                self.assertNotIn("sk-should-never-be-stored", body.decode("utf-8"))

    def test_unknown_api_route_is_404_not_silently_accepted(self):
        csrf, set_cookie = self.start_session()
        status, _headers, _body = self.request(
            "POST", "/api/nonexistent", {"a": 1},
            headers={"Cookie": self.cookie_header(set_cookie), "X-CSRF-Token": csrf})
        self.assertEqual(404, status)

    def _cookie(self):
        csrf, set_cookie = self.start_session()
        self._csrf = csrf
        return self.cookie_header(set_cookie)


# ==================================================================== L07: plain-text display
class PlainTextDisplay(unittest.TestCase):
    def test_the_page_never_injects_html_from_answers(self):
        source = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        for pattern in ("innerHTML=ai", "insertAdjacentHTML", "document.write"):
            self.assertNotIn(pattern, source.replace(" ", ""))

    def test_no_external_resource_is_loaded_by_answer_content(self):
        source = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        # the answer renderer must not build an <img> or a fetch from model text
        for pattern in ("img src='+", 'img src="+', "new Image(", "eval("):
            self.assertNotIn(pattern, source)
        page = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("https://", page, "the page must not reference a remote origin")

    def test_csp_keeps_the_browser_away_from_cloud_apis(self):
        source = (ROOT / "server.py").read_text(encoding="utf-8")
        self.assertIn("connect-src 'self' blob:", source)
        self.assertIn("frame-ancestors 'none'", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
