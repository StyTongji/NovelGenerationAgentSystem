"""CanonicalCommitService tests (Phase 8 §83 checklist).

Reuses the e2e pipeline helpers to build REAL preview packages through the
production services, then exercises the commit boundary.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from app.agents.memory_extractor import MemoryExtractorAgent
from app.agents.scene_planner import ScenePlannerAgent
from app.agents.writer import WriterAgent
from app.commit.models import (
    ChapterCommitRequest,
    ChapterNumberCollisionError,
    CommitError,
    StaleStoryStateError,
)
from app.context.builder import ContextBuilder
from app.continuity.models import ContinuityReport
from app.draft.models import SceneDraft
from app.llm.client import AgentLLMClient
from app.llm.fake import FakeLLMProvider
from app.llm.prompts import get_default_registry
from app.memory.models import (
    CharacterStateChange,
    EventCandidate,
    EvidenceSpan,
    ItemChange,
    KnowledgeChange,
    MemoryUpdateBatch,
    MemoryUpdateOperation,
    MemoryUpdateType,
    MemoryVisibility,
    PlotThreadChange,
    ProposedCanonicalEntity,
    StoryFactCandidate,
)
from app.models import (
    Chapter,
    ChapterCommit,
    Novel,
    Character,
    CharacterKnowledge,
    CharacterState,
    Event,
    Item,
    PlotThread,
    StoryFact,
    StorySnapshot,
)
from app.planning.scene_validator import ScenePlanValidator
from app.planning.provenance import compute_story_state_hash
from app.schemas.agent_outputs import ChapterPlan, EntityRef, ScenePlan
from app.services.canonical_commit import CanonicalCommitService
from app.services.chapter_generation import ChapterGenerationService
from app.services.chapter_planning import ChapterPlanningService, build_director_agent
from app.services.continuity import ContinuityService
from app.services.memory_extraction import MemoryExtractionService
from tests.helpers import create_novel, create_rich_world
from tests.e2e.test_preview_pipeline import (
    MIRROR_FACT_STATEMENT,
    SCENE_TEXTS,
    _build_agents,
    _memory_batch,
    _scene_batch,
    _world,
    db_fingerprint,
)
from tests.helpers import create_character_with_state, create_rich_world

USER_DIRECTION = "下一章推进拍卖会剧情。"


async def _full_preview(session, provider, *, novel=None, world=None, chapter_number=5):
    """Run the production preview pipeline and return (novel, world, package)."""
    if novel is None:
        novel = await create_novel(session)
        world = await create_rich_world(session, novel.id)
    mirror_fact = await world["service"].add_fact(
        novel.id, MIRROR_FACT_STATEMENT, secrecy="secret"
    )
    await world["service"].grant_knowledge(
        world["characters"]["林凡"].id, mirror_fact.id
    )
    # the rich world already has canonical chapters 1-3; next commit is ch5
    novel.current_chapter_number = 4
    await session.flush()
    await session.commit()  # isolate fixture data from commit rollbacks

    provider.enqueue_structured(build_valid_plan_dict(world))
    director = build_director_agent(
        session, AgentLLMClient(provider, agent_name="director", model="fake-model")
    )
    planning_service = ChapterPlanningService(session, director=director)
    planning = await planning_service.generate_preview(
        novel.id, USER_DIRECTION, chapter_number=chapter_number
    )
    plan = planning.plan

    provider.enqueue_structured(_scene_batch(world))
    for scene_id in ("scene_1", "scene_2", "scene_3", "scene_4"):
        provider.enqueue_text(SCENE_TEXTS[scene_id])
    scene_planner = ScenePlannerAgent(
        llm=AgentLLMClient(provider, agent_name="scene_planner", model="fake-model"),
        prompt_registry=get_default_registry(),
        validator=ScenePlanValidator(session),
    )
    writer = WriterAgent(
        llm=AgentLLMClient(provider, agent_name="writer", model="fake-model"),
        prompt_registry=get_default_registry(),
    )
    draft_service = ChapterDraftService(session, scene_planner, writer)
    draft = await draft_service.generate_preview(novel.id, plan)

    continuity = ContinuityService(
        session, llm_checker=None, repair_agent=None, mode="deterministic_only"
    )
    report = await continuity.check_draft(
        novel.id, draft, plan, scene_plans=list(draft.scene_plans)
    )
    assert report.valid

    provider.enqueue_structured(
        _memory_batch(world, str(mirror_fact.id)).model_dump(exclude_none=True)
    )
    extractor = MemoryExtractorAgent(
        contract_version=2,  # explicit offline legacy replay
        llm=AgentLLMClient(provider, agent_name="memory_extractor", model="fake-model"),
        prompt_registry=get_default_registry(),
    )
    memory_service = MemoryExtractionService(session, extractor)
    memory = await memory_service.extract_preview(
        novel.id, draft, plan, report, scene_plans=list(draft.scene_plans)
    )
    return novel, world, mirror_fact, planning, draft, report, memory


from app.services.chapter_draft import ChapterDraftService  # noqa: E402
from tests.draft_helpers import build_valid_plan_dict  # noqa: E402


def _request(novel, plan, draft, report, memory, expected_hash) -> ChapterCommitRequest:
    return ChapterCommitRequest(
        novel_id=str(novel.id),
        chapter_plan=plan,
        chapter_draft=draft,
        continuity_report=report,
        memory_result=memory,
        expected_story_state_hash=expected_hash,
    )


# ------------------------------------------------------------------- happy commit

async def test_happy_commit_persists_everything(session):
    novel, world, mirror_fact, planning, draft, report, memory = await _full_preview(
        session, FakeLLMProvider()
    )
    expected = await compute_story_state_hash(session, novel.id)
    service = CanonicalCommitService(session, allow_legacy_batches=True)

    result = await service.commit_chapter(
        _request(novel, planning.plan, draft, report, memory, expected)
    )

    # chapter persisted, canonical, versioned
    chapter = await session.get(Chapter, uuid.UUID(result.chapter_id))
    assert chapter is not None
    assert chapter.status == "canonical"
    assert chapter.number == 5
    assert chapter.draft_hash == result.draft_hash
    # events persisted with author + reader summaries in metadata
    events = (
        await session.execute(select(Event).where(Event.chapter_number == 5))
    ).scalars().all()
    assert any(e.event_type == "combat" for e in events)
    # character state: 苏青 injured at chapter 5
    su_state = None
    for state in (await session.execute(select(CharacterState))).scalars():
        if str(state.character_id) == str(world["characters"]["苏青"].id) and state.chapter_number == 5:
            su_state = state
    assert su_state is not None and su_state.physical_condition == "injured"
    # knowledge: 苏青 knows the mirror fact
    knowledge = (await session.execute(select(CharacterKnowledge))).scalars().all()
    assert any(
        str(k.character_id) == str(world["characters"]["苏青"].id)
        and k.status == "known"
        and str(k.fact_id) == str(mirror_fact.id)
        for k in knowledge
    )
    assert True
    # plot thread advanced
    father = next(
        t for t in (await session.execute(select(PlotThread))).scalars()
        if t.title == "父亲失踪之谜"
    )
    assert father.last_advanced_chapter == 5
    # canonicalized entity
    assert "new_character_1" in result.temporary_entity_mapping
    gu = await session.get(Character, uuid.UUID(result.temporary_entity_mapping["new_character_1"]))
    assert gu.name == "顾清璃"
    # summaries with visibility separation
    from app.models import ChapterSummary

    summary = (
        await session.execute(select(ChapterSummary).where(ChapterSummary.chapter_number == 5))
    ).scalar_one()
    assert summary.author_summary and summary.reader_summary
    assert summary.author_summary != summary.reader_summary
    # snapshot created after mutations
    snapshot = await session.get(StorySnapshot, uuid.UUID(result.snapshot_id))
    assert snapshot is not None
    assert snapshot is not None and snapshot.after_chapter_number == 5
    # novel advanced
    novel_after = await session.get(Novel, novel.id)
    assert novel_after.current_chapter_number == 5
    # hashes
    assert result.before_story_state_hash == expected
    assert result.after_story_state_hash != result.before_story_state_hash
    # commit record exists with fingerprints + evidence provenance
    record = await session.get(ChapterCommit, result.commit_id)
    assert record.draft_hash == result.draft_hash
    assert len(record.operation_fingerprints_json) == result.operation_count
    provenance = record.operations_json[0]
    assert provenance["evidence"]
    assert {"evidence_id", "exact_text", "start_char", "end_char"}.issubset(
        provenance["evidence"][0]
    )
    # memory_operation evidence persisted (§80)
    assert "挡在林凡身前" in json.dumps(record.operations_json, ensure_ascii=False)


# --------------------------------------------------------------------- gates

async def test_stale_story_state_rejected(session):
    novel, world, mirror_fact, planning, draft, report, memory = await _full_preview(
        session, FakeLLMProvider()
    )
    service = CanonicalCommitService(session, allow_legacy_batches=True)
    with pytest.raises(StaleStoryStateError):
        await service.commit_chapter(
            _request(novel, planning.plan, draft, report, memory, "0" * 64)
        )
    # zero mutation relative to the fixture (chapters 1-3 exist, current=4)
    novel_after = await session.get(Novel, novel.id)
    assert novel_after.current_chapter_number == 4
    assert all(c.number != 5 for c in (await session.execute(select(Chapter))).scalars().all())


async def test_draft_hash_mismatch_rejected(session):
    novel, world, mirror_fact, planning, draft, report, memory = await _full_preview(
        session, FakeLLMProvider()
    )
    expected = await compute_story_state_hash(session, novel.id)
    # client secretly modifies the draft after continuity approval (#49)
    tampered = draft.model_copy(deep=True)
    tampered.scenes = list(tampered.scenes)
    tampered.scenes[0] = tampered.scenes[0].model_copy(update={"text": "偷改过的正文。"})
    tampered.full_text = "偷改过的正文。"
    service = CanonicalCommitService(session, allow_legacy_batches=True)
    with pytest.raises(CommitError) as excinfo:
        await service.commit_chapter(
            _request(novel, planning.plan, tampered, report, memory, expected)
        )
    assert excinfo.value.code.value in ("draft_hash_mismatch", "continuity_invalid")


async def test_modified_memory_batch_rejected(session):
    """#50: client swaps in batch B -> server re-fingerprints/validates."""
    novel, world, mirror_fact, planning, draft, report, memory = await _full_preview(
        session, FakeLLMProvider()
    )
    expected = await compute_story_state_hash(session, novel.id)
    bad_batch = MemoryUpdateBatch(
        operations=[
            MemoryUpdateOperation(
                operation_id="op-bad", update_type=MemoryUpdateType.create_event,
                source_scene_id="scene_1",
                evidence=[EvidenceSpan(scene_id="scene_1", exact_quote="这句话不在正文里。")],
                event=EventCandidate(
                    event_type="death", author_summary="x", reader_safe_summary="x",
                    importance=0.5,
                ),
            )
        ]
    )
    tampered_memory = memory.model_copy(deep=True)
    tampered_memory.batch = bad_batch
    service = CanonicalCommitService(session, allow_legacy_batches=True)
    with pytest.raises(CommitError) as excinfo:
        await service.commit_chapter(
            _request(novel, planning.plan, draft, report, tampered_memory, expected)
        )
    assert excinfo.value.code.value == "memory_invalid"


async def test_chapter_number_collision_rejected(session):
    novel, world, mirror_fact, planning, draft, report, memory = await _full_preview(
        session, FakeLLMProvider()
    )
    novel.current_chapter_number = 5  # someone already committed ch5
    await session.flush()
    expected = await compute_story_state_hash(session, novel.id)
    service = CanonicalCommitService(session, allow_legacy_batches=True)
    with pytest.raises(ChapterNumberCollisionError):
        await service.commit_chapter(
            _request(novel, planning.plan, draft, report, memory, expected)
        )


async def test_duplicate_commit_returns_existing_result(session):
    """#14/#47: same draft committed twice -> no new state, existing result."""
    novel, world, mirror_fact, planning, draft, report, memory = await _full_preview(
        session, FakeLLMProvider()
    )
    expected = await compute_story_state_hash(session, novel.id)
    service = CanonicalCommitService(session, allow_legacy_batches=True)
    request = _request(novel, planning.plan, draft, report, memory, expected)

    first = await service.commit_chapter(request)
    counts_after_first = await _row_counts(session)
    state_hash_after_first = await compute_story_state_hash(session, novel.id)

    second = await service.commit_chapter(request)
    assert second.duplicate is True
    assert second.commit_id == first.commit_id
    assert await _row_counts(session) == counts_after_first
    assert await compute_story_state_hash(session, novel.id) == state_hash_after_first


async def _row_counts(session) -> dict:
    out = {}
    for model in (Chapter, Event, CharacterState, CharacterKnowledge,
                  StoryFact, Item, PlotThread, StorySnapshot, ChapterCommit):
        total = await session.execute(select(func.count()).select_from(model))
        out[model.__name__] = total.scalar_one()
    return out


# --------------------------------------------------------------- ordering etc.

async def test_story_fact_then_knowledge_ordering(session):
    """#69: same batch creates fact + knowledge -> commit succeeds."""
    novel, world, mirror_fact, planning, draft, report, memory = await _full_preview(
        session, FakeLLMProvider()
    )
    lin = str(world["characters"]["林凡"].id)
    extra = MemoryUpdateBatch(
        operations=[
            MemoryUpdateOperation(
                operation_id="op-fact", update_type=MemoryUpdateType.story_fact_create,
                source_scene_id="scene_2",
                evidence=[EvidenceSpan(scene_id="scene_2", exact_quote="玉佩边缘的纹路竟与")],
                story_fact=StoryFactCandidate(
                    statement="玉佩背面存在归墟纹", secrecy="secret", reader_visible=False,
                ),
            ),
            MemoryUpdateOperation(
                operation_id="op-know", update_type=MemoryUpdateType.character_knowledge_update,
                source_scene_id="scene_2",
                evidence=[EvidenceSpan(scene_id="scene_2", exact_quote="玉佩边缘的纹路竟与")],
                knowledge=KnowledgeChange(
                    character_id=lin, status="known",
                    fact_statement="玉佩背面存在归墟纹",
                ),
            ),
        ]
    )
    expected = await compute_story_state_hash(session, novel.id)
    service = CanonicalCommitService(session, allow_legacy_batches=True)
    result = await service.commit_chapter(
        _request(novel, planning.plan, draft, report, memory_with_extra(memory, extra), expected)
    )
    facts = (await session.execute(select(StoryFact))).scalars().all()
    assert any(f.statement == "玉佩背面存在归墟纹" for f in facts)
    knowledge = (await session.execute(select(CharacterKnowledge))).scalars().all()
    fact_id = next(f.id for f in facts if f.statement == "玉佩背面存在归墟纹")
    assert any(str(k.fact_id) == str(fact_id) for k in knowledge)
    # reader-invisible fact NOT auto-revealed (#24)
    fact_row = next(f for f in facts if f.statement == "玉佩背面存在归墟纹")
    assert fact_row.revealed_to_reader_chapter is None


def memory_with_extra(memory, extra):
    merged = memory.batch.model_copy(deep=True)
    merged.operations = merged.operations + extra.operations
    return memory.model_copy(update={"batch": merged})


async def test_same_chapter_state_collapse(session):
    """#19/#70: injured then partially healed -> ONE chapter-end row."""
    novel, world, mirror_fact, planning, draft, report, memory = await _full_preview(
        session, FakeLLMProvider()
    )
    su = str(world["characters"]["苏青"].id)
    injured = MemoryUpdateBatch(
        operations=[
            MemoryUpdateOperation(
                operation_id="op-inj1", update_type=MemoryUpdateType.character_state_update,
                source_scene_id="scene_3",
                evidence=[EvidenceSpan(scene_id="scene_3", exact_quote="肩头顿时渗出血迹")],
                character_state=CharacterStateChange(
                    character_id=su, field="physical_condition",
                    proposed_new_value="serious",
                ),
            ),
            MemoryUpdateOperation(
                operation_id="op-inj2", update_type=MemoryUpdateType.character_state_update,
                source_scene_id="scene_4",
                evidence=[EvidenceSpan(scene_id="scene_4", exact_quote="林凡收起古镜")],
                character_state=CharacterStateChange(
                    character_id=su, field="physical_condition",
                    proposed_new_value="minor",
                ),
            ),
        ]
    )
    expected = await compute_story_state_hash(session, novel.id)
    service = CanonicalCommitService(session, allow_legacy_batches=True)
    await service.commit_chapter(
        _request(novel, planning.plan, draft, report, memory_with_extra(memory, injured), expected)
    )
    su_states = [
        s for s in (await session.execute(select(CharacterState))).scalars()
        if str(s.character_id) == su
    ]
    ch5 = [s for s in su_states if s.chapter_number == 5]
    assert len(ch5) == 1, "同一章只应有一个 chapter-end state row"
    assert ch5[0].physical_condition == "minor"
    # history preserved: ch0 row intact
    assert any(s.chapter_number == 0 for s in su_states)


async def test_item_transfer_chain_final_owner(session):
    """#71: multi-hop transfers in one batch collapse to the final owner."""
    from app.models import Item

    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    jade = await world["service"].items.add(
        Item(novel_id=novel.id, name="玉佩", type="treasure", status="intact")
    )
    novel2, world2, mirror_fact, planning, draft, report, memory = await _full_preview(
        session, FakeLLMProvider(), novel=novel, world=world
    )
    novel = novel2
    jade = await session.get(Item, jade.id)
    lin = str(world["characters"]["林凡"].id)
    su = str(world["characters"]["苏青"].id)
    hops = MemoryUpdateBatch(
        operations=[
            MemoryUpdateOperation(
                operation_id="h1", update_type=MemoryUpdateType.item_update,
                source_scene_id="scene_1",
                evidence=[EvidenceSpan(scene_id="scene_1", exact_quote="林凡在雅间落座")],
                item=ItemChange(item_id=str(jade.id), change="owner_transfer",
                                expected_old_owner_id=None, new_owner_id=lin),
            ),
            MemoryUpdateOperation(
                operation_id="h2", update_type=MemoryUpdateType.item_update,
                source_scene_id="scene_2",
                evidence=[EvidenceSpan(scene_id="scene_2", exact_quote="顾清璃缓缓举牌")],
                item=ItemChange(item_id=str(jade.id), change="owner_transfer",
                                expected_old_owner_id=lin, new_owner_id=su),
            ),
            MemoryUpdateOperation(
                operation_id="h3", update_type=MemoryUpdateType.item_update,
                source_scene_id="scene_4",
                evidence=[EvidenceSpan(scene_id="scene_4", exact_quote="玉佩最终落入林凡手中")],
                item=ItemChange(item_id=str(jade.id), change="owner_transfer",
                                expected_old_owner_id=su, new_owner_id=lin),
            ),
        ]
    )
    expected = await compute_story_state_hash(session, novel.id)
    service = CanonicalCommitService(session, allow_legacy_batches=True)
    await service.commit_chapter(
        _request(novel, planning.plan, draft, report, memory_with_extra(memory, hops), expected)
    )
    final = await session.get(Item, jade.id)
    assert str(final.current_owner_character_id) == lin


async def test_mid_transaction_failure_rolls_back_everything(session):
    """#45/#85: injected failure mid-transaction -> deep fingerprint unchanged."""
    novel, world, mirror_fact, planning, draft, report, memory = await _full_preview(
        session, FakeLLMProvider()
    )
    fingerprint_before = await db_fingerprint(session)
    expected = await compute_story_state_hash(session, novel.id)
    service = CanonicalCommitService(session, allow_legacy_batches=True)

    original = service._apply_character_states

    async def exploding(*args, **kwargs):
        raise RuntimeError("injected failure")

    service._apply_character_states = exploding  # type: ignore[method-assign]
    with pytest.raises(CommitError) as excinfo:
        await service.commit_chapter(
            _request(novel, planning.plan, draft, report, memory, expected)
        )
    assert excinfo.value.code.value == "precondition_failed"
    assert "commit_failed" in excinfo.value.message
    service._apply_character_states = original  # type: ignore[method-assign]

    assert await db_fingerprint(session) == fingerprint_before, (
        "事务中间失败必须完全 rollback"
    )
    novel_after = await session.get(Novel, novel.id)
    assert novel_after.current_chapter_number == 4


async def test_proposed_entity_rollback(session):
    """#46: 顾清璃 created then failure -> not left in DB."""
    novel, world, mirror_fact, planning, draft, report, memory = await _full_preview(
        session, FakeLLMProvider()
    )
    fingerprint_before = await db_fingerprint(session)
    expected = await compute_story_state_hash(session, novel.id)
    service = CanonicalCommitService(session, allow_legacy_batches=True)

    async def exploding(*args, **kwargs):
        raise RuntimeError("injected failure")

    service._apply_story_facts = exploding  # type: ignore[method-assign]
    with pytest.raises(CommitError):
        await service.commit_chapter(
            _request(novel, planning.plan, draft, report, memory, expected)
        )
    service._apply_story_facts = CanonicalCommitService._apply_story_facts  # type: ignore[method-assign]

    characters = (await session.execute(select(Character))).scalars().all()
    assert all(c.name != "顾清璃" for c in characters)
    assert await db_fingerprint(session) == fingerprint_before


async def test_finalize_zero_llm_calls(session):
    """#84: finalize is a deterministic transaction."""
    novel, world, mirror_fact, planning, draft, report, memory = await _full_preview(
        session, FakeLLMProvider()
    )
    provider = FakeLLMProvider()
    generation_service = _generation_service(session, provider)
    from app.commit.models import ChapterPreviewPackage

    package = ChapterPreviewPackage(
        novel_id=str(novel.id),
        chapter_number=draft.chapter_number,
        plan_result=planning,
        draft=draft,
        continuity_report=report,
        memory_result=memory,
        story_state_hash=planning.context_hash,
        preview_package_hash=ChapterPreviewPackage.compute_hash(
            plan=planning.plan, draft=draft, continuity_report=report,
            memory_result=memory, story_state_hash=planning.context_hash,
        ),
        generated_at=datetime.now(UTC),
    )
    calls_before = len(provider.calls)
    result = await generation_service.finalize(package)
    assert len(provider.calls) == calls_before, "finalize 绝不调用 LLM"
    assert result.chapter_number == 5


def _generation_service(session, provider):
    from app.agents.scene_planner import ScenePlannerAgent
    from app.agents.writer import WriterAgent
    from app.agents.writer_repair import WriterRepairAgent

    planning_service = ChapterPlanningService(
        session, director=build_director_agent(
            session, AgentLLMClient(provider, agent_name="director", model="fake-model")
        )
    )
    draft_service = ChapterDraftService(
        session,
        ScenePlannerAgent(
            llm=AgentLLMClient(provider, agent_name="scene_planner", model="fake-model"),
            prompt_registry=get_default_registry(),
            validator=ScenePlanValidator(session),
        ),
        WriterAgent(
            llm=AgentLLMClient(provider, agent_name="writer", model="fake-model"),
            prompt_registry=get_default_registry(),
        ),
    )
    continuity = ContinuityService(
        session,
        llm_checker=None,
        repair_agent=WriterRepairAgent(
            llm=AgentLLMClient(provider, agent_name="writer_repair", model="fake-model"),
            prompt_registry=get_default_registry(),
        ),
        mode="deterministic_only",
    )
    memory = MemoryExtractionService(
        session,
        MemoryExtractorAgent(
        contract_version=2,  # explicit offline legacy replay
            llm=AgentLLMClient(provider, agent_name="memory_extractor", model="fake-model"),
            prompt_registry=get_default_registry(),
        ),
    )
    return ChapterGenerationService(
        session, planning_service, draft_service, continuity, memory,
        CanonicalCommitService(session, allow_legacy_batches=True),
    )


async def test_cross_novel_mutation_rejected(session):
    novel, world, mirror_fact, planning, draft, report, memory = await _full_preview(
        session, FakeLLMProvider()
    )
    other = await create_novel(session, title="另一本")
    other_world = await create_rich_world(session, other.id)
    lin = str(world["characters"]["林凡"].id)
    # batch references OTHER novel's item
    from app.models import Item

    foreign_item = await world["service"].items.add(
        Item(novel_id=other.id, name="异界之物", type="treasure")
    )
    batch = MemoryUpdateBatch(
        operations=[
            MemoryUpdateOperation(
                operation_id="op-x", update_type=MemoryUpdateType.item_update,
                source_scene_id="scene_1",
                evidence=[EvidenceSpan(scene_id="scene_1", exact_quote="林凡在雅间落座")],
                item=ItemChange(item_id=str(foreign_item.id), change="owner_transfer",
                                new_owner_id=lin),
            )
        ]
    )
    merged = memory.model_copy(update={"batch": batch})
    expected = await compute_story_state_hash(session, novel.id)
    service = CanonicalCommitService(session, allow_legacy_batches=True)
    with pytest.raises(CommitError) as excinfo:
        await service.commit_chapter(
            _request(novel, planning.plan, draft, report, merged, expected)
        )
    assert excinfo.value.code.value == "memory_invalid"
    foreign_after = await session.get(Item, foreign_item.id)
    assert foreign_after.current_owner_character_id is None


async def test_next_chapter_context_sees_committed_state(session):
    """#66: after commit, DirectorContext for ch6 carries the new world."""
    novel, world, mirror_fact, planning, draft, report, memory = await _full_preview(
        session, FakeLLMProvider()
    )
    expected = await compute_story_state_hash(session, novel.id)
    service = CanonicalCommitService(session, allow_legacy_batches=True)
    result = await service.commit_chapter(
        _request(novel, planning.plan, draft, report, memory, expected)
    )
    builder = ContextBuilder(session)
    context = await builder.build_director_context(novel.id, chapter_number=6)
    su = next(c for c in context.active_characters if c.name == "苏青")
    assert su.state.physical_condition == "injured"
    assert su.state.chapter_number == 5
    # 顾清璃 now canonical
    assert any(c.name == "顾清璃" for c in context.active_characters)
    # previous chapter ending available for the next writer
    writer_context = await builder.build_writer_context(
        novel.id, chapter_number=6, pov_character_id=world["characters"]["林凡"].id
    )
    assert writer_context.recent_context.previous_chapter_ending is not None
    assert "共鸣" in writer_context.recent_context.previous_chapter_ending
    # story state hash chain: next expected == commit after hash
    assert await compute_story_state_hash(session, novel.id) == result.after_story_state_hash


async def test_two_chapter_committed_flow(session):
    """#67: ch5 commit -> ch6 preview -> ch6 commit; state carries forward."""
    novel, world, mirror_fact, planning5, draft5, report5, memory5 = await _full_preview(
        session, FakeLLMProvider()
    )
    service = CanonicalCommitService(session, allow_legacy_batches=True)
    expected5 = await compute_story_state_hash(session, novel.id)
    result5 = await service.commit_chapter(
        _request(novel, planning5.plan, draft5, report5, memory5, expected5)
    )
    assert result5.chapter_number == 5

    # chapter 6: next preview is based on the AFTER hash
    provider = FakeLLMProvider()
    provider.enqueue_structured(build_valid_plan_dict(world))
    provider.enqueue_structured(_scene_batch(world))
    for scene_id in ("scene_1", "scene_2", "scene_3", "scene_4"):
        # different prose from ch5 so the draft hash differs (no idempotent dup)
        provider.enqueue_text(SCENE_TEXTS[scene_id] + "第六章的后续展开。")
    director = build_director_agent(
        session, AgentLLMClient(provider, agent_name="director", model="fake-model")
    )
    planning_service = ChapterPlanningService(session, director=director)
    planning6 = await planning_service.generate_preview(
        novel.id, "第六章继续推进。", chapter_number=6
    )
    assert planning6.context_hash == result5.after_story_state_hash, (
        "Chapter 6 的 state hash 必须等于 Chapter 5 的 after hash"
    )
    plan6 = planning6.plan.model_copy(update={"chapter_number": 6})
    scene_planner = ScenePlannerAgent(
        llm=AgentLLMClient(provider, agent_name="scene_planner", model="fake-model"),
        prompt_registry=get_default_registry(),
        validator=ScenePlanValidator(session),
    )
    writer = WriterAgent(
        llm=AgentLLMClient(provider, agent_name="writer", model="fake-model"),
        prompt_registry=get_default_registry(),
    )
    draft6 = await ChapterDraftService(session, scene_planner, writer).generate_preview(
        novel.id, plan6
    )
    assert draft6.chapter_number == 6
    continuity6 = ContinuityService(
        session, llm_checker=None, repair_agent=None, mode="deterministic_only"
    )
    report6 = await continuity6.check_draft(
        novel.id, draft6, plan6, scene_plans=list(draft6.scene_plans)
    )
    # 苏青 injury persisted from ch5 -> writer context saw it; no contradiction
    assert report6.valid
    provider.enqueue_structured({"operations": [], "candidate_author_summary": None,
                                 "candidate_reader_summary": None})
    memory6 = await MemoryExtractionService(
        session,
        MemoryExtractorAgent(
        contract_version=2,  # explicit offline legacy replay
            llm=AgentLLMClient(provider, agent_name="memory_extractor", model="fake-model"),
            prompt_registry=get_default_registry(),
        ),
    ).extract_preview(
        novel.id, draft6, plan6, report6, scene_plans=list(draft6.scene_plans)
    )
    expected6 = await compute_story_state_hash(session, novel.id)
    result6 = await service.commit_chapter(
        _request(novel, plan6, draft6, report6, memory6, expected6)
    )
    assert result6.chapter_number == 6
    novel_after = await session.get(Novel, novel.id)
    assert novel_after.current_chapter_number == 6
    snapshots = (await session.execute(select(StorySnapshot))).scalars().all()
    assert {s.after_chapter_number for s in snapshots} == {5, 6}
