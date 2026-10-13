from pathlib import Path
root = Path(__file__).resolve().parent / 'review-1.3.2'
def edit(name, old, new):
    p = root / name
    s = p.read_text(encoding='utf-8')
    assert old in s, (name, old[:90])
    p.write_text(s.replace(old, new), encoding='utf-8')

# Credentials shorter than five bytes have no useful substring detection boundary.
# Accept five-character provider tokens and guard them, refuse shorter input explicitly.
edit('credentials.py', '    if len(text.encode("utf-8")) > MAX_KEY_BYTES:', '''    if len(text.encode('utf-8')) < 5:
        return False, CODE_INVALID, 'Credential must contain at least five UTF-8 bytes'
    if len(text.encode("utf-8")) > MAX_KEY_BYTES:''')
edit('credentials.py', 'def registry_hint() -> dict:', '''def live_identity(ref):
    """Process-local keyed digest: binds consent to a value without a reusable hash.

    The salt is not persisted. Restarting conservatively requires credential consent
    again, and a value rotation under the same environment name invalidates queued work.
    """
    import hashlib, hmac
    secret, code, _ = read_reference(ref)
    if secret is None:
        return code if ref else ''
    return hmac.new(_IDENTITY_SALT, secret.reveal().encode('utf-8'), hashlib.sha256).hexdigest()


def registry_hint() -> dict:''')
edit('credentials.py', 'import os\n', 'import os\nimport secrets\n_IDENTITY_SALT = secrets.token_bytes(32)\n')
edit('secret_guard.py', 'import re\n', 'import re\nimport threading\n')
edit('secret_guard.py', '_MAX_TRACKED = 32', '_REGISTRY_LOCK = threading.RLock()')
edit('secret_guard.py', '    _REGISTERED[text] = _REGISTERED.get(text, 0) + 1', '''    with _REGISTRY_LOCK:
        _REGISTERED[text] = _REGISTERED.get(text, 0) + 1''')
edit('secret_guard.py', '    return len(_REGISTERED)', '''    with _REGISTRY_LOCK:
        return len(_REGISTERED)

def _registered_values():
    with _REGISTRY_LOCK:
        return tuple(_REGISTERED)''')
edit('secret_guard.py', 'for value in list(_REGISTERED):', 'for value in _registered_values():')
edit('job_policy.py', '    max_output_tokens: int = 4096', '    max_output_tokens: int = 4096\n    credential_identity: str = ""')
edit('job_policy.py', '"base_url": self.base_url, "max_output_tokens": self.max_output_tokens}',
    '"base_url": self.base_url, "max_output_tokens": self.max_output_tokens,\n                "credential_identity": self.credential_identity}')
edit('job_policy.py', '        fields["base_url"] =', '        fields["credential_identity"] = str(fields.get("credential_identity") or "")\n        fields["base_url"] =')
edit('job_policy.py', '("config_revision", "credential_revision", "source_sha256")',
    '("config_revision", "credential_revision", "source_sha256", "credential_identity")')
edit('job_policy.py', '                      "source_sha256": snapshot_like.get("source_sha256", ""),',
    '                      "source_sha256": snapshot_like.get("source_sha256", ""),\n                      "credential_identity": snapshot_like.get("credential_identity", ""),')
edit('analysis.py', '        if self.prompt_version != snapshot.prompt_version:', '''        if credentials.live_identity(snapshot.credential_ref) != snapshot.credential_identity:
            return False
        if self.prompt_version != snapshot.prompt_version:''')
edit('analysis.py', '            max_output_tokens=int(settings.get("max_output_tokens") or 4096))',
    '''            max_output_tokens=int(settings.get("max_output_tokens") or 4096),
            credential_identity=credentials.live_identity(settings.get('api_key_env', '')))''')
edit('analysis.py', '                       "credential_revision": settings.get("credential_revision", 1),',
    '                       "credential_revision": settings.get("credential_revision", 1),\n                       "credential_identity": credentials.live_identity(reference),')
edit('analysis.py', '        frozen = dict(settings)\n', '''        frozen = dict(settings)
        frozen_credential = credentials.live_identity(settings.get('api_key_env', ''))
''')
edit('analysis.py', '                blocked, _ = self.outbound_blocked(current)', '''                if credentials.live_identity(current.get('api_key_env', '')) != frozen_credential:
                    raise providers.SendDenied(secret_guard.E_TARGET_CHANGED)
                blocked, _ = self.outbound_blocked(current)''')
edit('providers.py', '        previous_timeout = self.timeout\n', '''        if self.credential_missing:
            blocked = self._credential_block()
            return secure_transport.TransportResponse(False, code=blocked.public_code)
        request_started = time.monotonic()
        previous_timeout = self.timeout
''')
edit('providers.py', '        headers = self._headers() if self.api_key else {}', '''        request_limit = min(previous_timeout, timeout) if timeout is not None else previous_timeout
        remaining = request_limit - (time.monotonic() - request_started)
        if remaining <= 0:
            return secure_transport.TransportResponse(False, code=secret_guard.E_TIMEOUT)
        transport.read_timeout = remaining
        headers = self._headers() if self.api_key else {}''')
# Explicitly grant synthetic scanner consent; a scanner must fail if no request ran.
edit('tests/security/phase1_channel_scan.py', '                manager.enqueue(PAPER, reason="force", force=True)', '''                manager.grant_approval(PAPER, scope='synthetic-test')
                manager.enqueue(PAPER, reason="force", force=True)''')
edit('tests/security/phase1_channel_scan.py', '            meta = store.analysis_meta(PAPER)', '''            meta = store.analysis_meta(PAPER)
            report['requests'] = sink.count
            report['exercised'] = sink.count == 1 and meta['status'] == 'failed' \\
                and meta.get('error_code') == 'auth_failed'
            assert report['exercised'], 'Echo-error path must actually send and fail with auth_failed' ''')
edit('tests/security/phase1_channel_scan.py', 'return 0 if not report["findings"] else 1',
    'return 0 if report.get("exercised") and not report["findings"] else 1')
edit('tests/security/phase2_e2e_https.py', '                    manager.enqueue(PAPER, reason="e2e", force=True)',
    '''                    manager.grant_approval(PAPER, scope='synthetic-test')
                    manager.enqueue(PAPER, reason="e2e", force=True)''')
edit('tests/security/phase2_e2e_https.py', '            note("job reached a terminal state",', '''            note('job actually transmitted', stub.count > 0, stub.count)
            note("job reached a terminal state",''')
edit('tests/security/test_phase2_endpoint.py', 'self.assertEqual(2, len(stub.requests), "health_check plus list_models")',
    'self.assertEqual(1, len(stub.requests), "single bounded health response")')
print('Credential rotation and scanner fixes applied')
