"""A recording transport double for tests that assert on the outbound request.

`SecureTransport` is exercised for real by `tests/security/test_phase2_*.py` (including a
real local TLS socket). This double exists so the older adapter tests can keep asserting the
*payload shape* without a socket, and it records only what those tests need:

* the URL path (relative to the approved target),
* the decoded JSON body,
* the header names, plus whether an Authorization header was present and its scheme —
  never the header value.

It implements the same `post_json`/`get_json` surface as the real transport and returns the
same `TransportResponse`, so a caller cannot tell the difference in behaviour.
"""
from __future__ import annotations

import json

import secure_transport


class RecordingTransport:
    def __init__(self, policy, ssl_context=None, connect_timeout=None, read_timeout=None,
                 respond=None, **kwargs):
        self.policy = policy
        self.requests = []
        self.respond = respond or (lambda method, path, body: secure_transport.TransportResponse(
            True, status=200, body=b"{}"))
        self.connections = []
        self.authorized_addresses = [address for _family, address in
                                     (policy.connection_addresses() or [])]

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _body(payload):
        return json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else b""

    def _record(self, method, path, payload, headers):
        auth = ""
        for name, value in (headers or {}).items():
            if name.lower() == "authorization" and isinstance(value, str):
                auth = value.split(" ", 1)[0]
        entry = {
            "method": method,
            "path": path,
            "url": self.policy.target.url_for(path),
            "body": payload,
            "body_bytes": len(self._body(payload)),
            "header_names": sorted((headers or {}).keys()),
            "auth_present": bool(auth),
            "auth_scheme": auth,
        }
        self.requests.append(entry)
        return entry

    def post_json(self, path, payload, headers=None):
        entry = self._record("POST", path, payload, headers)
        response = self.respond("POST", path, entry["body"])
        entry["status"] = response.status
        entry["code"] = response.code
        return response

    def get_json(self, path, headers=None):
        entry = self._record("GET", path, None, headers)
        response = self.respond("GET", path, None)
        entry["status"] = response.status
        entry["code"] = response.code
        return response

    # -- assertions helpers -------------------------------------------------
    @property
    def last(self):
        return self.requests[-1] if self.requests else None


def json_response(payload, status=200, request_id="", code="ok"):
    return secure_transport.TransportResponse(True, status=status,
                                              body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                              code=code, request_id=request_id)


def raw_response(text, status=200, request_id="", body_code="ok"):
    return secure_transport.TransportResponse(True, status=status, body=text.encode("utf-8"),
                                              code=body_code, request_id=request_id)


def error_response(code, status=0, request_id=""):
    return secure_transport.TransportResponse(False, status=status, code=code,
                                              request_id=request_id)
