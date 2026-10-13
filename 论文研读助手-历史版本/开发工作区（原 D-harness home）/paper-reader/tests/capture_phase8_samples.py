"""Produce the phase 8 sample analysis files: one Chinese and one English paper.

The card structure, quotes, page numbers and verification block are real: the quotes are
the papers' own text-layer lines, and the files are written by the shipped validator and
storage layer. The answer sentences come from the test fixture rather than from a language
model, so the samples document the format (and prove every field is page-traceable), not
the wording of any particular model.

Regenerate with a real local model by removing the FakeProvider from
`serve_with_fake_provider.py` and pointing the app at Ollama or LM Studio instead.

    python tests/capture_phase8_samples.py
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

OUT_DIR = Path(__file__).resolve().parent / "samples"
PAPERS = [
    ("medical-2026-zang", "zh", "中文实证论文：分级诊疗制度下的医疗控费机制"),
    ("fertility-2026-coffey", "en", "English empirical paper: Does Fertility Stabilize At Low Levels?"),
]


class FixedExtractor:
    """Extractor factory that serves one paper's captured pages."""

    def __init__(self, pages):
        self.pages = pages

    def __call__(self):
        return self

    def extract(self, path, paper_id=None, use_cache=None):
        pages = self.pages
        return type("R", (), {"ok": True, "code": "ok", "message": "", "page_count": len(pages),
                              "pages": pages, "backend": "sample", "text_pages": pages,
                              "source_sha256": "0" * 64, "cached": False})()

    def chunks(self, result, max_chars=6000, overlap_ratio=0.1, cleaned=False, document_id=""):
        from chunker import Chunker
        return Chunker(max_chars=max_chars).chunk(self.pages, document_id=document_id)


def produce(paper_id, language):
    pages = extraction.PdfTextExtractor(
        files=DataStore(Path(tempfile.mkdtemp(prefix="phase8-sample-"))).ensure_directories(),
        prefer=["builtin"]).extract(ROOT / "data" / "papers" / f"{paper_id}.pdf",
                                    paper_id=paper_id).pages
    card = build_card(pages, language=language)

    tmp = Path(tempfile.mkdtemp(prefix="phase8-sample-run-"))
    fresh_library_v1(tmp)
    store = server.Store(tmp)
    store.update_ai_settings({"provider": "ollama", "model": "sample-model", "auto_analyze": False})
    fake = providers.FakeProvider(script=["success"], payload=card)
    manager = analysis.AnalysisManager(store, provider_factory=lambda settings: fake,
                                       extractor_factory=FixedExtractor(pages),
                                       log=lambda record: None)
    manager.enqueue(paper_id, reason="manual", force=True)
    manager.process_once()
    meta = store.analysis_meta(paper_id)
    payload, error = store.analysis_answer(paper_id)
    result = {"status": meta["status"], "error": error, "pages": pages,
              "answer": payload, "meta": meta}
    shutil.rmtree(tmp, ignore_errors=True)
    return result


def main():
    OUT_DIR.mkdir(exist_ok=True)
    index = []
    for paper_id, language, description in PAPERS:
        outcome = produce(paper_id, language)
        if outcome["status"] != "ready":
            raise SystemExit(f"{paper_id}: analysis did not validate ({outcome['error']})")
        answer = outcome["answer"]
        target = OUT_DIR / f"{paper_id}.json"
        target.write_text(json.dumps(answer, ensure_ascii=False, indent=2), encoding="utf-8")

        trace = {}
        for key, item in answer["answers"].items():
            trace[key] = {"status": item["status"], "confidence": item["confidence"],
                          "pages": [entry["page"] for entry in item["evidence"]],
                          "quotes": [entry["quote"] for entry in item["evidence"]]}
        index.append({
            "paper_id": paper_id,
            "language": language,
            "description": description,
            "file": target.name,
            "page_count": answer["verification"]["page_count"],
            "fields": len(answer["answers"]),
            "verified_quotes": answer["verification"]["verified_quotes"],
            "corrected_pages": answer["verification"]["corrected_pages"],
            "not_found_fields": answer["verification"]["not_found_fields"],
            "pages_per_field": {key: value["pages"] for key, value in trace.items()},
            "citations": trace,
        })
        print(f"{paper_id}: {target.name} ({target.stat().st_size} bytes), "
              f"{answer['verification']['verified_quotes']} verified quotes, "
              f"pages {sorted({p for value in trace.values() for p in value['pages']})}")

    (OUT_DIR / "README.md").write_text(
        "# 阶段 8 样例分析文件\n\n"
        "这两个 JSON 由 `python tests/capture_phase8_samples.py` 生成，格式与落盘的\n"
        "`data/analyses/<paper_id>.json` 完全一致（同一个 `reading_card` 校验器与 `storage` 写入器）。\n\n"
        "**说明**：引文、页码与 `verification` 区块都是真实的——引文取自论文自己的文本层，\n"
        "并逐条通过引文核对；答案文字来自测试夹具，用来演示字段格式与页码可追溯性，\n"
        "不代表任何具体模型的措辞。要得到真实模型的措辞，请把本机 Ollama / LM Studio\n"
        "接入后在界面上点击“查看 AI 参考”。\n\n"
        "字段级页码索引见 `index.json`。\n",
        encoding="utf-8")
    (OUT_DIR / "index.json").write_text(
        json.dumps({"prompt_version": reading_card.PROMPT_VERSION,
                    "schema_version": reading_card.SCHEMA_VERSION,
                    "generated_by": "tests/capture_phase8_samples.py",
                    "papers": index}, ensure_ascii=False, indent=2), encoding="utf-8")
    print("written:", OUT_DIR)


if __name__ == "__main__":
    main()
