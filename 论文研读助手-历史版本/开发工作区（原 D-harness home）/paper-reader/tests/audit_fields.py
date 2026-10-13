"""Audit A: do the 13 reading-card fields agree across every layer?

Layers: the validator/prompt (reading_card.FIELD_KEYS), the front-end card and steps
(guides.js FIELDS / STAGES), the paper record (schema/library fields), the stored answer
file, and the export path.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import reading_card  # noqa: E402

report = {}

# 1. validator
validator = list(reading_card.FIELD_KEYS)
report["reading_card.FIELD_KEYS"] = validator

# 2. front end
guides = (ROOT / "static" / "guides.js").read_text(encoding="utf-8")


def block(name):
    start = guides.index(f"export const {name}")
    nxt = guides.find("export const", start + 10)
    return guides[start:nxt if nxt != -1 else len(guides)]


fields_block = block("FIELDS")
front_fields = re.findall(r"^\s*([A-Za-z_]+)\s*:\s*\[", fields_block, re.M)
report["guides.FIELDS order"] = front_fields

stages_block = block("STAGES")
groups = re.findall(r"fields:\s*\[([^\]]*)\]", stages_block)
stage_flat = []
for group in groups:
    stage_flat += [part.strip().strip("'\"") for part in group.split(",") if part.strip()]
report["guides.STAGES fields"] = stage_flat

# 3. every layer that lists the keys in the prompt and the empty card
prompt = reading_card.SYSTEM_PROMPT
report["prompt_lists_all_fields"] = all(key in prompt for key in validator)

empty = reading_card.empty_card("audit", 1, provider="none", language="zh")
report["empty_card_keys"] = sorted(empty["answers"])

# 4. the human record and the export template
schema_module = (ROOT / "schema.py").read_text(encoding="utf-8")
report["schema_fields_mentioned"] = {
    key: (f'"{key}"' in schema_module or f"'{key}'" in schema_module) for key in validator}
report["app_export_uses_FIELDS"] = "Object.entries(FIELDS)" in (ROOT / "static" / "app.js").read_text(
    encoding="utf-8")

# 5. the sample answer files
samples = {}
for path in sorted((ROOT / "tests" / "samples").glob("*.json")):
    payload = json.loads(path.read_text(encoding="utf-8"))
    if "answers" in payload:
        samples[path.name] = {
            "keys_equal": sorted(payload["answers"]) == sorted(validator),
            "count": len(payload["answers"]),
            "verification_field_count": payload.get("verification", {}).get("field_count"),
        }
report["samples"] = samples

verdict = {
    "front_order_matches": front_fields == validator,
    "front_set_matches": set(front_fields) == set(validator),
    "stages_cover_all": sorted(stage_flat) == sorted(validator),
    "empty_card_matches": sorted(empty["answers"]) == sorted(validator),
    "prompt_complete": report["prompt_lists_all_fields"],
}
report["verdict"] = verdict
print(json.dumps(report, ensure_ascii=False, indent=2))
