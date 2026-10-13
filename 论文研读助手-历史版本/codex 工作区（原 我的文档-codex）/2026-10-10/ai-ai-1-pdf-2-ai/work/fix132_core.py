from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent / "review-1.3.2"

def edit(name, fn):
    p = ROOT / name
    old = (ROOT.parent / "fix132-baseline" / name).read_text(encoding="utf-8")
    new = fn(old)
    assert new != old, name
    p.write_text(new, encoding="utf-8")

def replace(s, old, new):
    assert old in s, old[:120]
    return s.replace(old, new)

def schema(s):
    s = replace(s, '    "max_concurrent_jobs": 1,\n', '    "max_concurrent_jobs": 1,\n    "config_revision": 1,\n    "credential_revision": 1,\n')
    s = replace(s, '    merged["model"] = _text(merged["model"], MAX_MODEL_NAME, "model")',
        '    for revision in ("config_revision", "credential_revision"):\n'
        '        value = merged[revision]\n'
        '        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 2**53:\n'
        '            raise SchemaError("invalid_revision")\n'
        '    merged["model"] = _text(merged["model"], MAX_MODEL_NAME, "model")')
    return s

def ledger(s):
    s = replace(s, '    reason: str = ""\n', '    reason: str = ""\n    base_url: str = ""\n    max_output_tokens: int = 4096\n')
    s = replace(s, '"created_at": self.created_at, "reason": self.reason}', '"created_at": self.created_at, "reason": self.reason,\n                "base_url": self.base_url, "max_output_tokens": self.max_output_tokens}')
    s = replace(s, '        fields["generation"] = int(fields.get("generation") or 0)', '        fields["base_url"] = str(fields.get("base_url") or "")\n        fields["max_output_tokens"] = int(fields.get("max_output_tokens") or 4096)\n        fields["generation"] = int(fields.get("generation") or 0)')
    s = replace(s, '        self.load_error = ""', '        self.load_error = ""\n        self.write_error = ""')
    s = replace(s, '            os.replace(temporary, self.path)', '            os.replace(temporary, self.path)\n            self.write_error = ""\n            return True')
    s = replace(s, '                                             outcome=type(exc).__name__))\n\n    # -- approvals', '                                             outcome=type(exc).__name__))\n            self.write_error = "ledger_unavailable"\n            return False\n\n    # -- approvals')
    s = replace(s, '            if (isinstance(existing, dict)', '            if (isinstance(existing, dict)')
    s = replace(s, '                      "model": model, "scope": scope, "revoked": False,',
        '                      "model": model, "scope": scope, "revoked": False,\n'
        '                      "config_revision": snapshot_like.get("config_revision", 1),\n'
        '                      "credential_revision": snapshot_like.get("credential_revision", 1),\n'
        '                      "source_sha256": snapshot_like.get("source_sha256", ""),')
    s = replace(s, '                    and existing.get("model") == model and not existing.get("revoked")):',
        '                    and existing.get("model") == model and not existing.get("revoked")\n'
        '                    and all(existing.get(k) == snapshot_like.get(k) for k in\n'
        '                            ("config_revision", "credential_revision", "source_sha256"))):')
    s = replace(s, '            self.state["journal"] = journal[-1000:]\n            self._save_locked()\n            return reservation, DENY_OK',
        '            # Keep every charge in the 24-hour window, and active-job charges\n'
        '            # even beyond that window. Never truncate by a fixed item count.\n'
        '            active = set(self.state.get("active", {}).values())\n'
        '            self.state["journal"] = [e for e in journal if\n'
        '                float(e.get("started_at_epoch") or 0) >= now - 86400\n'
        '                or e.get("job_id") in active]\n'
        '            if not self._save_locked():\n'
        '                return None, DENY_JOB_LIMIT\n'
        '            return reservation, DENY_OK')
    s = replace(s, '        cutoff = (now or self.clock()) - 24 * 3600', '        cutoff = (self.clock() if now is None else now) - 24 * 3600')
    s = replace(s, '    if ledger.paused():', '    if ledger.write_error or ledger.load_error:\n        return None, DENY_NO_APPROVAL\n    if ledger.paused():')
    s = replace(s, '    if not reserve:\n        return None, DENY_OK',
        '    if approval.get("approval_id") != snapshot.approval_id:\n'
        '        return None, DENY_NO_APPROVAL\n'
        '    if approval.get("model") != snapshot.model:\n'
        '        return None, DENY_TARGET_CHANGED\n'
        '    for name in ("config_revision", "credential_revision", "source_sha256"):\n'
        '        if approval.get(name) != getattr(snapshot, name):\n'
        '            return None, DENY_CREDENTIAL_CHANGED\n'
        '    if ledger.unknown_outcome(snapshot.job_id):\n'
        '        return None, DENY_NO_APPROVAL\n'
        '    if not reserve:\n        return None, DENY_OK')
    return s

def store(s):
    start = s.index('            self.state["settings"]["ai"] = merged', s.index('    def update_ai_settings'))
    end = s.index('            self.persist()', start)
    s = s[:start] + '''            sensitive = ("provider", "base_url", "mode", "model", "api_key_env",
                         "max_output_tokens")
            changed = any(before_ai.get(k) != merged.get(k) for k in sensitive)
            merged["config_revision"] = before_ai.get("config_revision", 1) + int(changed)
            merged["credential_revision"] = before_ai.get("credential_revision", 1) + int(
                before_ai.get("api_key_env") != merged.get("api_key_env"))
            self.state["settings"]["ai"] = merged
            if changed:
                manager = getattr(self, "_analysis_manager", None)
                ledger = manager.ledger if manager is not None else job_policy.JobLedger(self.directory)
                ledger.revoke_all("configuration_changed")
                ledger.revoke_targets()
                for paper_id in list(ledger.state["jobs"]):
                    ledger.clear_job(paper_id)
                self._write_outbound({"schema": 1, "approvals": {}})
                self.state["settings"]["last_rebind_parked"] = self._park_for_rebinding()
                if manager is not None:
                    for paper in self.state.get("papers", []):
                        manager._cancel_running(paper["id"])
''' + s[end:]
    if 'import job_policy' not in s:
        s = replace(s, 'import analysis', 'import job_policy\nimport analysis')
    start = s.index('                    if any(field in body for field in ("provider",')
    end = s.index('                    result = {"settings":', start)
    s = s[:start] + '''                    ai_patch = {k: body[k] for k in schema.AI_SETTINGS_DEFAULTS if k in body}
                    if ai_patch:
                        store.update_ai_settings(ai_patch)
                        settings = copy.deepcopy(store.state["settings"])
''' + s[end:]
    # A final JSON response boundary applies equally to all legacy/new APIs.
    s = replace(s, '            if isinstance(data, (dict, list)):\n                data = json.dumps(data, ensure_ascii=False).encode("utf-8")',
        '            if isinstance(data, (dict, list)):\n'
        '                if secret_guard.contains_secret(data):\n'
        '                    data = {"ok": False, "code": secret_guard.E_SECRET_ECHO_BLOCKED,\n'
        '                            "error": secret_guard.message_for(secret_guard.E_SECRET_ECHO_BLOCKED)}\n'
        '                    status = 502\n'
        '                data = json.dumps(data, ensure_ascii=False).encode("utf-8")')
    return s

def manager(s):
    s = replace(s, 'import threading', 'import threading\nfrom contextlib import contextmanager')
    s = replace(s, '            store.directory, clock=clock, log=self.log)\n',
        '            store.directory, clock=clock, log=self.log)\n'
        '        self.ledger._lock = self.store.lock\n'
        '        self.store._analysis_manager = self\n'
        '        self._execution = threading.local()\n')
    s = replace(s, '        cancel = threading.Event()\n        settings = self.ai_settings()\n        adapter = self._effective_provider(settings)\n        self._adapter = adapter\n        context = self.build_context(paper_id)\n        snapshot = context.get("job")',
        '        cancel = threading.Event()\n'
        '        with self.store.lock:\n'
        '            snapshot = self.ledger.frozen_job(paper_id)\n'
        '            if snapshot is None or not self._snapshot_valid(snapshot):\n'
        '                return\n'
        '            context = self.build_context(paper_id, snapshot=snapshot)\n'
        '            adapter = self._effective_provider(self._snapshot_settings(snapshot))\n'
        '        self._execution.snapshot = snapshot\n'
        '        self._execution.cancel = cancel\n'
        '        self._adapter = adapter')
    s = replace(s, '            if not result.ok:\n                self._fail', '            if self._superseded(paper_id, context):\n                return\n            if not result.ok:\n                self._fail')
    s = replace(s, '            self.store.write_analysis_answer(paper_id, answer)',
        '            with self.store.lock:\n'
        '                if not self._snapshot_valid(snapshot) or cancel.is_set():\n'
        '                    return\n'
        '                self.store.write_analysis_answer(paper_id, answer)')
    s = replace(s, '            self._forced_ids.pop(paper_id, None)\n            with self._current_lock:',
        '            if self.ledger.job_is_current(paper_id, job_id):\n'
        '                self._forced_ids.pop(paper_id, None)\n'
        '            self._execution.snapshot = None\n'
        '            self._execution.cancel = None\n'
        '            with self._current_lock:')
    s = replace(s, '        def on_retransmit(parameter):\n            reservation, code',
        '        def on_retransmit(parameter):\n'
        '            if (cancel is not None and cancel.is_set()) or self._stop.is_set():\n'
        '                return False\n'
        '            if not self._snapshot_valid(snapshot):\n'
        '                return False\n'
        '            reservation, code')
    s = replace(s, '        adapter.on_retransmit = on_retransmit',
        '        adapter.send_guard = lambda: self._send_guard(snapshot, cancel)\n'
        '        adapter.on_retransmit = on_retransmit')
    s = replace(s, '    def _job_id(self, paper_id):\n        snapshot = self.ledger.frozen_job(paper_id)',
        '    def _job_id(self, paper_id):\n'
        '        snapshot = getattr(self._execution, "snapshot", None) or self.ledger.frozen_job(paper_id)')
    s = replace(s, '        attempts = self._attempts_so_far(paper_id)\n        unknown',
        '        executing = getattr(self._execution, "snapshot", None)\n'
        '        if executing is not None and not self._snapshot_valid(executing):\n'
        '            self.ledger.record_outcome(executing.job_id, "superseded")\n'
        '            return\n'
        '        attempts = self._attempts_so_far(paper_id)\n        unknown')
    s = replace(s, '            self._push(paper_id)\n            self.log(secret_guard.log_record(event="retry"',
        '            if executing is not None and not self._snapshot_valid(executing):\n'
        '                return\n'
        '            self._push(paper_id)\n            self.log(secret_guard.log_record(event="retry"')
    s = replace(s, '                         provider=self.ai_settings().get("provider", "") or "none",',
        '                         provider=provider or provider_name,')
    s = replace(s, '            reason=reason)', '            reason=reason, base_url=str(settings.get("base_url") or ""),\n            max_output_tokens=int(settings.get("max_output_tokens") or 4096))')
    s = replace(s, '            paper_id, {"target_identity": identity, "credential_ref": reference},',
        '            paper_id, {"target_identity": identity, "credential_ref": reference,\n'
        '                       "config_revision": settings.get("config_revision", 1),\n'
        '                       "credential_revision": settings.get("credential_revision", 1),\n'
        '                       "source_sha256": pdftext.file_sha256(self.files.pdf_path(paper_id))\n'
        '                            if self.files.has_pdf(paper_id) else ""},')
    s = replace(s, '        with self.store.lock:\n            paper = self.store.paper(paper_id)\n            meta = paper.setdefault("analysis", schema.default_analysis())\n            self._set(paper, meta, status, **changes)',
        '        with self.store.lock:\n'
        '            snapshot = getattr(self._execution, "snapshot", None)\n'
        '            if snapshot is not None and not self._snapshot_valid(snapshot):\n'
        '                self.ledger.record_outcome(snapshot.job_id, "superseded")\n'
        '                return False\n'
        '            paper = self.store.paper(paper_id)\n'
        '            meta = paper.setdefault("analysis", schema.default_analysis())\n'
        '            self._set(paper, meta, status, **changes)')
    s = replace(s, '            file_name = self.store.write_failed_answer(paper_id, record)',
        '            with self.store.lock:\n'
        '                if not self._snapshot_valid(context.get("job")):\n'
        '                    return\n'
        '                file_name = self.store.write_failed_answer(paper_id, record)')
    # Freeze under the same lock that the conditional state writer uses.
    s = replace(s, '                self.store.persist()\n        self.ledger.freeze_job(snapshot)',
        '                self.store.persist()\n            self.ledger.freeze_job(snapshot)')
    insert = s.index('    def snapshot_for(')
    s = s[:insert] + '''    def _snapshot_settings(self, snapshot):
        return {"provider": snapshot.adapter, "mode": snapshot.mode,
                "base_url": snapshot.base_url, "model": snapshot.model,
                "api_key_env": snapshot.credential_ref,
                "max_output_tokens": snapshot.max_output_tokens,
                "config_revision": snapshot.config_revision,
                "credential_revision": snapshot.credential_revision}

    def _snapshot_valid(self, snapshot):
        if snapshot is None or not self.ledger.job_is_current(snapshot.paper_id, snapshot.job_id):
            return False
        current = self.ai_settings()
        frozen = self._snapshot_settings(snapshot)
        for key in ("provider", "mode", "base_url", "model", "api_key_env",
                    "max_output_tokens", "config_revision", "credential_revision"):
            if current.get(key) != frozen.get(key):
                return False
        if self.prompt_version != snapshot.prompt_version:
            return False
        if self.files.has_pdf(snapshot.paper_id):
            return pdftext.file_sha256(self.files.pdf_path(snapshot.paper_id)) == snapshot.source_sha256
        return not snapshot.source_sha256

    @contextmanager
    def _send_guard(self, snapshot, cancel):
        # The lock covers the last permission check and writing HTTP bytes, but
        # never the response read, so cancellation remains available.
        with self.store.lock:
            if (cancel is not None and cancel.is_set()) or self._stop.is_set():
                raise providers.SendDenied(secret_guard.E_CANCELLED)
            if not self._snapshot_valid(snapshot):
                raise providers.SendDenied(secret_guard.E_STALE_GENERATION)
            _, code = job_policy.decide_send(self.ledger, snapshot, self.limits(), reserve=False)
            if code != job_policy.DENY_OK:
                raise providers.SendDenied(secret_guard.E_CONSENT_REQUIRED)
            yield

''' + s[insert:]
    return s

edit("schema.py", schema)
edit("job_policy.py", ledger)
edit("server.py", store)
edit("analysis.py", manager)
print("core patch applied")
