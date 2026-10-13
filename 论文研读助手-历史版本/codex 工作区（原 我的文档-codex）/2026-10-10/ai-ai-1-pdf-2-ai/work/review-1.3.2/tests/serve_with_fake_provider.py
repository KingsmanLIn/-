"""Run the real server with a scriptable fake provider, for HTTP client checks.

Used by tests/api_smoke.py; nothing here is part of the shipped app.

    python tests/serve_with_fake_provider.py --port 8799 --data-dir <copy> --mode ok|fail
"""
from __future__ import annotations

import argparse
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import analysis      # noqa: E402
import providers     # noqa: E402
import server        # noqa: E402
from card_fixtures import build_card  # noqa: E402


def card_for(paper_id):
    import extraction
    from storage import DataStore
    directory = Path(__file__).resolve().parent / "fixtures" / "card-cache"
    extractor = extraction.PdfTextExtractor(
        files=DataStore(directory).ensure_directories(), prefer=["builtin"])
    result = extractor.extract(ROOT / "data" / "papers" / f"{paper_id}.pdf", paper_id=paper_id)
    return build_card(result.pages)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8799)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--mode", choices=("ok", "fail", "slow"), default="ok")
    parser.add_argument("--delay", type=float, default=0.0)
    args = parser.parse_args()

    store = server.Store(args.data_dir)
    store.update_ai_settings({"provider": "ollama", "model": "fake-model", "auto_analyze": False})
    payload = card_for("medical-2026-zang")
    if args.mode == "fail":
        payload = {"schema_version": 1, "overall": {}, "answers": {}, "terms": [],
                   "reader_questions": [], "warnings": []}
    fake = providers.FakeProvider(script=["success"], payload=payload, delay=args.delay)
    log = []
    manager = analysis.AnalysisManager(store, provider_factory=lambda settings: fake,
                                       log=log.append)
    manager.recover()
    manager.start()

    httpd = server.ThreadingHTTPServer(("127.0.0.1", args.port),
                                       server.handler_for(store, "smoke-token", manager))
    httpd.daemon_threads = True
    print(f"http://127.0.0.1:{args.port}", flush=True)
    try:
        httpd.serve_forever()
    finally:
        manager.stop()
        httpd.server_close()


if __name__ == "__main__":
    main()
