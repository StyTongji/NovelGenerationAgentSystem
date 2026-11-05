"""Offline Studio exercises the production agents and canonical commit path."""

from __future__ import annotations

import hashlib
import asyncio

import pytest
from sqlalchemy import func, select

from app.commit.models import ReviewApproval
from app.context.builder import ContextBuilder
from app.models import Chapter, ChapterCommit, Character, CharacterKnowledge, CharacterState, Event, StoryFact, StorySnapshot
from app.planning.provenance import compute_story_state_hash
from app.services.studio_demo import build_demo_services
from app.services.studio_seed import StudioNovelInput, create_studio_novel


def approval(package, attempt):
    return ReviewApproval(
        attempt_id=attempt, reviewed_by="offline test reviewer", approved=True,
        draft_hash=hashlib.sha256(package.draft.full_text.encode()).hexdigest(),
        memory_hash=ReviewApproval.hash_memory(package.memory_result),
        preview_package_hash=package.preview_package_hash,
        operation_reviews={op.operation_id: "correct" for op in package.memory_result.batch.operations},
        quality_scores={name: 4 for name in ("readability", "character", "dialogue", "continuity", "progression")},
        semantic_review_complete=True,
    )


@pytest.mark.parametrize("chapter_count", [3, 10])
async def test_custom_story_chapters_preserve_canon_until_review_and_inherit(session, chapter_count):
    novel = await create_studio_novel(session, StudioNovelInput(
        title="雾港档案", genre="悬疑", synopsis="档案员追查港口记录的改动，寻找失踪的同事。",
        protagonist="叶闻舟", companions=["孟青禾"], world_rules=["所有证据都必须保留原件。"],
    ))
    await session.commit()
    hero = (await session.scalars(select(Character).where(
        Character.novel_id == novel.id, Character.is_protagonist.is_(True)))).one()
    previous_fact = None
    drafts = []
    for chapter_number in range(1, chapter_count + 1):
        before = await compute_story_state_hash(session, novel.id)
        service = await build_demo_services(session, novel.id, chapter_number)
        package = await service.generate_preview(novel.id, "继续调查并保留证据", chapter_number=chapter_number)
        assert await compute_story_state_hash(session, novel.id) == before
        assert novel.current_chapter_number == chapter_number - 1
        assert package.continuity_report.valid
        assert package.memory_result.validation_report.valid, package.memory_result.validation_report.model_dump()
        assert package.memory_result.dry_run.applicable
        assert package.memory_result.batch.contract_version == 3
        assert len(package.memory_result.batch.operations) == 6
        assert all(op.evidence_refs and op.evidence for op in package.memory_result.batch.operations)
        assert "叶闻舟" in package.draft.full_text
        assert "林凡" not in package.draft.full_text
        if previous_fact:
            assert previous_fact.statement in package.draft.full_text
            assert previous_fact.statement in str(package.plan_result.plan.model_dump()) or "上一次留下的判断" in package.draft.full_text
        drafts.append(package.draft.full_text)
        receipt = approval(package, f"demo-{chapter_number}")
        result = await service.finalize(package, attempt_id=receipt.attempt_id, approval=receipt)
        assert not result.duplicate
        assert novel.current_chapter_number == chapter_number
        assert await compute_story_state_hash(session, novel.id) == result.after_story_state_hash
        repeated = await service.finalize(package, attempt_id=receipt.attempt_id, approval=receipt)
        assert repeated.duplicate
        assert repeated.after_story_state_hash == result.after_story_state_hash
        for model in (Chapter, ChapterCommit, Event, StorySnapshot):
            assert await session.scalar(select(func.count()).select_from(model)) == chapter_number
        previous_fact = (await session.scalars(select(StoryFact).where(
            StoryFact.novel_id == novel.id, StoryFact.category == "demo_discovery",
            StoryFact.introduced_chapter == chapter_number))).one()
        knowledge = (await session.scalars(select(CharacterKnowledge).where(
            CharacterKnowledge.character_id == hero.id, CharacterKnowledge.fact_id == previous_fact.id))).one()
        assert knowledge.learned_chapter == chapter_number
        current = (await session.scalars(select(CharacterState).where(
            CharacterState.character_id == hero.id, CharacterState.chapter_number == chapter_number))).one()
        assert current.mental_condition == next(op.character_state.proposed_new_value
            for op in package.memory_result.batch.operations if op.character_state)
        context = await ContextBuilder(session).build_director_context(
            novel.id, chapter_number=chapter_number + 1, user_direction="继续")
        assert previous_fact.statement in context.model_dump_json()
    assert len(set(drafts)) == chapter_count
    with pytest.raises(ValueError, match="十个连续章节"):
        await build_demo_services(session, novel.id, 11)


async def test_demo_refuses_skipping_uncommitted_chapter_and_accepts_solo_cast(session):
    novel = await create_studio_novel(session, StudioNovelInput(
        title="独行档案", genre="都市", synopsis="一个人查阅旧卷。", protagonist="陶言",
    ))
    await session.commit()
    with pytest.raises(ValueError, match="上一章"):
        await build_demo_services(session, novel.id, 2)
    service = await build_demo_services(session, novel.id, 1)
    package = await service.generate_preview(novel.id, "检查旧记录", chapter_number=1)
    assert package.memory_result.validation_report.valid
    assert package.memory_result.dry_run.applicable
    assert "陶言" in package.draft.full_text


def test_seed_normalizes_names_and_rejects_duplicate_protagonist():
    parsed = StudioNovelInput(title=" 新故事 ", synopsis=" 梗概 ", protagonist=" 主角 ",
                             companions=[" 配角 ", "", "配角"], world_rules=["规则", "规则"])
    assert parsed.title == "新故事" and parsed.companions == ["配角"]
    assert parsed.world_rules == ["规则"]
    with pytest.raises(ValueError, match="主角不能"):
        StudioNovelInput(title="故事", synopsis="梗概", protagonist="主角", companions=["主角"])


async def test_demo_resume_after_first_scene_preserves_scene_identity(session, tmp_path):
    from app.services.trial_artifacts import TrialAttempt
    scene_saved = asyncio.Event()

    class InterruptedAttempt(TrialAttempt):
        async def stage(self, name, model, produce):
            value = await super().stage(name, model, produce)
            if name == "scene_1":
                scene_saved.set()
                await asyncio.Future()
            return value

    novel = await create_studio_novel(session, StudioNovelInput(
        title="恢复测试", synopsis="核对档案。", protagonist="顾简"))
    await session.commit()
    before = await compute_story_state_hash(session, novel.id)
    attempt = InterruptedAttempt(tmp_path / "demo", {"state_hash": before, "chapter": 1})
    first = await build_demo_services(session, novel.id, 1)
    task = asyncio.create_task(first.generate_preview(novel.id, "继续", chapter_number=1, checkpoints=attempt))
    await asyncio.wait_for(scene_saved.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await compute_story_state_hash(session, novel.id) == before
    resumed = TrialAttempt(attempt.path, attempt.signature, resume=True)
    second = await build_demo_services(session, novel.id, 1)
    result = await second.generate_preview(novel.id, "继续", chapter_number=1, checkpoints=resumed)
    assert result.memory_result.validation_report.valid
    assert result.memory_result.dry_run.applicable
    assert "摊开旧记录" in result.draft.scenes[0].text
    assert "将已经核实的部分另抄了一份" in result.draft.scenes[1].text
    assert result.draft.scenes[0].text != result.draft.scenes[1].text
    provider = second.draft_service.writer.llm.provider
    assert len(provider.calls) == 3  # only scene 2, continuity and memory resume


def test_extraction_state_accepts_legacy_context_and_tracks_mental_condition():
    from app.context.models import ExtractionCharacterState

    legacy = ExtractionCharacterState(character_id="character", name="主角", alive_status="alive")
    assert legacy.field_value("mental_condition") is None
    current = legacy.model_copy(update={"mental_condition": "警觉"})
    assert current.field_value("mental_condition") == "警觉"
