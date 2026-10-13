"""Immutable job identity, send authorisation and the request budget.

Three guarantees live here, and each is enforced on the server side:

**One identity per job.** When a job is queued, everything that decides *what* is sent and
*where* is frozen into a snapshot: job id, generation, paper id, source PDF hash, profile id,
target identity, model, policy/config/credential revisions, prompt version, language and the
approval it relies on. The worker then reads only that snapshot, so a settings change in the
middle of a job cannot produce a mixed identity or a request to a target the user never
approved. No credential value is ever stored in a snapshot.

**Authorisation is revocable and target-bound.** An approval names the paper and the exact
target identity. Changing the target, the credential reference or the model clears it; a job
whose approval no longer exists refuses to send and parks instead. A job that was restored
from a backup never carries an approval: a restored `queued`/`running` row is paper state, not
permission to spend money.

**Money is reserved before it is spent.** A single call budget counts the first request, the
repair and every retry. Reservations are taken atomically, so when several jobs race for the
last daily slot exactly one wins and the others are refused *before* any HTTP happens. A
request that was never sent is refunded; a request that was sent but whose outcome is unknown
stays charged and is never retried automatically.

The ledger is a separate file next to the library (`data/job-ledger.json`) so it is not part
of the library schema and not part of a backup. It holds no credential value.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import secret_guard
import storage

LEDGER_VERSION = 1
LEDGER_FILE = "job-ledger.json"
POLICY_VERSION = "job-policy-v1"

DEFAULT_LIMITS = {
    "max_concurrent_jobs": 1,
    "max_queue_depth": 20,
    "max_requests_per_job": 2,          # first request + one repair, per the reading card
    # Matches schema.AI_SETTINGS_DEFAULTS: a reasoning model spends output tokens on its
    # reasoning channel first, so a 4096 ceiling can leave the answer empty.
    "max_output_tokens": 16384,
    "daily_request_limit": 100,
}
LIMIT_RANGES = {
    "max_concurrent_jobs": (1, 4),
    "max_queue_depth": (1, 500),
    "max_requests_per_job": (1, 4),
    "max_output_tokens": (256, 32768),
    "daily_request_limit": (1, 10000),
}

DENY_OK = "ok"
DENY_NO_APPROVAL = "consent_required"
DENY_TARGET_CHANGED = "target_changed"
DENY_PAUSED = "ai_paused"
DENY_PROFILE_DISABLED = "profile_disabled"
DENY_JOB_LIMIT = "job_request_limit"
DENY_DAY_LIMIT = "daily_request_limit"
DENY_QUEUE_FULL = "queue_full"
DENY_GENERATION = "stale_generation"
DENY_CREDENTIAL_CHANGED = "credential_changed"


def _now():
    return time.time()


def _utc(stamp=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stamp or _now()))


def new_id(prefix=""):
    return (prefix + secrets.token_hex(8)) if prefix else secrets.token_hex(16)


@dataclass(frozen=True)
class JobSnapshot:
    """Everything a job is allowed to rely on. Frozen at enqueue time."""

    job_id: str
    paper_id: str
    generation: int
    source_sha256: str
    profile_id: str
    adapter: str
    mode: str
    target_identity: str
    model: str
    credential_ref: str
    credential_revision: int
    config_revision: int
    policy_version: str
    prompt_version: str
    language: str
    approval_id: str
    budget_id: str
    created_at: str
    reason: str = ""
    base_url: str = ""
    max_output_tokens: int = 16384
    #: Frozen with the job so one run cannot change its mind about sending page images: the
    #: reader approved a target, and this records how much of the paper went to it.
    vision: bool = False
    max_figure_pages: int = 4
    credential_identity: str = ""

    def as_dict(self):
        """Safe to store and to show: no credential value, no prompt text."""
        return {"job_id": self.job_id, "paper_id": self.paper_id,
                "generation": self.generation, "source_sha256": self.source_sha256,
                "profile_id": self.profile_id, "adapter": self.adapter, "mode": self.mode,
                "target_identity": self.target_identity, "model": self.model,
                "credential_ref": self.credential_ref,
                "credential_revision": self.credential_revision,
                "config_revision": self.config_revision, "policy_version": self.policy_version,
                "prompt_version": self.prompt_version, "language": self.language,
                "approval_id": self.approval_id, "budget_id": self.budget_id,
                "created_at": self.created_at, "reason": self.reason,
                "base_url": self.base_url, "max_output_tokens": self.max_output_tokens,
                "vision": bool(self.vision),
                "max_figure_pages": int(self.max_figure_pages),
                "credential_identity": self.credential_identity}

    @classmethod
    def from_dict(cls, payload):
        fields = {name: payload.get(name) for name in cls.__dataclass_fields__}
        fields["credential_identity"] = str(fields.get("credential_identity") or "")
        fields["base_url"] = str(fields.get("base_url") or "")
        fields["max_output_tokens"] = int(fields.get("max_output_tokens") or 16384)
        fields["vision"] = bool(fields.get("vision"))
        fields["max_figure_pages"] = int(fields.get("max_figure_pages") or 4)
        fields["generation"] = int(fields.get("generation") or 0)
        fields["credential_revision"] = int(fields.get("credential_revision") or 0)
        fields["config_revision"] = int(fields.get("config_revision") or 0)
        for name in ("job_id", "paper_id", "source_sha256", "profile_id", "adapter", "mode",
                     "target_identity", "model", "credential_ref", "policy_version",
                     "prompt_version", "language", "approval_id", "budget_id", "created_at",
                     "reason"):
            fields[name] = str(fields.get(name) or "")
        return cls(**fields)


@dataclass
class Reservation:
    """One authorised model call, charged against the job and the day."""

    job_id: str
    paper_id: str
    kind: str                      # first | repair | retry
    sequence: int
    target_identity: str
    started_at: float = field(default_factory=_now)
    settled: str = ""              # sent | not_sent | unknown
    note: str = ""

    def as_dict(self):
        return {"job_id": self.job_id, "paper_id": self.paper_id, "kind": self.kind,
                "sequence": self.sequence, "target_identity": self.target_identity,
                "started_at": _utc(self.started_at), "settled": self.settled,
                "note": self.note}


class JobLedger:
    """Persistent approvals, frozen jobs and the request budget.

    Stored outside the library on purpose: the library shape is a published contract, and this
    is application state that must never travel in a backup. Every mutation is written
    atomically; a corrupt file is reported and replaced with an empty ledger rather than
    silently trusted.
    """

    def __init__(self, directory, clock=time.time, log=None):
        self.directory = Path(directory)
        self.path = self.directory / LEDGER_FILE
        self.clock = clock
        self.log = log or (lambda record: None)
        self._lock = threading.RLock()
        self.state = {"version": LEDGER_VERSION, "approvals": {}, "jobs": {},
                      "active": {}, "spend": [], "outcomes": {}}
        self.load_error = ""
        self.write_error = ""
        self._load()

    # -- persistence -------------------------------------------------------
    def _load(self):
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError as exc:
            self.load_error = type(exc).__name__
            return
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            self.load_error = "invalid_json"
            self.log(secret_guard.log_record(event="ledger_unreadable", outcome="reset"))
            return
        if not isinstance(payload, dict) or payload.get("version") != LEDGER_VERSION:
            self.load_error = "unsupported_version"
            self.log(secret_guard.log_record(event="ledger_unreadable",
                                             outcome=str(payload.get("version"))[:12]
                                             if isinstance(payload, dict) else "not_object"))
            return
        for key in ("approvals", "jobs", "active", "outcomes"):
            if not isinstance(payload.get(key), dict):
                payload[key] = {}
        if not isinstance(payload.get("spend"), list):
            payload["spend"] = []
        self.state = payload

    def _save_locked(self):
        # A corrupt/unreadable budget is evidence, not an empty budget. Preserve it
        # across restarts rather than silently resetting charges through a later save.
        if self.load_error:
            self.write_error = 'ledger_unavailable'
            return False
        payload = dict(self.state)
        try:
            # Reuse the application's fsync + atomic replace writer, including its
            # bounded Windows sharing-violation retries and temporary-file cleanup.
            storage.atomic_write_json(self.path, payload)
            self.write_error = ""
            return True
        except OSError as exc:
            self.log(secret_guard.log_record(event="ledger_write_failed",
                                             outcome=type(exc).__name__))
            self.write_error = "ledger_unavailable"
            return False

    # -- approvals ---------------------------------------------------------
    def grant_approval(self, paper_id, snapshot_like, model="", scope="single_paper"):
        """Record an explicit go-ahead for one paper and one exact target."""
        target = str(snapshot_like.get("target_identity") or "")
        credential_ref = str(snapshot_like.get("credential_ref") or "")
        with self._lock:
            existing = self.state["approvals"].get(paper_id)
            if (isinstance(existing, dict) and existing.get("target_identity") == target
                    and existing.get("credential_ref") == credential_ref
                    and existing.get("model") == model and not existing.get("revoked")
                    and all(existing.get(k) == snapshot_like.get(k) for k in
                            ("config_revision", "credential_revision", "source_sha256", "credential_identity"))):
                return existing
            record = {"approval_id": new_id("ap-"), "paper_id": paper_id,
                      "target_identity": target, "credential_ref": credential_ref,
                      "model": model, "scope": scope, "revoked": False,
                      "config_revision": snapshot_like.get("config_revision", 1),
                      "credential_revision": snapshot_like.get("credential_revision", 1),
                      "source_sha256": snapshot_like.get("source_sha256", ""),
                      "credential_identity": snapshot_like.get("credential_identity", ""),
                      "granted_at": _utc(self.clock())}
            self.state["approvals"][paper_id] = record
            self._save_locked()
            return record

    def approval_for(self, paper_id):
        record = self.state["approvals"].get(paper_id)
        return record if isinstance(record, dict) and not record.get("revoked") else None

    def revoke_approval(self, paper_id, reason=""):
        with self._lock:
            record = self.state["approvals"].get(paper_id)
            if not isinstance(record, dict) or record.get("revoked"):
                return False
            record["revoked"] = True
            record["revoked_at"] = _utc(self.clock())
            record["revoked_reason"] = str(reason)[:60]
            self._save_locked()
            return True

    def clear_approval(self, paper_id):
        """Remove an approval row entirely. Returns True when one was there.

        `revoke_approval` deliberately keeps the row and only flags it: the row still explains
        why a paper is parked after a target change. A *deleted* paper is different - leaving
        its approval behind would mean re-importing the same id arrives pre-approved, which is
        an approval the user never gave for that new file.
        """
        with self._lock:
            dropped = self.state["approvals"].pop(str(paper_id), None) is not None
            if dropped:
                self._save_locked()
            return dropped

    def revoke_all(self, reason=""):
        with self._lock:
            count = 0
            for record in self.state["approvals"].values():
                if isinstance(record, dict) and not record.get("revoked"):
                    record["revoked"] = True
                    record["revoked_at"] = _utc(self.clock())
                    record["revoked_reason"] = str(reason)[:60]
                    count += 1
            if count:
                self._save_locked()
            return count

    def approve_target(self, target_identity, credential_ref="", model=""):
        """Target-level approval, kept for the settings screen."""
        with self._lock:
            self.state.setdefault("targets", {})
            self.state["targets"][target_identity] = {
                "credential_ref": credential_ref, "model": model,
                "approved_at": _utc(self.clock())}
            self._save_locked()

    def target_approved(self, target_identity, credential_ref=""):
        entry = (self.state.get("targets") or {}).get(target_identity)
        if not isinstance(entry, dict):
            return False
        return entry.get("credential_ref", "") == credential_ref

    def revoke_targets(self, keep=""):
        with self._lock:
            targets = self.state.get("targets") or {}
            for key in list(targets):
                if key != keep:
                    targets.pop(key, None)
            self._save_locked()

    # -- jobs --------------------------------------------------------------
    def freeze_job(self, snapshot):
        with self._lock:
            self.state["jobs"][snapshot.paper_id] = snapshot.as_dict()
            self.state["active"][snapshot.paper_id] = snapshot.job_id
            self._save_locked()
            return snapshot

    def frozen_job(self, paper_id):
        payload = self.state["jobs"].get(paper_id)
        return JobSnapshot.from_dict(payload) if isinstance(payload, dict) else None

    def clear_job(self, paper_id, job_id=None):
        with self._lock:
            current = self.state["jobs"].get(paper_id)
            if job_id and isinstance(current, dict) and current.get("job_id") != job_id:
                return False
            if current is None:
                return False
            self.state["jobs"].pop(paper_id, None)
            if self.state["active"].get(paper_id) == (current or {}).get("job_id"):
                self.state["active"].pop(paper_id, None)
            self._save_locked()
            return True

    def job_is_current(self, paper_id, job_id):
        """False when a newer generation replaced this job (a late result must not win)."""
        current = self.state["jobs"].get(paper_id)
        return bool(isinstance(current, dict) and current.get("job_id") == job_id)

    # -- outcomes ----------------------------------------------------------
    def record_outcome(self, job_id, outcome, code=""):
        with self._lock:
            self.state["outcomes"][job_id] = {"outcome": outcome, "code": str(code)[:40],
                                              "at": _utc(self.clock())}
            # keep the map bounded
            while len(self.state["outcomes"]) > 500:
                self.state["outcomes"].pop(next(iter(self.state["outcomes"])), None)
            self._save_locked()

    def outcome_of(self, job_id):
        return self.state["outcomes"].get(job_id)

    def unknown_outcome(self, job_id):
        record = self.state["outcomes"].get(job_id)
        return bool(isinstance(record, dict) and record.get("outcome") == "unknown")

    # -- budget ------------------------------------------------------------
    def _journal_locked(self):
        journal = self.state.get("journal")
        if not isinstance(journal, list):
            journal = []
            self.state["journal"] = journal
        return journal

    def spend_today(self, now=None):
        """Requests charged in the rolling 24 hour window.

        Derived from the journal, which is the single source of truth. An entry that was
        refused before the socket was opened is marked `refunded` and does not count: nothing
        was charged and the user must not lose a slot for a request that never left the
        machine.
        """
        cutoff = (self.clock() if now is None else now) - 24 * 3600
        return len([entry for entry in self._journal_locked()
                    if isinstance(entry, dict) and not entry.get("refunded")
                    and float(entry.get("started_at_epoch") or 0) >= cutoff])

    def job_spend(self, job_id):
        """Charged requests for one job: the first call, the repair and every retry."""
        return len([entry for entry in self._journal_locked()
                    if isinstance(entry, dict) and entry.get("job_id") == job_id
                    and not entry.get("refunded")])

    def reserve(self, snapshot, limits, kind="first", target_identity="", paper_id=""):
        """Atomically take one request slot, or explain why it cannot be taken.

        The count covers the first request, the repair and every retry: one budget per job.
        Returns (Reservation | None, code). Because the whole check-and-append happens under
        one lock, two jobs racing for the last daily slot cannot both win.
        """
        with self._lock:
            if self.load_error or self.write_error:
                return None, DENY_NO_APPROVAL
            job_id = snapshot.job_id if snapshot else ""
            journal = self._journal_locked()
            used_for_job = self.job_spend(job_id) if job_id else 0
            max_per_job = int(limits.get("max_requests_per_job", 2))
            if job_id and used_for_job >= max_per_job:
                return None, DENY_JOB_LIMIT
            daily = int(limits.get("daily_request_limit", 100))
            now = self.clock()
            if self.spend_today(now) >= daily:
                return None, DENY_DAY_LIMIT
            reservation = Reservation(job_id=job_id,
                                      paper_id=paper_id or (snapshot.paper_id if snapshot else ""),
                                      kind=kind, sequence=used_for_job + 1, started_at=now,
                                      target_identity=target_identity or
                                      (snapshot.target_identity if snapshot else ""))
            entry = reservation.as_dict()
            entry["started_at_epoch"] = reservation.started_at
            journal.append(entry)
            # Keep every charge in the 24-hour window, and active-job charges
            # even beyond that window. Never truncate by a fixed item count.
            active = set(self.state.get("active", {}).values())
            self.state["journal"] = [e for e in journal if
                float(e.get("started_at_epoch") or 0) >= now - 86400
                or e.get("job_id") in active]
            if not self._save_locked():
                return None, DENY_JOB_LIMIT
            return reservation, DENY_OK

    def settle(self, reservation, sent, ok=False, note="", refund_unsent=True):
        """Close a reservation.

        * never sent            -> refunded by default (no HTTP happened, nothing charged)
        * sent and succeeded    -> charged
        * sent and failed/unknown -> charged and marked; the job must not be retried blindly
        """
        with self._lock:
            journal = self._journal_locked()
            for entry in reversed(journal):
                if (isinstance(entry, dict) and entry.get("job_id") == reservation.job_id
                        and entry.get("sequence") == reservation.sequence
                        and not entry.get("settled")):
                    entry["settled"] = "sent" if sent else "not_sent"
                    if not sent and refund_unsent:
                        entry["refunded"] = True
                    if note:
                        entry["note"] = str(note)[:60]
                    break
            self._save_locked()
            return reservation

    def entries_for(self, job_id):
        return [entry for entry in self._journal_locked()
                if isinstance(entry, dict) and entry.get("job_id") == job_id]

    # -- global switches ---------------------------------------------------
    def paused(self):
        return bool(self.state.get("paused"))

    def set_paused(self, paused, reason=""):
        with self._lock:
            self.state["paused"] = bool(paused)
            self.state["paused_reason"] = str(reason)[:80] if paused else ""
            self.state["paused_at"] = _utc(self.clock()) if paused else ""
            self._save_locked()
            return self.state["paused"]

    def profile_disabled(self, profile_id):
        return profile_id in (self.state.get("disabled_profiles") or [])

    def set_profile_disabled(self, profile_id, disabled):
        with self._lock:
            current = set(self.state.get("disabled_profiles") or [])
            if disabled:
                current.add(profile_id)
            else:
                current.discard(profile_id)
            self.state["disabled_profiles"] = sorted(current)
            self._save_locked()
            return sorted(current)

    def disabled_profiles(self):
        return list(self.state.get("disabled_profiles") or [])

    def status(self):
        """Safe summary for the UI: counts and switches, never a credential."""
        return {"paused": self.paused(), "paused_reason": self.state.get("paused_reason", ""),
                "disabled_profiles": self.disabled_profiles(),
                "spend_last_24h": self.spend_today(),
                "approvals": len([1 for record in self.state["approvals"].values()
                                  if isinstance(record, dict) and not record.get("revoked")]),
                "jobs": len(self.state["jobs"]), "policy_version": POLICY_VERSION}


def decide_send(ledger, snapshot, limits, kind="first", reserve=True):
    """The single gate every outbound call goes through.

    Returns (Reservation | None, code). `code` is a member of the fixed vocabulary; callers
    must refuse to send whenever it is not DENY_OK.

    With `reserve=False` the policy is checked without spending budget. That is what the
    pre-flight check uses: it must be able to report "this would be refused" before extraction
    without consuming the slot the real request is about to need.
    """
    if ledger.write_error or ledger.load_error:
        return None, DENY_NO_APPROVAL
    if ledger.paused():
        return None, DENY_PAUSED
    if snapshot is None:
        return None, DENY_NO_APPROVAL
    if ledger.profile_disabled(snapshot.profile_id):
        return None, DENY_PROFILE_DISABLED
    if not ledger.job_is_current(snapshot.paper_id, snapshot.job_id):
        return None, DENY_GENERATION
    approval = ledger.approval_for(snapshot.paper_id)
    if approval is None:
        return None, DENY_NO_APPROVAL
    if approval.get("target_identity") != snapshot.target_identity:
        return None, DENY_TARGET_CHANGED
    if approval.get("credential_ref", "") != snapshot.credential_ref:
        return None, DENY_CREDENTIAL_CHANGED
    if approval.get("approval_id") != snapshot.approval_id:
        return None, DENY_NO_APPROVAL
    if approval.get("model") != snapshot.model:
        return None, DENY_TARGET_CHANGED
    for name in ("config_revision", "credential_revision", "source_sha256", "credential_identity"):
        if approval.get(name) != getattr(snapshot, name):
            return None, DENY_CREDENTIAL_CHANGED
    if ledger.unknown_outcome(snapshot.job_id):
        return None, DENY_NO_APPROVAL
    if not reserve:
        return None, DENY_OK
    return ledger.reserve(snapshot, limits, kind=kind,
                          target_identity=snapshot.target_identity,
                          paper_id=snapshot.paper_id)


def code_message(code):
    return {
        DENY_PAUSED: secret_guard.message_for(secret_guard.E_PAUSED),
        DENY_NO_APPROVAL: secret_guard.message_for(secret_guard.E_CONSENT_REQUIRED),
        DENY_TARGET_CHANGED: "接口目标或凭据已变更，需要重新确认后再生成。",
        DENY_CREDENTIAL_CHANGED: "凭据引用已变更，需要重新确认后再生成。",
        DENY_PROFILE_DISABLED: "这个 API 已被停用；重新启用后才能生成。",
        DENY_JOB_LIMIT: "这篇论文的调用次数已用完（含修复与重试）。",
        DENY_DAY_LIMIT: "已达到 24 小时内的请求上限，本次未发送。",
        DENY_QUEUE_FULL: "队列已满，请稍后再试。",
        DENY_GENERATION: "这次任务已被更新的任务取代，旧结果不会覆盖新结果。",
    }.get(code, "本次请求未获批准，已停止。")


def limits_from_settings(ai_settings, fallback=None):
    """Read the limits block, filling gaps from the defaults."""
    limits = dict(fallback or DEFAULT_LIMITS)
    source = (ai_settings or {}).get("limits")
    if isinstance(source, dict):
        for key, (low, high) in LIMIT_RANGES.items():
            value = source.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and low <= value <= high:
                limits[key] = value
    for key in ("max_concurrent_jobs", "max_output_tokens"):
        value = (ai_settings or {}).get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            low, high = LIMIT_RANGES[key]
            if low <= value <= high:
                limits[key] = value
    return limits
