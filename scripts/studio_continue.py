"""Continue one saved Studio chapter to human review, never auto-finalize.

Run while the Studio web server is stopped: the normal single-owner lock
prevents concurrent managers. Recovery creates an immutable recheck attempt.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings
from app.db import get_engine, get_sessionmaker
from app.planning.provenance import compute_story_state_hash
from app.schemas.studio import BudgetLimits, RecheckInput
from app.services.studio import StudioManager
from app.services.trial_artifacts import write_new


async def main(args):
    settings = get_settings()
    manager = StudioManager(Path(settings.studio_directory), get_sessionmaker(), settings)
    try:
        run = manager.get(args.run)
        if run.mode != "live" or run.config.provider != "scnet":
            raise ValueError("Select an existing SCNet live run")
        async with manager.session_factory() as session:
            before = await compute_story_state_hash(session, uuid.UUID(run.novel_id))
        initial_budget = manager.budget_summary(run.id)
        if run.status != "awaiting_review":
            profile = json.loads(args.capabilities.read_text())
            review_model = args.review_model or run.config.model
            if (not profile.get("ok") or profile["model"] != review_model
                or profile["base_url"].rstrip("/") != settings.llm.providers["scnet"].base_url.rstrip("/")):
                raise ValueError("Capability profile must match this provider and model")
            capabilities = {review_model: profile["capabilities"]}
            role_models = ({**run.config.role_models, **{role: review_model for role in
                ("continuity_checker", "writer_repair", "memory_extractor", "literary_reviewer")}}
                if args.review_model else run.config.role_models)
            await manager.update_budget(run.id, BudgetLimits(max_calls=args.max_calls,
                chapter_tokens=args.chapter_tokens, run_tokens=run.config.run_tokens))
            chapter = manager._chapter(run)
            if (chapter.action == "recheck" and run.config.runtime_capabilities == capabilities
                and run.config.max_output_tokens == args.max_output_tokens and run.config.role_models == role_models):
                await manager.resume(run.id)
            else:
                await manager.new_candidate(run.id, run.current_chapter, "recheck", {},
                    runtime=RecheckInput(max_output_tokens=args.max_output_tokens, runtime_capabilities=capabilities,
                                         role_models=role_models))
            task = manager.tasks[run.id]
            while not task.done():
                await asyncio.wait({task}, timeout=20)
                print(json.dumps({"status": run.status, "stage": run.active_stage,
                    "budget": manager.budget_summary(run.id)}, ensure_ascii=False), flush=True)
            await task
        detail = manager.chapter_detail(run.id, run.current_chapter)
        async with manager.session_factory() as session:
            after = await compute_story_state_hash(session, uuid.UUID(run.novel_id))
        package = detail["package"]
        report = {"run_id": run.id, "title": run.title, "chapter_number": run.current_chapter,
            "attempt_id": detail["attempt_id"], "status": run.status, "error": run.error,
            "before_state_hash": before, "after_state_hash": after, "canonical_unchanged": before == after,
            "initial_budget": initial_budget, "final_budget": manager.budget_summary(run.id),
            "draft_chars": len(package["draft"]["full_text"]) if package else None,
            "continuity_valid": package["continuity_report"]["valid"] if package else None,
            "memory_valid": package["memory_result"]["validation_report"]["valid"] if package else None,
            "dry_run_applicable": bool(package and package["memory_result"]["dry_run"] and package["memory_result"]["dry_run"]["applicable"]),
            "quality": detail["quality"], "human_approved": False,
        }
        path = manager.root / run.id / f"recovery_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
        write_new(path, report)
        print(json.dumps({"report": str(path), **{k: v for k, v in report.items() if k != "quality"}}, ensure_ascii=False), flush=True)
        if before != after:
            raise RuntimeError("Unexpected canonical mutation during preview")
        return 0 if run.status == "awaiting_review" else 2
    finally:
        await manager.close()
        await get_engine().dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True)
    parser.add_argument("--capabilities", type=Path, required=True)
    parser.add_argument("--chapter-tokens", type=int, default=300000)
    parser.add_argument("--max-calls", type=int, default=50)
    parser.add_argument("--max-output-tokens", type=int, default=8192)
    parser.add_argument("--review-model", help="Explicit SCNet model for checks/repair/memory/review; planning and Writer unchanged")
    raise SystemExit(asyncio.run(main(parser.parse_args())))
