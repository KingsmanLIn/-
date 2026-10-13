"""Re-run every audit probe and summarise the re-review result."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"
AUDITS = ["audit_fields.py", "audit_ai_safety.py", "audit_provider_http.py",
          "audit_reader_visual.py", "audit_legibility.py", "audit_safety_http.py",
          "audit_stale_paths.py", "audit_scan_path.py", "audit_integrity.py"]


def run(script):
    proc = subprocess.run([sys.executable, str(TESTS / script)], cwd=str(ROOT),
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=900)
    return proc.stdout


summary = {}
for script in AUDITS:
    out = run(script)
    try:
        summary[script] = json.loads(out)
    except json.JSONDecodeError:
        summary[script] = {"raw": out[:400]}
    print(f"--- {script} ---")
    print(out[:400].encode("ascii", "replace").decode("ascii"))

# the interesting answers, collected
fields = summary.get("audit_fields.py", {})
ai = summary.get("audit_ai_safety.py", {})
http = summary.get("audit_provider_http.py", {})
visual = summary.get("audit_reader_visual.py", {})
legib = summary.get("audit_legibility.py", {})
safety = summary.get("audit_safety_http.py", {})
stale = summary.get("audit_stale_paths.py", {})
integ = summary.get("audit_integrity.py", {})

report = {
    "fields_all_layers_match": fields.get("verdict"),
    "write_sites": {"fields": [w["line"] for w in ai.get("field_write_sites", [])],
                    "steps": [w["line"] for w in ai.get("step_write_sites", [])],
                    "annotations": [w["line"] for w in ai.get("annotation_write_sites", [])]},
    "ai_first_run": ai.get("first_run"),
    "ai_recover_after_ready": ai.get("recover_after_ready"),
    "disabled_adapter": ai.get("disabled_adapter_is_used"),
    "no_provider_run": ai.get("run_without_provider"),
    "events_have_paper_text": ai.get("event_log_contains_paper_text"),
    "ollama_ok": {k: http.get("ollama_ok", {}).get(k) for k in ("status", "error_code")},
    "ollama_answer": http.get("ollama_ok", {}).get("answer"),
    "openai_ok": {k: http.get("openai_compatible_ok", {}).get(k) for k in ("status", "error_code")},
    "ollama_500": {k: http.get("ollama_http_500", {}).get(k)
                   for k in ("status", "error_code", "error_message")},
    "wheel_lock": visual.get("wheelLock"),
    "scroll_during_paging": visual.get("scrollDuringPaging"),
    "rapid": visual.get("rapidDuringLock"),
    "resize": {"layer_matches": visual.get("layerMatchesCanvas"),
               "page": visual.get("pageNumberIsDisplayedPage")},
    "legibility": {paper: {size: {"medianFontPx": data.get("medianFontPx"),
                                  "scale": data.get("scaleFactor"),
                                  "wholePage": data.get("pageVisible")}
                           for size, data in sizes.items() if isinstance(data, dict)}
                   for paper, sizes in legib.get("papers", {}).items()},
    "auth_failure": {k: v for k, v in safety.get("auth_failure", {}).items()
                     if k not in ("events",)},
    "backup": safety.get("backup"),
    "stale": {"restart_same_model": stale.get("restart_same_model"),
              "answer_file_deleted": stale.get("answer_file_deleted_after_recover"),
              "ghost_file": stale.get("library_points_at_missing_file")},
    "integrity": integ,
}
(TESTS / "audit-recheck.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                          encoding="utf-8")
print("\n==== recheck summary written ====")
print("written:", TESTS / "audit-recheck.json")
