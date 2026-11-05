#!/usr/bin/env python3
"""Read-only comparison of ALL memory attempts, including failures; no API calls."""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.services.trial_artifacts import safe_name, write_new


def compare(run_dir: Path, chapter: int, models: list[str], expected=3, attempt_prefix=None):
    rows = []
    cohorts = set()
    for path in sorted((run_dir / f"chapter_{chapter:02d}" / "attempts").glob("*/manifest.json")):
        if attempt_prefix and not path.parent.name.startswith(attempt_prefix):
            continue
        signature = json.loads(path.read_text())["signature"]
        if signature.get("kind") != "memory":
            continue
        model = signature.get("memory_model") or signature.get("model")
        if model not in models:
            continue
        cohort = {k: signature.get(k) for k in (
            "source_hash", "state_hash", "prompt_hash", "capabilities_hash", "base_url", "memory_max_tokens")}
        cohorts.add(json.dumps(cohort, sort_keys=True))
        row = {"attempt": path.parent.name, "requested_model": model, "cohort": cohort}
        memory_path = path.parent / "memory_preview.json"
        if memory_path.exists():
            memory = json.loads(memory_path.read_text())
            metadata = memory.get("generation_metadata") or {}
            calls = metadata.get("extra", {}).get("http_attempts", [])
            row.update(status="completed", operations=len(memory["batch"]["operations"]),
                valid=memory["validation_report"]["valid"],
                applicable=bool(memory.get("dry_run") and memory["dry_run"]["applicable"]),
                issues=dict(Counter(i["code"] for i in memory["validation_report"]["issues"])),
                http_requests=len(calls),
                known_tokens=sum(c["usage"]["total_tokens"] for c in calls if c.get("usage_known")),
                usage_complete=metadata.get("extra", {}).get("usage_complete", False),
                response_model=metadata.get("extra", {}).get("response_model"))
            row["input_hash"] = metadata.get("extra", {}).get("input_hash")
            row["input_utf8_bytes"] = metadata.get("extra", {}).get("input_utf8_bytes")
            row["latency_ms"] = metadata.get("latency_ms")
        else:
            failures = [json.loads(p.read_text()) for p in sorted(path.parent.glob("failure_*.json"))]
            row.update(status="failed" if failures else "incomplete",
                       errors=[f["error_type"] for f in failures], usage_complete=False)
        rows.append(row)
    ledger_path = run_dir / "usage_ledger.json"
    ledger = json.loads(ledger_path.read_text())["reservations"] if ledger_path.exists() else {}
    charges = [r for r in ledger.values() if r["chapter"] == str(chapter)]
    input_hashes = {r["input_hash"] for r in rows if r.get("input_hash")}
    return {"chapter": chapter, "attempt_prefix": attempt_prefix, "expected_per_model": expected,
        "same_input_cohort": len(cohorts) == 1,
        "same_completed_request_input_hash": len(input_hashes) == 1,
        "completed_request_input_hashes": sorted(input_hashes),
        "samples": {model: {"attempts": sum(r["requested_model"] == model for r in rows),
            "completed": sum(r["requested_model"] == model and r["status"] == "completed" for r in rows),
            "missing_planned_attempts": max(0, expected - sum(r["requested_model"] == model for r in rows))}
            for model in models},
        "attempts": rows,
        "budget": {"http_requests": len(charges),
            "known_tokens": sum(r["charged_tokens"] for r in charges if r["usage_known"]),
            "unknown_usage_reservations": sum(not r["usage_known"] for r in charges),
            "reserved_unknown_tokens": sum(r["charged_tokens"] for r in charges if not r["usage_known"]),
            "charged_tokens": sum(r["charged_tokens"] for r in charges)},
        "conclusion": "No model ranking: validate all attempts and obtain independent human semantic review."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True)
    parser.add_argument("--chapter", type=int, default=1)
    parser.add_argument("--models", nargs="+", default=["DeepSeek-V4.1-Flash-1017", "Auto"])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--attempt-prefix", help="Select a named experiment cohort, not only passing results")
    args = parser.parse_args()
    result = compare(ROOT / "real_trial_runs" / safe_name(args.run), args.chapter, args.models,
                     attempt_prefix=args.attempt_prefix)
    if args.output:
        write_new(args.output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
