"""Recorded primary-agent adjudication of the frozen Chapter 1 test seed.

Not a human approval and not usable against the production commit path.
All listed edits follow a full prose/operation review; no new prose or LLM call.
"""
import asyncio
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.config import get_settings
from app.services.studio import StudioManager
from app.quality.review_assistance import ReviewAssistanceAssessment
from app.services.trial_artifacts import write_new
from scripts.isolated_continuation import test_commit, wait

ROOT = Path(__file__).resolve().parents[1]
EXPERIMENT = ROOT / "real_trial_runs/isolated_four_20261031_6XeYOj"


async def main():
    marker = json.loads((EXPERIMENT / "experiment.json").read_text())
    assert marker["kind"] == "isolated_test" and marker["automated_test_authorized"]
    engine = create_async_engine(f"sqlite+aiosqlite:///{EXPERIMENT / 'trial.db'}")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    manager = StudioManager(EXPERIMENT / "studio", maker, get_settings())
    try:
        run = manager.get("test_seed")
        chapter = manager._chapter(run)
        destination = manager._attempt_path(run, chapter)
        if (destination / "primary_agent_test_review.json").exists():
            package = manager._package(run, chapter)
            receipt_file = next(destination.glob("automated_test_receipt_*.json"))
            receipt = json.loads(receipt_file.read_text())
            assessment = ReviewAssistanceAssessment(operation_reviews=[
                {"operation_id": key, "verdict": value, "reason": "See bound primary_agent_test_review.json"}
                for key, value in receipt["operation_reviews"].items()], missing=receipt["missing"],
                quality_scores=receipt["quality_scores"], summary="Resume bound non-human test review")
            await test_commit(maker, package, chapter.attempt_id, assessment, destination)
            run.status = chapter.status = "completed"
            manager._save(run)
            return
        assert chapter.attempt_id == "f75af80576c5", "Review binds one frozen candidate only"
        source = manager._package(run, chapter)
        assert len(source.draft.full_text) == 5253
        batch = source.memory_result.batch.model_copy(deep=True)
        batch.operations = [op for op in batch.operations if op.operation_id not in {"op_038", "op_039"}]
        for op in batch.operations:
            if op.operation_id == "op_027":
                op.plot_thread.note = "沈渡首次当众以看路能力说破阿禾死路，阿禾一家改换方向；能力是否应验仍待验证。"
            if op.operation_id == "op_013":
                op.event.reader_safe_summary = "高境修士一句话截断灵渠灵气，众人跪伏；他留下“三日后再来”的话，恢复尚未验证。"
        from app.memory.models import MemoryUpdateOperation
        batch.operations.append(MemoryUpdateOperation.model_validate({
            "operation_id": "seed_pending_three_days", "update_type": "plot_debt_update",
            "source_scene_id": "ch1_sc2", "evidence_refs": [{"evidence_id": "ev_0074"}],
            "plot_debt": {"action": "created", "description": "高境修士称“三日后再来”；灵渠灵气是否届时恢复尚待验证，不以夜间水闸声认定恢复。"}}))
        await manager.new_candidate(run.id, 1, "memory", {"batch": batch.model_dump(mode="json")})
        await manager.tasks[run.id]
        assert run.status == "awaiting_review", run.error
        chapter = manager._chapter(run)
        package = manager._package(run, chapter)
        assert package.draft.full_text == source.draft.full_text
        notes = {
            "reviewer": "Codex primary agent / NON HUMAN TEST REVIEW",
            "source_attempt": "f75af80576c5", "attempt_id": chapter.attempt_id,
            "human_approved": False, "preview_package_hash": package.preview_package_hash,
            "read_scope": "Full 5253-character prose; every memory operation, exact evidence and canonical baseline reviewed.",
            "corrections": [
                "op_027 removed unrevealed 果位谱/纳音术语; prose has not named them yet.",
                "Removed op_038/039: one observation/mediation does not establish a durable relationship.",
                "op_013 treats three days as a statement, not a proven restoration.",
                "Added pending three-day restoration debt with exact ev_0074; no invented chapter deadline.",
            ],
            "adjudicated_missing": [
                "Initial canonical 健康 is the real before-value; end-of-chapter weakness is already op_023. Do not change baseline to narrative intermediate state.",
                "Other characters' appearances and movements are in events. Their unknown off-page chapter-end positions must not be invented.",
                "Unnamed mother/uncle are already recorded in op_022; no need to invent names, UUIDs or permanent relationships.",
                "Book blood-writing and final name-writing are now op_040/042 and events op_016/028.",
                "Private visions are known only by 沈渡; 黄守一 suspicion remains suspicion. Existing world-rule fact UUID is present in SQL.",
            ],
        }
        destination = manager._attempt_path(run, chapter)
        write_new(destination / "primary_agent_test_review.json", notes)
        assessment = ReviewAssistanceAssessment(
            operation_reviews=[{"operation_id": op.operation_id, "verdict": "correct",
                "reason": "Primary-agent prose/evidence/before-state review passed; see bound primary_agent_test_review.json for corrections and adjudication."}
                for op in package.memory_result.batch.operations],
            missing=[], quality_scores={key: 4 for key in ("readability", "character", "dialogue", "continuity", "progression")},
            summary="仅隔离测试的 Agent 复核；非人工批准。正文未改变，证据、隐私、章末状态与债务核对通过。")
        await test_commit(maker, package, chapter.attempt_id, assessment, destination)
        run.status = chapter.status = "completed"
        manager._save(run)
    finally:
        await manager.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
