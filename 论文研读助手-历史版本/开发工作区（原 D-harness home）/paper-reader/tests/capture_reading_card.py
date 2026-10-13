"""Phase 4 evidence: capture a real validated card and a real failure log.

Both are produced with FakeProvider against a bundled paper, so the samples show the
exact artefacts the app writes - no hand-written JSON.

    python tests/capture_reading_card.py
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from baseline_helpers import fresh_library_v1  # noqa: E402
import analysis      # noqa: E402
import extraction    # noqa: E402
import providers     # noqa: E402
import reading_card  # noqa: E402
import server        # noqa: E402
from card_fixtures import build_card  # noqa: E402
from storage import DataStore  # noqa: E402

PAPER = "medical-2026-zang"
PDF = ROOT / "data" / "papers" / f"{PAPER}.pdf"
OUT_DIR = ROOT / "tests"
report = {"prompt_version": reading_card.PROMPT_VERSION,
          "schema_version": reading_card.SCHEMA_VERSION,
          "error_codes": list(reading_card.ERROR_CODES),
          "limits": {"max_answer_chars": reading_card.MAX_ANSWER_CHARS,
                     "max_quote_chars": reading_card.MAX_QUOTE_CHARS,
                     "min_quote_chars": reading_card.MIN_QUOTE_CHARS,
                     "max_evidence_per_answer": reading_card.MAX_EVIDENCE_PER_ANSWER,
                     "min_verified_quotes": reading_card.MIN_VERIFIED_QUOTES,
                     "min_verified_ratio": reading_card.MIN_VERIFIED_RATIO,
                     "max_terms": reading_card.MAX_TERMS,
                     "max_questions": reading_card.MAX_QUESTIONS}}

pages = extraction.PdfTextExtractor(
    files=DataStore(Path(tempfile.mkdtemp(prefix="card-capture-"))).ensure_directories(),
    prefer=["builtin"]).extract(PDF, paper_id=PAPER).pages
report["source_pages"] = len(pages)

valid_card = build_card(pages)


class FixedExtractor:
    """Extractor factory returning the pages captured above."""

    def __call__(self):
        return self

    def extract(self, path, paper_id=None, use_cache=None):
        return type("R", (), {"ok": True, "code": "ok", "message": "", "page_count": len(pages),
                              "pages": pages, "backend": "capture", "text_pages": pages,
                              "source_sha256": "0" * 64, "cached": False})()

    def chunks(self, result, max_chars=6000, overlap_ratio=0.1, cleaned=False, document_id=""):
        from chunker import Chunker
        return Chunker(max_chars=max_chars).chunk(pages, document_id=document_id)


def run(payloads, script):
    tmp = Path(tempfile.mkdtemp(prefix="card-capture-run-"))
    fresh_library_v1(tmp)
    store = server.Store(tmp)
    store.update_ai_settings({"provider": "ollama", "model": "capture-model", "auto_analyze": False})
    fake = providers.FakeProvider(script=list(script), payloads=list(payloads))
    manager = analysis.AnalysisManager(store, provider_factory=lambda settings: fake,
                                       extractor_factory=FixedExtractor(), log=lambda record: None)
    manager.enqueue(PAPER, reason="manual", force=True)
    manager.process_once()
    meta = store.analysis_meta(PAPER)
    answer, error = store.analysis_answer(PAPER)
    failed = None
    if store.files.has_failed_answer(PAPER):
        failed = json.loads(store.files.failed_answer_path(PAPER).read_text(encoding="utf-8"))
    result = {"status": meta["status"], "stage": meta["stage"], "progress": meta["progress"],
              "message": meta["message"], "error_code": meta["error_code"],
              "answer": answer, "answer_error": error, "failed_log": failed,
              "provider_calls": len(fake.calls)}
    shutil.rmtree(tmp, ignore_errors=True)
    return result


# 1. a valid card -> ready
success = run([valid_card], ["success"])
report["success"] = {"status": success["status"], "message": success["message"],
                     "provider_calls": success["provider_calls"],
                     "verification": (success["answer"] or {}).get("verification"),
                     "answers": (success["answer"] or {}).get("answers"),
                     "overall": (success["answer"] or {}).get("overall"),
                     "created_at": (success["answer"] or {}).get("created_at"),
                     "source_sha256": (success["answer"] or {}).get("source_sha256"),
                     "model": (success["answer"] or {}).get("model")}

# 2. forged quote + causal overstatement twice -> failed with a log
def broken_card():
    card = build_card(pages)

    def mutate(card):
        card["answers"]["evidence"]["evidence"] = [{"page": 1,
                                                    "quote": "本文证明分级诊疗在全国降低了费用"}]
        card["answers"]["claim"]["answer"] = "分级诊疗导致了费用下降"
        card["answers"]["claim"]["causal_strength"] = "causal"
    mutate(card)
    return card


failure = run([broken_card(), broken_card()], ["success", "success"])
report["failure"] = {"status": failure["status"], "message": failure["message"],
                     "error_code": failure["error_code"],
                     "provider_calls": failure["provider_calls"],
                     "answer_file_exists": failure["answer"] is not None,
                     "failed_log": failure["failed_log"]}

# 3. one repair attempt: broken then repaired -> ready
broken_then_ok = build_card(pages)
broken_then_ok = json.loads(json.dumps(broken_then_ok))
repaired = build_card(pages)
missing = json.loads(json.dumps(build_card(pages)))
del missing["answers"]["next"]
repair_run = run([missing, repaired], ["success", "success"])
report["repair"] = {"status": repair_run["status"], "message": repair_run["message"],
                    "provider_calls": repair_run["provider_calls"],
                    "verified_quotes": (repair_run["answer"] or {}).get(
                        "verification", {}).get("verified_quotes")}

report["_meta"] = {"capturedBy": "tests/capture_reading_card.py",
                   "note": "FakeProvider only; quotes come from the bundled paper's real "
                           "text layer, the library is a temporary copy."}

out = OUT_DIR / "phase4-reading-card-sample.json"
out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
summary = {key: report[key]["status"] if isinstance(report.get(key), dict) else value
           for key in ("success", "failure", "repair")}
print("written:", out)
print("statuses:", json.dumps(summary, ensure_ascii=False))
print("success verification:", json.dumps(report["success"]["verification"], ensure_ascii=False))
print("failure codes:", json.dumps(report["failure"]["failed_log"]["errors"][0]["codes"],
                                   ensure_ascii=False) if report["failure"]["failed_log"] else None)
print("failure raw text:", (report["failure"]["failed_log"] or {}).get("raw_model_output", "")[:60])
