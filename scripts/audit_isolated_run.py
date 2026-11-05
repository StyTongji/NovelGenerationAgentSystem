"""Audit a marked test database and replay its non-human duplicate finalizes.

No API requests; no production writes. Refuses a database without a marker.
"""
import argparse
import asyncio
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.commit.models import ChapterPreviewPackage, ReviewApproval
from app.models import Chapter, ChapterCommit, StorySnapshot, Event, Character, StoryFact, CharacterState, Item, Location, Realm, CharacterKnowledge
from app.planning.provenance import compute_story_state_hash, hash_state_dict
from app.services.canonical_commit import CanonicalCommitService
from app.services.chapter_generation import ChapterGenerationService
from app.services.trial_artifacts import write_new
from scripts.isolated_continuation import fingerprint, NOVEL, ROOT
from app.llm.budget import TrialBudget


async def main(directory):
    root = directory.resolve()
    marker = json.loads((root / "experiment.json").read_text())
    assert marker["kind"] == "isolated_test" and marker["automated_test_authorized"]
    database = root / "trial.db"
    assert database.resolve() != (ROOT / "novel_engine.db").resolve()
    before = fingerprint(database)
    engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    report = {"human_approved": False, "production_commit": False, "chapters": []}
    try:
        async with maker() as session:
            novel_id = uuid.UUID(NOVEL)
            report["counts"] = {model.__tablename__: await session.scalar(
                select(func.count()).select_from(model).where(model.novel_id == novel_id))
                for model in (Chapter, ChapterCommit, StorySnapshot, Event, Character, StoryFact)}
            chapters = (await session.scalars(select(Chapter).where(
                Chapter.novel_id == novel_id).order_by(Chapter.number))).all()
            records = (await session.scalars(select(ChapterCommit).where(
                ChapterCommit.novel_id == novel_id).order_by(ChapterCommit.chapter_number))).all()
            snapshots = (await session.scalars(select(StorySnapshot).where(
                StorySnapshot.novel_id == novel_id).order_by(StorySnapshot.after_chapter_number))).all()
            assert len(chapters) == len(records) == len(snapshots)
            assert [row.number for row in chapters] == list(range(1, len(chapters) + 1))
            for chapter, record, snapshot in zip(chapters, records, snapshots):
                candidates = list((root / "studio").glob(f"*/chapter_{chapter.number:02d}/attempts/*/test_commit.json"))
                path = next(path.parent for path in candidates
                    if json.loads(path.read_text())["result"]["commit_id"] == str(record.id))
                package = ChapterPreviewPackage.model_validate_json((path / "preview_package.json").read_text())
                receipt = next(ReviewApproval.model_validate_json(file.read_text())
                    for file in path.glob("automated_test_receipt_*.json")
                    if json.loads(file.read_text())["preview_package_hash"] == package.preview_package_hash)
                assert receipt.review_kind == "automated_test"
                assert chapter.body == package.draft.full_text
                assert chapter.story_state_after_hash == record.after_state_hash == snapshot.state_hash
                assert hash_state_dict(snapshot.serialized_state) == snapshot.state_hash
                events = (await session.scalars(select(Event).where(Event.chapter_id == chapter.id))).all()
                expected = [op.event for op in package.memory_result.batch.operations if op.event]
                assert sorted(event.title for event in events) == sorted(event.title for event in expected)
                merged = {}
                for op in package.memory_result.batch.operations:
                    if op.character_state:
                        merged.setdefault(op.character_state.character_id, {})[op.character_state.field] = op.character_state.proposed_new_value
                for identifier, fields in merged.items():
                    state = await session.scalar(select(CharacterState).where(
                        CharacterState.character_id == uuid.UUID(identifier), CharacterState.chapter_number == chapter.number))
                    assert state is not None
                    for field, value in fields.items():
                        if field == "realm":
                            assert (await session.get(Realm, state.realm_id)).name == value
                        elif field == "location":
                            assert (await session.get(Location, state.current_location_id)).name == value
                        else:
                            actual = getattr(state, field)
                            assert (actual.value if hasattr(actual, "value") else actual) == value
                service = ChapterGenerationService(session, None, None, None, None,
                    CanonicalCommitService(session, allow_automated_test_review=True))
                duplicate = await service.finalize(package, attempt_id=receipt.attempt_id, approval=receipt)
                assert duplicate.duplicate and duplicate.commit_id == record.id
                report["chapters"].append({"number": chapter.number, "title": chapter.title,
                    "characters": len(chapter.body), "attempt": receipt.attempt_id,
                    "operations": len(package.memory_result.batch.operations),
                    "events_match_preview": True, "snapshot_hash_verified": True,
                    "state_effects_match_preview": True,
                    "continuity_valid": package.continuity_report.valid,
                    "memory_valid": package.memory_result.validation_report.valid,
                    "dry_run_applicable": package.memory_result.dry_run.applicable,
                    "duplicate": True, "artifact": str(path.relative_to(root))})
            if chapters:
                report["current_state_hash_matches_latest_commit"] = (
                    await compute_story_state_hash(session, novel_id) == records[-1].after_state_hash)
                assert report["current_state_hash_matches_latest_commit"]
            report["cast"] = [{"name": row.name, "id": str(row.id), "description": row.description}
                for row in (await session.scalars(select(Character).where(Character.novel_id == novel_id))).all()]
            report["current_realms"] = []
            for character in (await session.scalars(select(Character).where(Character.novel_id == novel_id))).all():
                state = await session.scalar(select(CharacterState).where(CharacterState.character_id == character.id)
                    .order_by(CharacterState.chapter_number.desc()).limit(1))
                realm = await session.get(Realm, state.realm_id) if state and state.realm_id else None
                report["current_realms"].append({"character": character.name, "realm": realm.name if realm else None})
            report["item_owners"] = []
            for item in (await session.scalars(select(Item).where(Item.novel_id == novel_id))).all():
                owner = await session.get(Character, item.current_owner_character_id) if item.current_owner_character_id else None
                report["item_owners"].append({"item": item.name, "owner": owner.name if owner else None})
            grey = await session.scalar(select(StoryFact).where(StoryFact.novel_id == novel_id,
                StoryFact.statement == "沈渡凝神观察虞衡时只见灰雾，未能将其与谱中六十枚纳音印对应；原因与真实位格未明。"))
            if grey:
                knowers = (await session.scalars(select(Character).join(CharacterKnowledge,
                    CharacterKnowledge.character_id == Character.id).where(CharacterKnowledge.fact_id == grey.id))).all()
                report["grey_fog_knowledge_holders"] = [character.name for character in knowers]
                assert report["grey_fog_knowledge_holders"] == ["沈渡"]
        report["duplicate_all_tables_unchanged"] = before == fingerprint(database)
        report["production_database_unchanged"] = fingerprint(Path(marker["source_database"])) == marker["source_fingerprint"]
        assert report["duplicate_all_tables_unchanged"] and report["production_database_unchanged"]
        report["budget"] = TrialBudget(root / "usage_ledger.json", 5,
            max_calls=50, chapter_tokens=450000, run_tokens=1700000).summary()
        output = root / f"audit_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
        write_new(output, report)
        print(json.dumps({"report": str(output), **report}, ensure_ascii=False, indent=2))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", type=Path, required=True)
    asyncio.run(main(parser.parse_args().experiment))
