"""Rebuild the frozen test prefix in a NEW database after a discovered bug.

Preserve canonical identity by exact source row keys, not fuzzy matching.
No deletes, no updates to historical commits, no LLM calls. Old DB retained.
"""
import asyncio
import json
import shutil
import sqlite3
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from app import models
from app.commit.models import ChapterPreviewPackage
from app.planning.provenance import compute_story_state_hash
from app.quality.review_assistance import ReviewAssistanceAssessment
from app.schemas.studio import StudioRun
from app.schemas.story import BootstrapWorldRequest
from app.services.memory_extraction import MemoryExtractionService
from app.services.story_state import StoryStateService
from app.services.trial_artifacts import write_new
from scripts.isolated_continuation import ROOT, NOVEL, fingerprint, test_commit

SOURCE = ROOT / "real_trial_runs/isolated_four_20261031_6XeYOj"
DEST = SOURCE / "repaired"
IDENTITY_KEYS = {
    "characters": ("name",), "locations": ("name",), "items": ("name",),
    "factions": ("name",), "power_systems": ("name",), "realms": ("name", "power_system_id"),
    "story_facts": ("statement", "introduced_chapter"),
    "plot_debts": ("description", "created_chapter"),
    "foreshadowings": ("setup", "planted_chapter"),
}


def source_identity_map():
    identities = {}
    with sqlite3.connect(f"file:{SOURCE / 'trial.db'}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        for table, columns in IDENTITY_KEYS.items():
            rows = db.execute(f'SELECT * FROM "{table}"').fetchall()
            selected = [row for row in rows if table == "realms" or row["novel_id"] == uuid.UUID(NOVEL).hex]
            values = {tuple(row[column] for column in columns): uuid.UUID(row["id"]) for row in selected}
            assert len(values) == len(selected), f"Ambiguous source identity in {table}"
            identities[table] = values
    return identities


async def main():
    marker = json.loads((SOURCE / "experiment.json").read_text())
    assert marker["kind"] == "isolated_test" and marker["automated_test_authorized"]
    assert fingerprint(Path(marker["source_database"])) == marker["source_fingerprint"]
    DEST.mkdir(exist_ok=True)
    target = DEST / "trial.db"
    if target.exists():
        raise ValueError("Refuse overwrite; the failed and repaired databases are immutable artifacts")
    with sqlite3.connect(f"file:{marker['source_database']}?mode=ro", uri=True) as src, sqlite3.connect(target) as dst:
        src.backup(dst)
    write_new(DEST / "experiment.json", {**marker, "replayed_from_database": str(SOURCE / "trial.db"),
        "repair_reason": "Reject unknown state fields; explicitly correct ch4 power_realm to canonical realm=练气",
        "human_approved": False, "production_commit": False})
    for filename in ("usage_ledger.json", "capabilities.json", "native_capabilities.json", "llm_calls.jsonl", "cultivation_test_setup.json"):
        shutil.copy2(SOURCE / filename, DEST / filename)
    write_new(DEST / "source_identity_map.json", {
        table: [{"key": list(key), "id": str(value)} for key, value in rows.items()]
        for table, rows in source_identity_map().items()})
    identities = source_identity_map()

    def assign_original_identity(session, flush_context, instances):
        for obj in session.new:
            table = obj.__tablename__
            if table not in identities or obj.id is not None:
                continue
            values = []
            for key in IDENTITY_KEYS[table]:
                value = getattr(obj, key)
                values.append(value.hex if isinstance(value, uuid.UUID) else value)
            identifier = identities[table].get(tuple(values))
            if identifier:
                obj.id = identifier

    event.listen(AsyncSession.sync_session_class, "before_flush", assign_original_identity)
    engine = create_async_engine(f"sqlite+aiosqlite:///{target}")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        runs = [StudioRun.model_validate_json(path.read_text()) for path in (SOURCE / "studio").glob("*/run.json")]
        for run in sorted((r for r in runs if r.status == "completed"), key=lambda r: r.current_chapter):
            chapter = run.chapters[0]
            old_attempt = chapter.attempt_id
            source = SOURCE / "studio" / run.id / f"chapter_{chapter.number:02d}/attempts" / old_attempt
            package = ChapterPreviewPackage.model_validate_json((source / "preview_package.json").read_text())
            if chapter.number == 4:
                operation = next(op for op in package.memory_result.batch.operations if op.operation_id == "op_0012")
                assert operation.character_state.field == "power_realm"
                operation.character_state.field = "realm"
                operation.character_state.proposed_new_value = "练气"
                operation.character_state.new_value_description = "正文气落丹田且不再散，首次进入练气；一层的具体层级保留在事件/正文，不伪造目录 ID。"
            async with maker() as session:
                assert await compute_story_state_hash(session, uuid.UUID(NOVEL)) == package.story_state_hash, "Replay must reproduce the exact chapter-before state"
                package.memory_result = await MemoryExtractionService(session, require_evidence_refs=True).validate_batch(
                    uuid.UUID(NOVEL), package.draft, package.plan_result.plan, package.memory_result.batch, dry_run=True)
                assert package.memory_result.validation_report.valid, package.memory_result.validation_report
                assert package.memory_result.dry_run.applicable
            package.preview_package_hash = ChapterPreviewPackage.compute_hash(plan=package.plan_result.plan,
                draft=package.draft, continuity_report=package.continuity_report,
                memory_result=package.memory_result, story_state_hash=package.story_state_hash)
            chapter.attempt_id = f"replay_{old_attempt}"
            destination = DEST / "studio" / run.id / f"chapter_{chapter.number:02d}/attempts" / chapter.attempt_id
            destination.mkdir(parents=True)
            write_new(destination / "preview_package.json", package.model_dump(mode="json"))
            write_new(destination / "chapter.txt", package.draft.full_text, text=True)
            shutil.copy2(source / "quality.json", destination / "quality.json")
            note = {"human_approved": False, "source": str(source), "original_attempt": old_attempt,
                "identity_preserved_by": "exact table key -> original UUID, no fuzzy matching",
                "prose_unchanged": True, "no_llm_calls": True,
                "correction": "op_0012 field=realm value=练气" if chapter.number == 4 else "No semantic change",
                "source_model_review_and_primary_review": "Retained in original source attempt; this is a test replay, not a human approval."}
            write_new(destination / "replay_review.json", note)
            assessment = ReviewAssistanceAssessment(operation_reviews=[
                {"operation_id": op.operation_id, "verdict": "correct", "reason": "Previously reviewed operation; exact-identity replay. Ch4 realm field explicitly corrected to supported contract; see replay_review.json."}
                for op in package.memory_result.batch.operations], missing=[],
                quality_scores=json.loads((source / "quality.json").read_text())["scores"],
                summary="非人工测试重放；原稿未变，严格验证通过，保留全部原失败记录。")
            await test_commit(maker, package, chapter.attempt_id, assessment, destination)
            write_new(DEST / "studio" / run.id / "run.json", run.model_dump(mode="json"))
            if chapter.number in {2, 3, 4}:
                write_new(DEST / f"chapter_{chapter.number:02d}.txt", package.draft.full_text, text=True)
            if chapter.number == 1:
                setup = BootstrapWorldRequest.model_validate(json.loads((SOURCE / "cultivation_test_setup.json").read_text())["setup"])
                async with maker() as session:
                    await StoryStateService(session).bootstrap_world(uuid.UUID(NOVEL), setup)
                    await session.commit()
        async with maker() as session:
            hero = await session.scalar(select(models.Character).where(models.Character.name == "沈渡", models.Character.novel_id == uuid.UUID(NOVEL)))
            state = await StoryStateService(session).get_current_state(hero.id)
            assert state.realm.name == "练气", "Realm must exist in actual SQL, not just preview"
        write_new(DEST / "replay_complete.json", {"human_approved": False, "chapters": [1, 2, 3, 4],
            "chapter4_actual_realm": "练气", "original_database_preserved": True, "no_llm_calls": True})
    finally:
        event.remove(AsyncSession.sync_session_class, "before_flush", assign_original_identity)
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
