from pathlib import Path
ROOT = Path(__file__).resolve().parent / 'review-1.3.2'

def edit(name, old, new):
    p = ROOT / name
    s = p.read_text(encoding='utf-8')
    assert old in s, (name, old[:90])
    p.write_text(s.replace(old, new), encoding='utf-8')

edit('secure_transport.py', 'import time\n', 'import time\nimport threading\nfrom contextlib import nullcontext\n')
edit('secure_transport.py', '        self.authorized_addresses = []\n', '''        self.authorized_addresses = []
        self.send_guard = None
        self.cancel_event = None
        self.request_written = False
        self._deadline = 0
        self._socket = None
        self._socket_lock = threading.Lock()

    def _remaining(self):
        remaining = self._deadline - time.monotonic() if self._deadline else self.read_timeout
        if remaining <= 0:
            raise socket.timeout()
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise OutboundDenied(secret_guard.E_CANCELLED)
        return remaining

    def abort(self):
        # shutdown interrupts reads even when HTTPResponse retains a socket file.
        with self._socket_lock:
            sock = self._socket
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            _close_quietly(sock)

    def _track(self, sock):
        with self._socket_lock:
            self._socket = sock
        return sock
''')
edit('secure_transport.py', '                raw = socket.socket(family, socket.SOCK_STREAM)\n                raw.settimeout(self.connect_timeout)', '''                raw = self._track(socket.socket(family, socket.SOCK_STREAM))
                raw.settimeout(min(self.connect_timeout, self._remaining()))''')
edit('secure_transport.py', '''                    wrapped = self._context().wrap_socket(
                        raw, server_hostname=target.host)''', '''                    wrapped = self._track(self._context().wrap_socket(
                        raw, server_hostname=target.host, do_handshake_on_connect=False))''')
edit('secure_transport.py', '                    wrapped.settimeout(self.read_timeout)', '''                    wrapped.settimeout(min(self.connect_timeout, self._remaining()))
                    wrapped.do_handshake()
                    wrapped.settimeout(self._remaining())''')
edit('secure_transport.py', '                raw.settimeout(self.read_timeout)', '                raw.settimeout(self._remaining())')
edit('secure_transport.py', '        connection = None\n        try:', '''        connection = None
        done = threading.Event()
        total_timeout = max(0.001, float(timeout or self.read_timeout))
        self._deadline = time.monotonic() + total_timeout
        self.request_written = False

        def watch():
            while not done.wait(min(0.02, max(0.001, self._deadline - time.monotonic()))):
                if time.monotonic() >= self._deadline or (
                        self.cancel_event is not None and self.cancel_event.is_set()):
                    self.abort()
                    return
        watcher = threading.Thread(target=watch, daemon=True, name='bounded-http')
        watcher.start()
        try:''')
edit('secure_transport.py', '''            connection.connect()
            connection.request(method, url_path, body=body, headers=send_headers)
            response = connection.getresponse()
            raw = response.read(self.max_body + 1)''', '''            connection.connect()
            # Connect carries no credential. The application lock covers the final
            # authorization check and HTTP write; no response read holds that lock.
            with self.send_guard() if self.send_guard else nullcontext():
                self._remaining()
                connection.sock.settimeout(self._remaining())
                # A partial write may already be billable, so conservatively charge it.
                self.request_written = True
                connection.request(method, url_path, body=body, headers=send_headers)
            self._remaining()
            response = connection.getresponse()
            parts, size = [], 0
            while size <= self.max_body:
                self._remaining()
                with self._socket_lock:
                    active = self._socket
                if active is not None:
                    active.settimeout(self._remaining())
                block = response.read1(min(65536, self.max_body + 1 - size))
                if not block:
                    break
                parts.append(block)
                size += len(block)
            raw = b''.join(parts)
            self._remaining()''')
# Replace exception handling with fixed, classified errors; never echo upstream text.
p = ROOT / 'secure_transport.py'
s = p.read_text(encoding='utf-8')
a = s.index('        except _ConnectFailure as exc:', s.index('    def _request('))
b = s.index('    def post_json(', a)
s = s[:a] + '''        except OutboundDenied as exc:
            return TransportResponse(False, code=exc.code)
        except policy_mod.PolicyError as exc:
            return TransportResponse(False, code=exc.code)
        except (OSError, http.client.HTTPException, _ConnectFailure) as exc:
            if self.cancel_event is not None and self.cancel_event.is_set():
                code = secret_guard.E_CANCELLED
            elif time.monotonic() >= self._deadline or isinstance(exc, socket.timeout):
                code = secret_guard.E_TIMEOUT
            elif isinstance(exc, (ssl.SSLError, ssl.SSLCertVerificationError)):
                code = secret_guard.E_TLS_FAILED
            else:
                code = getattr(exc, 'code', secret_guard.E_UNREACHABLE)
            return TransportResponse(False, code=code,
                                     duration_ms=int((time.time() - started) * 1000))
        finally:
            done.set()
            self.abort()
            if connection is not None:
                connection.close()
            with self._socket_lock:
                self._socket = None
            watcher.join(timeout=0.1)
            self._deadline = 0

''' + s[b:]
s += '''\n\nclass OutboundDenied(Exception):
    def __init__(self, code):
        super().__init__('outbound request refused')
        self.code = code
'''
p.write_text(s, encoding='utf-8')

# Bound DNS without unlimited timed-out resolver threads. The slots remain occupied
# until the actual resolver finishes, even after the caller's deadline expires.
edit('endpoint_policy.py', 'import socket\n', 'import socket\nimport threading\n')
edit('endpoint_policy.py', 'def resolve(host:', '''_RESOLVER_SLOTS = threading.BoundedSemaphore(4)

def bounded_resolve(host, port, timeout=10):
    if not _RESOLVER_SLOTS.acquire(blocking=False):
        raise PolicyError('timeout', 'DNS resolver capacity exhausted')
    complete, result = threading.Event(), []
    def worker():
        try:
            result.append((True, resolve(host, port)))
        except Exception as exc:
            result.append((False, exc))
        finally:
            _RESOLVER_SLOTS.release()
            complete.set()
    threading.Thread(target=worker, daemon=True, name='bounded-dns').start()
    if not complete.wait(max(0.001, timeout)):
        raise PolicyError('timeout', 'DNS deadline exceeded')
    ok, value = result[0]
    if not ok:
        raise value
    return value


def resolve(host:''')
edit('providers.py', 'class SendDenied(RuntimeError):', 'class SendDenied(secure_transport.OutboundDenied):')
edit('providers.py', '''        policy.authorize()
        self._policy = policy''', '''        policy.authorize(resolver=lambda host, port: endpoint_policy.bounded_resolve(
            host, port, timeout=min(self.connect_timeout, self.timeout)))
        self._policy = policy''')
edit('providers.py', '''        headers = self._headers() if self.api_key else {}
        self.request_sent = True
        if method == "POST":''', '''        headers = self._headers() if self.api_key else {}
        if method == "POST":''')
edit('providers.py', '''        self.last_transport = response
        return response''', '''        written = bool(getattr(transport, 'request_written', True))
        self.request_sent = self.request_sent or written
        if written:
            self.transmissions = getattr(self, 'transmissions', 0) + 1
        self.last_transport = response
        return response''')
edit('providers.py', '''        with self._lock:
            self._cancel.set()
        return True''', '''        with self._lock:
            self._cancel.set()
        transport = self._transport_obj
        if transport is not None and hasattr(transport, 'abort'):
            transport.abort()
        return True''')
edit('providers.py', '        self._reset_cancel()\n        blocked = self._credential_block()', '''        self.request_sent = False
        self.transmissions = 0
        blocked = self._credential_block()''')
edit('providers.py', 'self._get_json_path("/models")', 'self._get_json_path("/models", timeout=timeout)')
edit('providers.py', 'self._get_json_path("/api/tags")', 'self._get_json_path("/api/tags", timeout=timeout)')
edit('providers.py', '        models = self.list_models(timeout)\n', '''        try:
            models = safe_models(payload.get('data', []), 'id') if self.name == 'openai_compatible' \\
                else safe_models(payload.get('models', []), 'name')
        except SendDenied as exc:
            return Health(False, CATEGORY_SECRET_ECHO, secret_guard.message_for(exc.code), [], latency)
''')

edit('job_policy.py', '        with self._lock:\n            job_id = snapshot.job_id if snapshot else ""', '''        with self._lock:
            if self.load_error or self.write_error:
                return None, DENY_NO_APPROVAL
            job_id = snapshot.job_id if snapshot else ""''')
edit('job_policy.py', '                                      kind=kind, sequence=used_for_job + 1,', '                                      kind=kind, sequence=used_for_job + 1, started_at=now,')

# Shared coordinator: multiple managers in the same Store must not overwrite a
# separately loaded budget/approval ledger. All settings/job operations share one RLock.
edit('analysis.py', '''self.ledger = ledger if ledger is not None else job_policy.JobLedger(
            store.directory, clock=clock, log=self.log)''', '''self.ledger = ledger if ledger is not None else getattr(store, '_job_ledger', None)
        if self.ledger is None:
            self.ledger = job_policy.JobLedger(store.directory, clock=clock, log=self.log)
        store._job_ledger = self.ledger''')
edit('analysis.py', '    def enqueue(self, paper_id, reason="request", force=False, context=None):', '''    def enqueue(self, paper_id, reason="request", force=False, context=None):
        with self.store.lock:
            return self._enqueue_locked(paper_id, reason, force, context)

    def _enqueue_locked(self, paper_id, reason="request", force=False, context=None):''')
edit('analysis.py', '    def grant_approval(self, paper_id, model="", scope="single_paper"):', '''    def grant_approval(self, paper_id, model="", scope="single_paper"):
        with self.store.lock:
            return self._grant_approval_locked(paper_id, model, scope)

    def _grant_approval_locked(self, paper_id, model="", scope="single_paper"):''')
edit('analysis.py', '        return not isinstance(effective, providers.DisabledAdapter), effective, effective.health_check()', '''        frozen = dict(settings)
        @contextmanager
        def health_guard():
            with self.store.lock:
                current = self.ai_settings()
                for key in ('provider', 'mode', 'base_url', 'api_key_env', 'model',
                            'config_revision', 'credential_revision'):
                    if frozen.get(key) != current.get(key):
                        raise providers.SendDenied(secret_guard.E_TARGET_CHANGED)
                blocked, _ = self.outbound_blocked(current)
                if blocked or self.ledger.load_error or self.ledger.write_error:
                    raise providers.SendDenied(blocked or secret_guard.E_CONSENT_REQUIRED)
                if not self.target_approved(current):
                    raise providers.SendDenied(secret_guard.E_CONSENT_REQUIRED)
                yield
        effective.send_guard = health_guard
        health = effective.health_check()
        return bool(health.ok) and not isinstance(effective, providers.DisabledAdapter), effective, health''')
edit('server.py', 'base_url = (settings or {}).get("base_url", "") or ""', '''base_url = (settings or {}).get("base_url", "") or (
            providers.DEFAULT_OLLAMA_BASE if settings.get('provider') == 'ollama'
            else providers.DEFAULT_OPENAI_BASE)''')
edit('server.py', '            merged = schema.validate_ai_settings(merged)\n', '''            merged = schema.validate_ai_settings(merged)
            reference = credentials.normalise_ref(merged.get('api_key_env', ''))
            if reference and reference.startswith('env:') and not credentials.is_registered(
                    reference[4:]):
                raise ValueError('Credential environment reference is not registered')
''')
print('Transport, DNS and coordinator fixes applied')
