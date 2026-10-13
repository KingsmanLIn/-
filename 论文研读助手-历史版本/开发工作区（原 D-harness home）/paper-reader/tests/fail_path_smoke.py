"""Curl checks for the failure path: status summary only, answer withheld, retry works.

    python tests/fail_path_smoke.py --base http://127.0.0.1:8801 --paper medical-2026-zang
"""
from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path

OUT = Path(__file__).resolve().parent / "phase5-curl-failure-report.txt"


def curl(base, token, path, method="GET", body_file=None):
    args = ["curl.exe", "-s", "-w", "\nHTTP %{http_code} in %{time_total}s",
            "-X", method, "-H", f"X-Reader-Token: {token}"]
    if body_file:
        args += ["-H", "Content-Type: application/json", "--data", f"@{body_file}"]
    args.append(base + path)
    done = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return done.stdout.strip()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--paper", required=True)
    parser.add_argument("--token", default="smoke-token")
    args = parser.parse_args()

    force = Path(__file__).resolve().parent / "force.json"
    lines = ["# Phase 5 failure-path curl report", ""]

    def record(title, command):
        lines.append(f"## {title}")
        lines.append(command)
        lines.append("")

    record("POST generate (invalid JSON on the model side)", curl(
        args.base, args.token, f"/api/analysis/{args.paper}/generate", "POST", str(force)))
    time.sleep(6)
    record("GET status (summary only, no answer text)", curl(
        args.base, args.token, f"/api/analysis/{args.paper}/status"))
    record("GET answer (withheld while failed)", curl(
        args.base, args.token, f"/api/analysis/{args.paper}"))
    record("POST generate again after fixing the provider", curl(
        args.base, args.token, f"/api/analysis/{args.paper}/generate", "POST", str(force)))
    time.sleep(6)
    record("GET status after retry", curl(
        args.base, args.token, f"/api/analysis/{args.paper}/status"))

    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    for line in lines:
        if line.startswith("HTTP") or line.startswith("{"):
            print(line[:200])
    print("written:", OUT)


if __name__ == "__main__":
    main()
