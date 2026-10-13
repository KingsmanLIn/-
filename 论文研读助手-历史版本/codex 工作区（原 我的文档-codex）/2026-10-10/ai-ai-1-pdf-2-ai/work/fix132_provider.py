from pathlib import Path
ROOT = Path(__file__).resolve().parent / "review-1.3.2"
def replace(s, old, new):
    assert old in s, old[:100]
    return s.replace(old, new)

p = ROOT / "providers.py"
s = p.read_text(encoding="utf-8")
s = replace(s, "import threading", "import threading\nimport reading_card\nfrom contextlib import nullcontext")
at = s.index("@dataclass\nclass Health")
s = s[:at] + '''class SendDenied(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__("send_denied")


def safe_models(items, field):
    if not isinstance(items, list) or len(items) > 1000:
        return []
    models = []
    for item in items:
        value = item.get(field) if isinstance(item, dict) else None
        if not isinstance(value, str):
            continue
        if secret_guard.contains_secret(value):
            raise SendDenied(secret_guard.E_SECRET_ECHO_BLOCKED)
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/+@-]{0,199}", value):
            models.append(value)
    return sorted(set(models))


''' + s[at:]
start = s.index("    def as_dict(self):", s.index("class Health"))
end = s.index("\n\n\n@dataclass", start)
s = s[:start] + '''    def as_dict(self):
        if secret_guard.contains_secret({"detail": self.detail, "models": self.models}):
            return {"ok": False, "category": CATEGORY_SECRET_ECHO,
                    "code": secret_guard.E_SECRET_ECHO_BLOCKED,
                    "detail": secret_guard.message_for(secret_guard.E_SECRET_ECHO_BLOCKED),
                    "models": [], "latency_ms": self.latency_ms}
        return {"ok": self.ok, "category": self.category, "detail": redact(self.detail),
                "models": safe_models([{"id": v} for v in self.models], "id"),
                "latency_ms": self.latency_ms}
''' + s[end:]
s = replace(s, "        self.on_retransmit = None", "        self.on_retransmit = None\n        self.send_guard = None")
s = replace(s, "        self._transport_obj = self.transport_factory(", "        self._transport_obj = self.transport_factory(")
s = replace(s, "        return self._transport_obj", "        self._transport_obj.send_guard = self.send_guard\n        self._transport_obj.cancel_event = self._cancel\n        return self._transport_obj")
s = replace(s, '    def _send(self, method, path, payload=None):\n        transport = self._transport()',
'''    def _send(self, method, path, payload=None, timeout=None):
        if method == "POST" and self._is_cancelled():
            return secure_transport.TransportResponse(False, code=secret_guard.E_CANCELLED)
        previous_timeout = self.timeout
        if timeout is not None:
            self.timeout = min(self.timeout, timeout)
        try:
            transport = self._transport()
        except (SendDenied, endpoint_policy.PolicyError) as exc:
            return secure_transport.TransportResponse(False, code=exc.code)
        finally:
            self.timeout = previous_timeout''')
s = replace(s, '        response = self._send("POST", path, payload)', '        response = self._send("POST", path, payload, timeout=timeout)')
s = replace(s, '    def _get_json_path(self, path):\n        response = self._send("GET", path)', '    def _get_json_path(self, path, timeout=10):\n        response = self._send("GET", path, timeout=timeout)')
s = replace(s, '                return json.loads(response.body.decode("utf-8", "replace")), None',
'''                payload = reading_card.parse_model_json(response.body.decode("utf-8"))
                if secret_guard.contains_secret(payload):
                    return None, AdapterResult(False, category=CATEGORY_SECRET_ECHO,
                        public_code=secret_guard.E_SECRET_ECHO_BLOCKED)
                return payload, None''')
s = replace(s, "            except (UnicodeDecodeError, json.JSONDecodeError):", "            except (UnicodeDecodeError, reading_card.ValidationError, TypeError):")
start = s.index("        if not text or not text.strip():", s.index("    def _load_json"))
end = s.index("\n    @staticmethod", start)
s = s[:start] + '''        if not isinstance(text, str) or not text.strip():
            return None, AdapterResult(False, category=CATEGORY_INVALID_JSON,
                                      public_code=secret_guard.E_INVALID_RESPONSE)
        if request.max_response_chars and len(text) > request.max_response_chars:
            return None, AdapterResult(False, category=CATEGORY_BAD_RESPONSE,
                                      public_code=secret_guard.E_INVALID_RESPONSE)
        try:
            return reading_card.parse_model_json(text), None
        except (reading_card.ValidationError, TypeError):
            return None, AdapterResult(False, category=CATEGORY_INVALID_JSON,
                                      public_code=secret_guard.E_INVALID_RESPONSE)
''' + s[end:]
s = replace(s, '        if dropped:\n            # A re-send',
'''        if self._is_cancelled():
            return None, AdapterResult(False, category=CATEGORY_CANCELLED,
                public_code=secret_guard.E_CANCELLED), tuple(dropped)
        if dropped:
            # A re-send''')
s = replace(s, '        return sorted(str(item.get("name", "")) for item in payload.get("models", []) if item.get("name"))',
            '        return safe_models(payload.get("models", []), "name")')
s = replace(s, '        return sorted(str(item.get("id", "")) for item in payload.get("data", []) if item.get("id"))',
            '        return safe_models(payload.get("data", []), "id")')
# Parse JSON envelopes with the same strict syntax/shape policy used by model content.
s = replace(s, 'envelope = json.loads(raw.decode("utf-8", "replace"))', 'envelope = reading_card.parse_model_json(raw.decode("utf-8"))')
s = replace(s, 'except (UnicodeDecodeError, json.JSONDecodeError):', 'except (UnicodeDecodeError, reading_card.ValidationError, TypeError):')
p.write_text(s, encoding="utf-8")

p = ROOT / "secret_guard.py"
s = p.read_text(encoding="utf-8")
# There are no arbitrary evictions: all credentials observed during this process
# remain covered, including values that may arrive in late responses after rotation.
s = replace(s, '    if not text or len(text) < 6:', '    if not text:')
s = replace(s, '    while len(_REGISTERED) > _MAX_TRACKED:\n        _REGISTERED.pop(next(iter(_REGISTERED)))', '')
s = replace(s, 'return {item for item in forms if item and len(item) >= 6}', 'return {item for item in forms if item}')
p.write_text(s, encoding="utf-8")
print("provider and output patch applied")
