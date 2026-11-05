"""Phase 8 committed E2E: preview -> finalize -> next chapter sees the
committed world. Full production services, FakeLLM only."""

from __future__ import annotations

import hashlib
import json
import uuid

import pytest
from sqlalchemy import select

from app.agents.memory_extractor import MemoryExtractorAgent
from app.agents.scene_planner import ScenePlannerAgent
from app.agents.writer import WriterAgent
from app.agents.writer_repair import WriterRepairAgent
from app.commit.models import ChapterPreviewPackage
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
)
from app.models import (
    Chapter,
    ChapterCommit,
    ChapterSummary,
    Character,
    CharacterKnowledge,
    CharacterState,
    Event,
    Item,
    Novel,
    PlotThread,
    StoryFact,
    StorySnapshot,
)
from app.planning.scene_validator import ScenePlanValidator
from app.planning.provenance import compute_story_state_hash
from app.schemas.agent_outputs import ChapterPlan, EntityRef, ScenePlan
from app.services.canonical_commit import CanonicalCommitService
from app.services.chapter_draft import ChapterDraftService
from app.services.chapter_generation import ChapterGenerationService
from app.services.chapter_planning import ChapterPlanningService, build_director_agent
from app.services.continuity import ContinuityService
from app.services.memory_extraction import MemoryExtractionService
from tests.e2e.test_preview_pipeline import (
    MIRROR_FACT_STATEMENT,
    SCENE_TEXTS,
    db_fingerprint,
)
from tests.helpers import create_rich_world

USER_DIRECTION_CH5 = (
    "下一章让林凡和苏青进入天剑城拍卖会，发现与轮回古镜有关的玉佩，"
    "遇到中州神秘女子；推进父亲伏笔但只给模糊线索；苏青为保护林凡受伤；"
    "不要让林凡突破。"
)


def _build_generation_service(session, provider) -> ChapterGenerationService:
    planning = ChapterPlanningService(
        session, director=build_director_agent(
            session, AgentLLMClient(provider, agent_name="director", model="fake-model")
        )
    )
    draft = ChapterDraftService(
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
        previous_scene_tail_chars=200,
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
        session, planning, draft, continuity, memory, CanonicalCommitService(session, allow_legacy_batches=True)
    )


def _scene_batch(world, chapter_number: int) -> dict:
    lin = str(world["characters"]["林凡"].id)
    su = str(world["characters"]["苏青"].id)
    scenes = []
    for index, scene_id in enumerate(["scene_1", "scene_2", "scene_3", "scene_4"], start=1):
        scenes.append(
            {
                "scene_id": f"ch{chapter_number}_{scene_id}",
                "scene_index": index,
                "source_beat_ids": ["1" if index <= 2 else "2"],
                "pov": {"character_id": lin, "name": "林凡"},
                "location": {"name": "天剑城"},
                "participants": [
                    {"character_id": lin, "name": "林凡"},
                    *(
                        [{"character_id": su, "name": "苏青"}]
                        if index == 3
                        else [{"temporary_ref": "new_character_1", "name": "神秘女子"}]
                    ),
                ],
                "scene_goal": f"ch{chapter_number} 场景{index}",
                "required_outcomes": [],
                "target_chars": 800,
                "temporary_entities": ["new_character_1"]
                if index != 3
                else [],
            }
        )
    return {"chapter_number": chapter_number, "scenes": scenes, "planner_notes": []}


def _memory_batch_for(world, mirror_fact_id: str, chapter_number: int) -> dict:
    su = str(world["characters"]["苏青"].id)
    lin = str(world["characters"]["林凡"].id)
    father_id = str(world["result"].plot_thread_ids["父亲失踪之谜"])
    prefix = f"ch{chapter_number}"
    return {
        "operations": [
            {
                "operation_id": f"{prefix}-event",
                "update_type": "create_event",
                "source_scene_id": f"{prefix}_scene_3",
                "evidence": [{"scene_id": f"{prefix}_scene_3",
                              "exact_quote": "苏青一步挡在林凡身前，硬受了一记掌劲，肩头顿时渗出血迹。"}],
                "event": {
                    "event_type": "combat",
                    "author_summary": "苏青替林凡挡下一击并受伤",
                    "reader_safe_summary": "苏青替林凡挡下一击，受了伤",
                    "participant_ids": [su, lin],
                    "importance": 0.8,
                    "visibility": {"reader_visible": True},
                },
            },
            {
                "operation_id": f"{prefix}-injury",
                "update_type": "character_state_update",
                "source_scene_id": f"{prefix}_scene_3",
                "evidence": [{"scene_id": f"{prefix}_scene_3",
                              "exact_quote": "肩头顿时渗出血迹"}],
                "character_state": {
                    "character_id": su, "field": "physical_condition",
                    "proposed_new_value": "injured",
                    "requires_confirmation": True,
                },
            },
            {
                "operation_id": f"{prefix}-know",
                "update_type": "character_knowledge_update",
                "source_scene_id": f"{prefix}_scene_3",
                "evidence": [{"scene_id": f"{prefix}_scene_3",
                              "exact_quote": "苏青看见了那面古镜"}],
                "knowledge": {"character_id": su, "fact_id": mirror_fact_id,
                              "status": "known"},
            },
            {
                "operation_id": f"{prefix}-thread",
                "update_type": "plot_thread_update",
                "source_scene_id": f"{prefix}_scene_4",
                "evidence": [{"scene_id": f"{prefix}_scene_4",
                              "exact_quote": "父亲失踪之谜似乎又多了一条线索"}],
                "plot_thread": {"plot_thread_id": father_id, "action": "advanced"},
            },
            {
                "operation_id": f"{prefix}-entity",
                "update_type": "propose_entity",
                "source_scene_id": f"{prefix}_scene_1",
                "evidence": [{"scene_id": f"{prefix}_scene_1",
                              "exact_quote": "她报上名号：顾清璃，目光却始终停留在展台方向。"}],
                "proposed_entity": {
                    "temporary_ref": "new_character_1", "entity_type": "character",
                    "proposed_name": "顾清璃",
                    "attributes": {"origin": "中州", "gender": "女"},
                },
            },
        ],
        "candidate_author_summary": "苏青挡剑受伤；古镜秘密暴露；父亲线推进。",
        "candidate_reader_summary": "拍卖会风波中苏青为林凡受伤，玉佩与古镜共鸣。",
    }


def _writer_texts(chapter_number: int) -> list[str]:
    return [SCENE_TEXTS[s] for s in ("scene_1", "scene_2", "scene_3", "scene_4")]


async def test_two_chapter_committed_pipeline(session):
    novel = (
        await _create_novel_direct(session)
    )
    world = await create_rich_world(session, novel.id)
    mirror_fact = await world["service"].add_fact(
        novel.id, MIRROR_FACT_STATEMENT, secrecy="secret"
    )
    await world["service"].grant_knowledge(
        world["characters"]["林凡"].id, mirror_fact.id
    )
    novel.current_chapter_number = 4
    await session.flush()
    await session.commit()
    fingerprint_after_setup = await db_fingerprint(session)

    provider = FakeLLMProvider()
    generation = _build_generation_service(session, provider)

    # ================= Chapter 5 preview =================
    provider.enqueue_structured(_plan_dict(world, 5))
    provider.enqueue_structured(_scene_batch(world, 5))
    for text in _writer_texts(5):
        provider.enqueue_text(text)
    provider.enqueue_structured(
        _memory_batch_for(world, str(mirror_fact.id), 5)
    )
    package5 = await generation.generate_preview(novel.id, USER_DIRECTION_CH5, chapter_number=5)

    assert package5.status == "preview"
    assert package5.continuity_report.valid
    assert package5.memory_result.validation_report.valid
    assert package5.memory_result.dry_run is not None
    calls_before_finalize = len(provider.calls)

    # ================= Chapter 5 finalize (deterministic) =================
    result5 = await generation.finalize(package5)

    assert len(provider.calls) == calls_before_finalize, "finalize 零 LLM 调用"
    assert result5.chapter_number == 5 and result5.duplicate is False

    # canonical world after commit
    chapter5 = await session.get(Chapter, uuid.UUID(result5.chapter_id))
    assert chapter5.status == "canonical" and chapter5.number == 5
    su_state = None
    for state in (await session.execute(select(CharacterState))).scalars():
        if (str(state.character_id) == str(world["characters"]["苏青"].id)
                and state.chapter_number == 5):
            su_state = state
    assert su_state.physical_condition == "injured"
    knowledge = (await session.execute(select(CharacterKnowledge))).scalars().all()
    assert any(
        str(k.character_id) == str(world["characters"]["苏青"].id)
        and str(k.fact_id) == str(mirror_fact.id) and k.status == "known"
        for k in knowledge
    )
    gu_qing_li = await session.get(Character, uuid.UUID(result5.temporary_entity_mapping["new_character_1"]))
    assert gu_qing_li.name == "顾清璃"
    father = next(
        t for t in (await session.execute(select(PlotThread))).scalars()
        if t.title == "父亲失踪之谜"
    )
    assert father.last_advanced_chapter == 5
    summary5 = (
        await session.execute(select(ChapterSummary).where(ChapterSummary.chapter_number == 5))
    ).scalar_one()
    assert summary5.author_summary and summary5.reader_summary
    assert (await session.get(StorySnapshot, uuid.UUID(result5.snapshot_id))).after_chapter_number == 5
    novel_after = await session.get(Novel, novel.id)
    assert novel_after.current_chapter_number == 5

    # ================= Chapter 6 preview: world carries forward =================
    provider.enqueue_structured(_plan_dict(world, 6))
    provider.enqueue_structured(_scene_batch(world, 6))
    for text in _writer_texts(6):
        provider.enqueue_text(text + "第六章的后续展开。")
    provider.enqueue_structured(
        _memory_batch_for(world, str(mirror_fact.id), 6)
    )
    package6 = await generation.generate_preview(novel.id, "第六章继续查探玉佩线索。", chapter_number=6)

    # ch6 planning was based on ch5 after-hash
    assert package6.story_state_hash == result5.after_story_state_hash
    # previous chapter ending comes from the COMMITTED chapter 5
    from app.context.builder import ContextBuilder as _CB

    writer_context6 = await _CB(session).build_writer_context(
        novel.id,
        chapter_number=6,
        pov_character_id=uuid.UUID(str(world["characters"]["林凡"].id)),
    )
    assert writer_context6.recent_context.previous_chapter_ending is not None
    assert "共鸣" in writer_context6.recent_context.previous_chapter_ending

    # ================= Chapter 6 finalize =================
    result6 = await generation.finalize(package6)
    assert result6.chapter_number == 6 and result6.duplicate is False
    assert result6.before_story_state_hash == result5.after_story_state_hash
    novel_final = await session.get(Novel, novel.id)
    assert novel_final.current_chapter_number == 6
    snapshots = (await session.execute(select(StorySnapshot))).scalars().all()
    assert {s.after_chapter_number for s in snapshots} == {5, 6}
    commits = (await session.execute(select(ChapterCommit))).scalars().all()
    assert {c.chapter_number for c in commits} == {5, 6}


async def _create_novel_direct(session):
    from tests.helpers import create_novel

    return await create_novel(session)


def _plan_dict(world, chapter_number: int) -> dict:
    from tests.draft_helpers import build_valid_plan_dict

    plan = build_valid_plan_dict(world)
    plan["chapter_number"] = chapter_number
    return plan
