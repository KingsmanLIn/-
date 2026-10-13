"""Phase 0 audit: confirm or refute F01–F15 against the actual extracted 1.3.1 source.

Read-only. It prints file:line, identifiers and booleans only - never a credential value,
never an environment variable value. It also checks whether the working copy has drifted
from the shipped package, because the security work must start from a known baseline.

    python tests/security/phase0_audit.py
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

BASELINE = Path(r"D:\harness home\paper-reader-sec\baseline-1.3.1\tree\论文研读助手-本地版-1.3.1-全新安装")
WORKING = Path(r"D:\harness home\paper-reader")
SEC_FILES = ["schema.py", "providers.py", "analysis.py", "server.py", "storage.py",
             "reading_card.py", "quotes.py", "extraction.py", "launch.py",
             "static/app.js", "static/index.html"]


def read(root: Path, name: str) -> str:
    path = root / name
    return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""


def find(text: str, needle: str, label: str, hits: list, limit=4):
    for index, line in enumerate(text.splitlines(), start=1):
        if needle in line and len(hits) < limit:
            hits.append({"label": label, "line": index, "text": line.strip()[:150]})


def line_of(text: str, needle: str):
    for index, line in enumerate(text.splitlines(), start=1):
        if needle in line:
            return index
    return None


def blocks(text: str, count_of: str):
    return text.count(count_of)


report = {"baseline": str(BASELINE), "working": str(WORKING), "findings": {}, "drift": {}}

# ---------------------------------------------------------------- drift check
drift = {}
for name in SEC_FILES + ["vendor/pdfjs/pdf.min.mjs", "使用说明.md"]:
    base_file, work_file = BASELINE / name, WORKING / name
    if not base_file.is_file() and not work_file.is_file():
        continue
    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()[:12].upper() if path.is_file() else "(absent)"
    same = digest(base_file) == digest(work_file)
    drift[name] = {"same": same, "baseline": digest(base_file), "working": digest(work_file)}
report["drift"] = drift
report["drift_identical"] = [name for name, item in drift.items() if item["same"]]
report["drift_changed"] = [name for name, item in drift.items() if not item["same"]]

# ---------------------------------------------------------------- F01 / F02
schema = read(BASELINE, "schema.py")
providers = read(BASELINE, "providers.py")
server = read(BASELINE, "server.py")
storage = read(BASELINE, "storage.py")
analysis = read(BASELINE, "analysis.py")
card = read(BASELINE, "reading_card.py")
quotes = read(BASELINE, "quotes.py")
appjs = read(BASELINE, "static/app.js")
index_html = read(BASELINE, "static/index.html")

report["findings"]["F01_url_validation"] = {
    "schema_base_url_rule": [line.strip()[:120] for line in schema.splitlines()
                             if "base_url" in line][:6],
    "http_scheme_accepted": "startswith(('http://', 'https://'))" in schema
                            or "http://" in schema,
    "sends_as_is": "urllib.request.Request(url" in providers,
    "url_built_from": [line.strip()[:120] for line in providers.splitlines()
                       if "self.base_url +" in line or "url =" in line][:6],
    "mode_field_present": "cloud_https" in schema or "local_loopback" in schema,
    "userinfo_check": "userinfo" in providers or "userinfo" in schema,
    "query_rejected": "urlparse" in providers,
}

report["findings"]["F02_redirect_proxy"] = {
    "build_opener_used": "build_opener" in providers,
    "proxy_handler_present": "ProxyHandler" in providers,
    "redirect_disable_present": "HTTPRedirectHandler" in providers,
    "urlopen_direct": "urlopen(request" in providers,
    "tls_context_present": "ssl.SSLContext" in providers or "create_default_context" in providers,
    "verify_disabled": "CERT_NONE" in providers or "_create_unverified_context" in providers,
}

report["findings"]["F03_plaintext_credentials"] = {
    "credentials_path_attr": "credentials" in storage,
    "read_credentials_defined": "def read_credentials" in storage,
    "fallback_call_sites": [
        {"file": name, "line": line_of(read(BASELINE, name), "read_credentials")}
        for name in ("providers.py", "storage.py", "server.py", "analysis.py")
        if line_of(read(BASELINE, name), "read_credentials")
    ],
    "env_read_of_arbitrary_name": "os.environ.get(api_key_env" in providers,
    "plaintext_file_name": [line.strip()[:120] for line in storage.splitlines()
                            if "credentials.json" in line or 'credentials' in line][:4],
    "wincred_support": "CredRead" in providers or "wincred" in storage,
}

report["findings"]["F04_redaction"] = {
    "redact_patterns": [line.strip()[:130] for line in providers.splitlines()
                        if "re.compile" in line][:6],
    "upstream_body_read": "exc.read(400)" in providers or "exc.read(" in providers,
    "message_includes_body": "_first_line(redact(detail))" in providers
                             or "redact(detail)" in providers,
    "fixed_error_codes_present": bool(re.search(r"CATEGORY_\w+ = \"", providers)),
}

report["findings"]["F05_raw_model_diagnostics"] = {
    "raw_model_output_written": "raw_model_output" in analysis,
    "raw_repair_output_written": "raw_repair_output" in analysis,
    "failed_file_name": "failed.json" in storage or "failed_answer" in storage,
    "field_lines": [line_of(analysis, "raw_model_output"), line_of(analysis, "raw_repair_output")],
}

report["findings"]["F06_config_race"] = {
    "settings_reads_in_pipeline": [
        {"line": line_of(analysis, "self.ai_settings()")},
        {"count": analysis.count("self.ai_settings()")},
    ],
    "job_identity_fields": [line.strip()[:120] for line in analysis.splitlines()
                            if "job_id" in line][:4],
    "config_revision_present": "config_revision" in analysis,
    "credential_revision_present": "credential_revision" in analysis,
    "generation_present": "generation" in analysis,
}

report["findings"]["F07_restore_authorizes"] = {
    "recover_call_sites": [{"file": name, "line": line_of(read(BASELINE, name), "recover()")}
                           for name in ("server.py", "launch.py", "analysis.py")
                           if line_of(read(BASELINE, name), "recover()")],
    "restore_sets_status": [line.strip()[:120] for line in server.splitlines()
                            if "restore" in line.lower()][:6],
    "ledger_present": "ledger" in analysis or "ledger" in storage,
    "awaiting_consent_state": "awaiting_consent" in analysis or "awaiting_consent" in schema,
}

report["findings"]["F08_prompt_injection"] = {
    "system_prompt_has_untrusted_note": "不可信" in card or "待分析数据" in card,
    "doc_markers": [line.strip()[:110] for line in card.splitlines()
                    if "PDF 第" in line][:3],
    "tool_definitions_present": "tool" in providers.lower() and "tools" in providers,
}

report["findings"]["F09_budget"] = {
    "max_tokens_field": "max_tokens" in providers,
    "output_cap_in_request": "max_output_tokens" in providers or "num_predict" in providers,
    "attempts_field": [line.strip()[:110] for line in analysis.splitlines()
                       if "max_attempts" in line or "MAX_ATTEMPTS" in line][:3],
    "retry_on_timeout": "TRANSIENT_CATEGORIES" in analysis,
    "daily_quota": "daily_request_limit" in schema or "daily" in analysis,
}

report["findings"]["F10_cancel"] = {
    "cancel_checks": [line.strip()[:110] for line in analysis.splitlines()
                      if "cancel.is_set()" in line][:6],
    "cancel_count": analysis.count("cancel.is_set()"),
    "connection_close_attempt": "close()" in providers,
}

report["findings"]["F11_cache_headers"] = {
    "no_store_in_server": "no-store" in server,
    "cache_control_lines": [{"line": line_of(server, needle), "needle": needle}
                            for needle in ("no-store", "Cache-Control")]
                            if "no-store" in server else "absent",
    "index_html_injects_token": "__BOOTSTRAP__" in index_html or "boot" in index_html,
    "path_cached_by_self_path": "self.path" in server and "no-store" not in server,
}

report["findings"]["F12_preauth_body"] = {
    "content_length_read_before_auth": [
        {"line": line_of(server, "Content-Length")},
        {"auth_line": line_of(server, "def authorized")},
    ],
    "max_json_limits": [line.strip()[:110] for line in server.splitlines()
                        if "MAX_JSON" in line or "MAX_BODY" in line or "64 * 1024" in line][:4],
    "max_pdf_limit": [line.strip()[:110] for line in server.splitlines()
                      if "MAX_PDF" in line][:2],
}

report["findings"]["F13_restore_validation"] = {
    "restore_revalidates_hash": "source_sha256" in server and "restore" in server,
    "restore_parses_json": "json.loads" in server,
    "cache_trust": [line.strip()[:110] for line in read(BASELINE, "extraction.py").splitlines()
                    if "source_sha256" in line][:3],
}

report["findings"]["F14_first_generate"] = {
    "app_js_generate_calls": [{"line": line_of(appjs, needle), "needle": needle}
                              for needle in ("analysis/'+id+'/generate'", "analysis-run")]
                              if line_of(appjs, "generate") else "absent",
    "force_default_in_server": [line.strip()[:120] for line in server.splitlines()
                                if "force" in line][:6],
    "waiting_for_user_enqueue": [line.strip()[:130] for line in analysis.splitlines()
                                 if "waiting_for_user" in line][:6],
}

report["findings"]["F15_quote_fuzzy"] = {
    "min_similarity": [line.strip()[:110] for line in quotes.splitlines()
                       if "0.72" in line or "MIN_" in line][:4],
    "prefix_score": "prefix" in quotes or "_partial_score" in quotes,
    "verified_flag": [line.strip()[:110] for line in card.splitlines()
                      if "verified" in line][:4],
    "fuzzy_marks_verified": "similarity" in card,
}

print(json.dumps(report, ensure_ascii=False, indent=2)[:12000])
out = Path(__file__).resolve().parent / "phase0-audit.json"
out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print("\nwritten:", out)
print("drift changed files:", report["drift_changed"])
