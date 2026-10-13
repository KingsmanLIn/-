"""Backup and cache validation: nothing from a ZIP is trusted until it is re-checked.

A backup is a sensitive document bundle that travels between machines. It is also an input an
attacker can craft. So on restore this module

* validates the whole archive before anything is written: member count, per-member and total
  size, compression ratio (a zip bomb), path shape, duplicate members, links and reparse
  points, and the presence of the members the format requires;
* re-extracts every PDF in a key-free restricted subprocess and rebuilds the text cache from
  that extraction instead of trusting the cached copy;
* re-validates every imported answer against the freshly extracted text: schema version,
  prompt version, source hash, page range, field set, quote verification and metadata that
  the server owns (paper_id, provider, model and file paths are overwritten, never trusted);
* refuses to carry credentials, approvals or queued jobs out of a backup.

Nothing here is a signature check: a passing archive proves internal consistency, not
authorship. Recovered answers are labelled `imported` so the reader can tell them apart from
something this installation generated.
"""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import PurePosixPath

import pdftext
import quotes as quotes_module
import reading_card
import schema
import secret_guard

# Archive limits, applied before any member is read into memory.
MAX_MEMBERS = 5000
MAX_TOTAL_BYTES = 500 * 1024 * 1024          # uncompressed, summed over all members
MAX_MEMBER_BYTES = 200 * 1024 * 1024
MAX_RATIO = 400                              # declared compression ratio ceiling
MAX_LIBRARY_BYTES = 64 * 1024 * 1024
MAX_ANSWER_BYTES = 8 * 1024 * 1024
MAX_EXTRACTED_BYTES = 64 * 1024 * 1024

# Members that must never leave a backup, whatever they claim to be.
FORBIDDEN_PREFIXES = ("credentials", "approvals/", "jobs/", "job-ledger", "run/", "logs/",
                      "diagnostics/", "sessions/", "tokens/", "localStorage/")
FORBIDDEN_NAMES = ("credentials.json", "credentials.legacy.json", "job-ledger.json",
                   "instance.lock", "server.json", "outbound.json", "run-instance.json")


class BackupError(ValueError):
    """A backup refused on safety grounds. The message is safe to show."""


def _member_is_link(info):
    """True for a symlink/hardlink entry or a Windows reparse point."""
    if (info.external_attr >> 16) & 0o170000 == 0o120000:       # S_IFLNK
        return True
    if info.create_system == 0 and (info.external_attr & 0x400):  # FILE_ATTRIBUTE_REPARSE
        return True
    return False


def inspect_archive(data, *, require_library=True):
    """Validate an archive and return (members, warnings) without extracting to disk.

    Raises BackupError for anything that makes the archive unsafe to unpack. The checks run
    over the central directory only - no member content is read here.
    """
    warnings = []
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError):
        raise BackupError("这个文件不是有效的 ZIP 备份")
    with archive:
        infos = archive.infolist()
        if not infos:
            raise BackupError("备份是空的")
        if len(infos) > MAX_MEMBERS:
            raise BackupError(f"备份成员过多（{len(infos)} > {MAX_MEMBERS}）")
        names, total, library_seen = {}, 0, False
        for info in infos:
            name = info.filename
            if name in names:
                # A duplicate member means two different payloads for one path; which one a
                # reader gets depends on the unzip implementation.
                raise BackupError(f"备份里有重复成员：{name[:60]}")
            names[name] = info
            if info.is_dir():
                continue
            if _member_is_link(info):
                # a link or reparse point is never followed: the member is skipped
                warnings.append({"code": "skipped_link", "member": name[:60]})
                continue
            pure = PurePosixPath(name.replace("\\", "/"))
            unsafe_path = (pure.is_absolute() or ".." in pure.parts or name.startswith("/")
                           or (len(name) > 1 and name[1] == ":") or ":" in pure.parts[0])
            if unsafe_path:
                # The member is ignored, never written: a crafted name must not be able to
                # place a file outside the library.
                warnings.append({"code": "skipped_path", "member": name[:60]})
                continue
            if info.file_size > MAX_MEMBER_BYTES:
                raise BackupError(f"备份成员过大：{name[:60]}")
            total += info.file_size
            if total > MAX_TOTAL_BYTES:
                raise BackupError("备份解压后总大小超过上限")
            if info.compress_size > 0 and info.file_size / info.compress_size > MAX_RATIO:
                raise BackupError(f"备份成员压缩比异常（可能是压缩炸弹）：{name[:60]}")
            lowered = name.lower()
            base = pure.name.lower()
            if base in FORBIDDEN_NAMES or lowered.startswith(FORBIDDEN_PREFIXES):
                # Never carry credentials, approvals, job state or logs out of a backup. The
                # member is skipped rather than fatal, so an older archive still restores.
                warnings.append({"code": "excluded_member", "member": name[:60]})
                continue
            if not (name == "library.json" or name.startswith(("papers/", "analyses/",
                                                              "extracted/", "notes/"))):
                # Unknown members are not written anywhere: the restore only reads the
                # members the format defines.
                warnings.append({"code": "skipped_unknown", "member": name[:60]})
                continue
            if name == "library.json":
                library_seen = True
        if require_library and not library_seen:
            raise BackupError("备份里没有 library.json")
    return names, warnings


def extract_text(pdf_path, paper_id="", extractor_factory=None):
    """Re-extract a PDF with the cache bypassed.

    A restore must not reuse the archive's cached text: the cache is exactly the part an
    attacker controls, and a self-declared hash proves nothing about the PDF beside it.
    """
    if extractor_factory is None:
        from extraction import IsolatedPdfTextExtractor
        extractor_factory = lambda: IsolatedPdfTextExtractor(allow_cache=False)   # noqa: E731
    extractor = extractor_factory()
    result = extractor.extract(pdf_path, paper_id=paper_id, use_cache=False)
    return result


def verify_analysis(payload, *, paper_id, source_sha256, page_index, page_count,
                    prompt_version=reading_card.PROMPT_VERSION, extracted_ok=True):
    """Re-check an imported answer against the paper that is actually here.

    Returns (record | None, report). A record is safe to write and to show, tagged
    `source=imported`; `None` means the answer could not be substantiated and must not be
    presented as a checked reference.
    """
    report = {"paper_id": paper_id, "ok": False, "codes": []}
    if not isinstance(payload, dict):
        report["codes"].append("not_an_object")
        return None, report
    declared = payload.get("schema_version")
    if declared is not None and declared != reading_card.SCHEMA_VERSION:
        # A different answer schema is not something this release can re-verify.
        report["codes"].append("schema_version")
        return None, report
    declared_hash = str(payload.get("source_sha256") or "")
    if declared_hash and declared_hash != str(source_sha256 or ""):
        # The PDF in the archive is not the PDF this answer was made from: it does not
        # describe the paper that is here, so it is refused.
        report["codes"].append("source_mismatch")
        return None, report
    declared_prompt = str(payload.get("prompt_version") or "")
    if declared_prompt and declared_prompt != str(prompt_version):
        # A different prompt version: keep the text, refuse the claim. The record is tagged
        # unverified and read-only so the reader can still see their own material.
        legacy = _legacy_record(payload, paper_id, source_sha256)
        if legacy is None:
            report["codes"].append("prompt_version")
            return None, report
        report["ok"] = False
        report["codes"].append("prompt_version")
        report["legacy"] = True
        report["declared_prompt"] = declared_prompt[:32]
        return legacy, report
    if not extracted_ok or not page_index.alive():
        report["codes"].append("no_text_layer")
        return None, report

    answers = payload.get("answers")
    if not isinstance(answers, dict) or set(answers) - set(reading_card.FIELD_KEYS):
        report["codes"].append("answer_shape")
        return None, report

    # Re-run the real validator over a rebuilt payload: quote verification, page ranges,
    # field completeness and the causal rules are all applied again to the stored JSON.
    # A stored entry keeps the page the model claimed beside the corrected one, so the claim
    # is what goes back in: re-validating then reproduces the same corrected record rather
    # than silently rewriting history. `verified` and `similarity` are recomputed, never
    # taken from the file.
    def _evidence(items):
        out = []
        for entry in items or []:
            if not isinstance(entry, dict):
                continue
            out.append({"page": entry.get("claimed_page") or entry.get("page"),
                        "quote": entry.get("quote")})
        return out

    rebuilt = {
        "schema_version": reading_card.SCHEMA_VERSION,
        "overall": payload.get("overall") or {},
        "answers": {key: {"status": item.get("status"), "answer": item.get("answer"),
                          "evidence": _evidence(item.get("evidence")),
                          "confidence": item.get("confidence"),
                          "causal_strength": item.get("causal_strength")}
                    for key, item in answers.items() if isinstance(item, dict)},
        "terms": payload.get("terms") or [],
        "reader_questions": payload.get("reader_questions") or [],
        "warnings": payload.get("warnings") or [],
    }
    try:
        record = reading_card.validate_reading_card(
            rebuilt, page_index, page_count, paper_id=paper_id,
            prompt_version=prompt_version, provider="", model="",
            language=str((payload.get("overall") or {}).get("language") or ""),
            source_sha256=source_sha256, created_at=str(payload.get("created_at") or ""),
            strict=True, require_evidence=True)
    except reading_card.ValidationError as exc:
        report["codes"] = list(getattr(exc, "codes", []) or ["invalid"])[:8]
        return None, report

    # Server-owned metadata is overwritten, never taken from the file.
    record["source"] = "imported"
    record["imported_at"] = record.get("created_at", "")
    record["provider"] = ""
    record["model"] = ""
    record["origin"] = {"provider": str(payload.get("provider") or "")[:40],
                        "model": str(payload.get("model") or "")[:80],
                        "generated_at": str(payload.get("generated_at") or "")[:40]}
    report["ok"] = True
    report["verified_quotes"] = record["verification"]["verified_quotes"]
    report["claimed_quotes"] = record["verification"]["claimed_quotes"]
    return record, report



def _legacy_record(payload, paper_id, source_sha256):
    """Shape an older answer file into a displayable, explicitly unverified record.

    Only the parts the format actually has are carried over, every value is length-bounded,
    and the citations are shown exactly as they were stored - marked unverified rather than
    re-checked, because this release cannot re-check them against a different prompt.
    """
    fields = payload.get("fields")
    if not isinstance(fields, dict) or not fields:
        return None
    if set(fields) - set(reading_card.FIELD_KEYS):
        return None

    def text_of(item):
        """A v1 field entry is either a plain string or {text, quote, page, not_found}."""
        if isinstance(item, str):
            return item.strip()[:4000]
        if isinstance(item, dict):
            value = item.get("text")
            return value.strip()[:4000] if isinstance(value, str) else ""
        return ""

    def quotes_of(item):
        """Unverified candidates: the recorded page and quote, never re-checked."""
        if not isinstance(item, dict):
            return []
        quote = item.get("quote")
        page = item.get("page")
        if not isinstance(quote, str) or not quote.strip():
            return []
        return [{"page": page if isinstance(page, int) and not isinstance(page, bool) else None,
                 "quote": quote.strip()[:200], "verified": False,
                 "match_kind": "unverified"}]

    answers = {}
    for key in reading_card.FIELD_KEYS:
        raw = fields.get(key)
        answer = text_of(raw)
        not_found = bool(raw.get("not_found")) if isinstance(raw, dict) else False
        if not answer or not_found:
            answers[key] = {"status": "not_found", "answer": answer or reading_card.NOT_FOUND_TEXT,
                            "evidence": [], "confidence": "low",
                            "causal_strength": "unclear", "verified_quotes": 0}
        else:
            answers[key] = {"status": "partial", "answer": answer,
                            "evidence": quotes_of(raw), "confidence": "low",
                            "causal_strength": "unclear", "verified_quotes": 0}
    return {
        "schema": reading_card.SCHEMA_VERSION,
        "schema_version": reading_card.SCHEMA_VERSION,
        "paper_id": paper_id,
        "prompt_version": str(payload.get("prompt_version") or "")[:32],
        "provider": "", "model": "",
        "language": "zh",
        "source_sha256": source_sha256,
        "generated_at": str(payload.get("created_at") or "")[:40],
        "created_at": str(payload.get("created_at") or "")[:40],
        "overall": {"title": "", "language": "zh", "paper_type": "", "main_claim": "",
                    "conclusion": "", "confidence": "low"},
        "answers": answers,
        "terms": [], "reader_questions": [], "warnings": [],
        "verification": {"claimed_quotes": 0, "verified_quotes": 0, "approximate_quotes": 0,
                         "corrected_pages": 0, "corrections": [], "page_count": 0,
                         "field_count": len(answers),
                         "not_found_fields": [key for key, item in answers.items()
                                              if item["status"] == "not_found"],
                         "evidence_pages": []},
        "input": {},
        "source": "imported",
        "verified": False,
        "import_note": "来自旧版本备份，未按当前规则重新核对，仅供参考",
    }


def rebuild_cache(pdf_path, paper_id, extractor_factory=None):
    """Rebuild the text cache from the PDF itself.

    The cached copy in the archive is dropped, not compared: a cache is an optimisation, and
    one that carries its own hash claim proves nothing about the PDF next to it.
    """
    result = extract_text(pdf_path, paper_id=paper_id, extractor_factory=extractor_factory)
    if not getattr(result, "ok", False):
        return None, {"code": getattr(result, "code", "") or "no_text_layer", "pages": 0,
                      "chars": 0, "message": str(getattr(result, "message", ""))[:120]}, result
    payload = result.as_cache_payload(_utc())
    return payload, {"code": "", "pages": len(getattr(result, "pages", []) or []),
                     "chars": result.total_chars, "backend": result.backend}, result


def _utc():
    import time
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def page_index_from(result):
    pages = getattr(result, "pages", []) or []
    return quotes_module.PageTextIndex(pages), (getattr(result, "page_count", 0) or 0)


def scan_state_for_forbidden(state):
    """Codes for anything a restored library must not contain."""
    codes = []
    text = json.dumps(state, ensure_ascii=False)
    for marker in ("api_key", "credential_ref", "api_key_env", "session_token", "csrf",
                   "approval_id", "budget_id", "job_id"):
        if marker in text:
            codes.append(marker)
    return codes


def safe_summary(report):
    """A restore report with no paper text and no credential in it."""
    return secret_guard.scrub(report)
