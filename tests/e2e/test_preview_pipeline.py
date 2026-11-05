"""Phase 7.5 — Preview End-to-End Integration Audit.

Runs the FULL production pipeline across Phases 0-7 with FakeLLM only:

    bootstrap world -> user direction -> ChapterPlanningService (Director)
    -> plan validation -> WriterSafePlan -> ScenePlannerAgent
    -> scene validation -> ChapterDraftService (per-scene Writer)
    -> ChapterDraft -> ContinuityService.check / targeted repair
    -> clean draft -> MemoryExtractionService -> validation -> dry-run
    -> Memory Preview

ZERO canonical mutation. No commit/apply/finalize endpoints exist.
"""

from __future__ import annotations

import hashlib
import json
import pathlib

import pytest
from sqlalchemy import select

from app.agents.memory_extractor import MemoryExtractorAgent
from app.agents.scene_planner import ScenePlannerAgent
from app.agents.writer import WriterAgent
from app.agents.writer_repair import WriterRepairAgent
from app.continuity.models import ContinuityReport  # noqa: F401
from app.continuity.repair_redaction import REDACTED_MARKER
from app.debug import PreviewExportService
from app.draft.models import SceneDraft  # noqa: F401
from app.llm.client import AgentLLMClient
from app.llm.fake import FakeLLMProvider
from app.llm.prompts import get_default_registry
from app.memory.models import (
    CharacterStateChange,
    EventCandidate,
    EvidenceSpan,
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
    Character,
    CharacterKnowledge,
    CharacterState,
    Event,
    EventEffect,
    Foreshadowing,
    Item,
    Novel,
    PlotDebt,
    PlotThread,
    Relationship,
    StoryFact,
    StorySnapshot,
)
from app.planning.scene_validator import ScenePlanValidator
from app.schemas.agent_outputs import ChapterPlan, EntityRef, ScenePlan
from app.services.chapter_draft import ChapterDraftService
from app.services.chapter_planning import ChapterPlanningService, build_director_agent
from app.services.continuity import ContinuityService
from app.services.memory_extraction import (
    BlockingContinuityError,
    MemoryExtractionService,
)
from tests.draft_helpers import build_valid_plan_dict
from tests.helpers import SECRET_STATEMENT, create_novel, create_rich_world

USER_DIRECTION = (
    "下一章让林凡和苏青进入天剑城拍卖会。"
    "让林凡发现一枚与轮回古镜有关的玉佩，并遇到一个来自中州的神秘女子。"
    "这一章推进父亲失踪伏笔，但只能给出模糊线索，不能揭露真正去向。"
    "中途让苏青为了保护林凡受伤。不要让林凡在本章突破。"
)

MIRROR_FACT_STATEMENT = "林凡持有轮回古镜"

# fake writer prose: real Chinese narrative consumed by checker + extractor
SCENE_TEXTS = {
    "scene_1": (
        "天剑城拍卖场人声鼎沸。林凡在雅间落座，注意到角落里一位来自中州的"
        "神秘女子。她报上名号：顾清璃，目光却始终停留在展台方向。"
    ),
    "scene_2": (
        "玉佩作为第七件拍品被呈上展台。林凡瞳孔微缩——玉佩边缘的纹路竟与"
        "轮回古镜背面的刻痕如出一辙。顾清璃缓缓举牌，与林凡展开竞价。"
    ),
    "scene_3": (
        "一名散修突然暴起，伸手抓向玉佩。苏青一步挡在林凡身前，硬受了一记"
        "掌劲，肩头顿时渗出血迹。混乱中，林凡怀中的轮回古镜滑落出来，苏青"
        "看见了那面古镜。护卫赶到，散修被制服。"
    ),
    "scene_4": (
        "玉佩最终落入林凡手中。回到住处，他将玉佩贴近轮回古镜，两面古物"
        "发出一阵轻微的共鸣。玉佩上的纹路指向一个模糊的方向，父亲失踪之谜"
        "似乎又多了一条线索。林凡收起古镜，决定明日启程查探。"
    ),
}

LEAK_SENTENCE = "林凡已经确定，玄清真人是魔教卧底。"

# repaired scene 3: leak removed, plot points (and extractable evidence) kept
REPAIRED_SCENE_3 = (
    "一名散修突然暴起，伸手抓向玉佩。苏青一步挡在林凡身前，硬受了一记"
    "掌劲，肩头顿时渗出血迹。混乱中，林凡怀中的轮回古镜滑落出来，苏青"
    "看见了那面古镜。护卫赶到，散修被制服。"
)


def _scene_plans(world) -> list[ScenePlan]:
    lin = str(world["characters"]["林凡"].id)
    su = str(world["characters"]["苏青"].id)
    participants = {
        "scene_1": [EntityRef(character_id=lin, name="林凡"),
                    EntityRef(temporary_ref="new_character_1", name="神秘女子")],
        "scene_2": [EntityRef(character_id=lin, name="林凡"),
                    EntityRef(temporary_ref="new_character_1", name="神秘女子")],
        "scene_3": [EntityRef(character_id=lin, name="林凡"),
                    EntityRef(character_id=su, name="苏青")],
        "scene_4": [EntityRef(character_id=lin, name="林凡")],
    }
    beats = {"scene_1": ["1"], "scene_2": ["1"], "scene_3": ["2"], "scene_4": ["2"]}
    plans = []
    for index, scene_id in enumerate(
        ["scene_1", "scene_2", "scene_3", "scene_4"], start=1
    ):
        plans.append(
            ScenePlan(
                scene_id=scene_id,
                scene_index=index,
                source_beat_ids=beats[scene_id],
                pov=EntityRef(character_id=lin, name="林凡"),
                location=EntityRef(name="天剑城"),
                participants=participants[scene_id],
                scene_goal=f"场景{index}",
                required_outcomes=[],
                target_chars=800,
                temporary_entities=["new_character_1"]
                if "神秘女子" in [p.name for p in participants[scene_id]]
                else [],
            )
        )
    return plans


def _scene_batch(world) -> dict:
    return {
        "chapter_number": 5,
        "scenes": [s.model_dump(exclude_none=True) for s in _scene_plans(world)],
        "planner_notes": [],
    }


def _memory_batch(world, mirror_fact_id: str) -> MemoryUpdateBatch:
    su = str(world["characters"]["苏青"].id)
    father_id = str(world["result"].plot_thread_ids["父亲失踪之谜"])
    return MemoryUpdateBatch(
        operations=[
            MemoryUpdateOperation(
                operation_id="op-event", update_type=MemoryUpdateType.create_event,
                source_scene_id="scene_3",
                evidence=[EvidenceSpan(
                    scene_id="scene_3",
                    exact_quote="苏青一步挡在林凡身前，硬受了一记掌劲，肩头顿时渗出血迹。")],
                event=EventCandidate(
                    event_type="combat",
                    author_summary="苏青替林凡挡下一击并受伤",
                    reader_safe_summary="苏青替林凡挡下一击，受了伤",
                    participant_ids=[su],
                    importance=0.8,
                    visibility=MemoryVisibility(reader_visible=True),
                ),
            ),
            MemoryUpdateOperation(
                operation_id="op-injury", update_type=MemoryUpdateType.character_state_update,
                source_scene_id="scene_3",
                evidence=[EvidenceSpan(scene_id="scene_3", exact_quote="肩头顿时渗出血迹")],
                character_state=CharacterStateChange(
                    character_id=su, field="physical_condition",
                    proposed_new_value="injured",
                    new_value_description="肩头受掌劲，出血",
                    requires_confirmation=True,
                ),
            ),
            MemoryUpdateOperation(
                operation_id="op-know", update_type=MemoryUpdateType.character_knowledge_update,
                source_scene_id="scene_3",
                evidence=[EvidenceSpan(scene_id="scene_3", exact_quote="苏青看见了那面古镜")],
                knowledge=KnowledgeChange(character_id=su, fact_id=mirror_fact_id, status="known"),
            ),
            MemoryUpdateOperation(
                operation_id="op-thread", update_type=MemoryUpdateType.plot_thread_update,
                source_scene_id="scene_4",
                evidence=[EvidenceSpan(
                    scene_id="scene_4",
                    exact_quote="父亲失踪之谜似乎又多了一条线索")],
                plot_thread=PlotThreadChange(plot_thread_id=father_id, action="advanced"),
            ),
            MemoryUpdateOperation(
                operation_id="op-entity", update_type=MemoryUpdateType.propose_entity,
                source_scene_id="scene_1",
                evidence=[EvidenceSpan(
                    scene_id="scene_1",
                    exact_quote="她报上名号：顾清璃，目光却始终停留在展台方向。")],
                proposed_entity=ProposedCanonicalEntity(
                    temporary_ref="new_character_1", entity_type="character",
                    proposed_name="顾清璃",
                    attributes={"origin": "中州", "gender": "女"},
                ),
            ),
        ],
        candidate_author_summary="苏青挡剑受伤；古镜秘密暴露给苏青；玉佩推进父亲线。",
        candidate_reader_summary="拍卖会上玉佩引出争夺，苏青为林凡挡下攻击受伤，玉佩与古镜产生共鸣。",
    )


# ------------------------------------------------------------- infrastructure

_TABLES = [
    Novel, Chapter, Character, CharacterState, CharacterKnowledge, Event,
    EventEffect, StoryFact, Item, Relationship, PlotThread, Foreshadowing,
    PlotDebt, StorySnapshot,
]


def _row_dict(row) -> dict:
    from datetime import UTC as _UTC, datetime as _datetime

    out = {}
    for column in row.__table__.columns:
        value = getattr(row, column.name)
        if isinstance(value, _datetime):
            # normalize: tz-aware defaults vs naive DB round-trips must hash equal
            if value.tzinfo is not None:
                value = value.astimezone(_UTC).replace(tzinfo=None)
            out[column.name] = value.isoformat()
        elif hasattr(value, "isoformat"):
            out[column.name] = value.isoformat()
        else:
            out[column.name] = value
    return out


async def db_fingerprint(session) -> str:
    """Deep fingerprint: per-table row CONTENT hashes, not just counts
    (an UPDATE would keep row counts identical)."""
    payload = {}
    for model in _TABLES:
        rows = (await session.execute(select(model))).scalars().all()
        serialized = sorted(
            json.dumps(_row_dict(r), sort_keys=True, ensure_ascii=False, default=str)
            for r in rows
        )
        payload[model.__name__] = serialized
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _build_agents(session, provider):
    director = build_director_agent(
        session,
        AgentLLMClient(provider, agent_name="director", model="fake-model", temperature=0.5),
    )
    scene_planner = ScenePlannerAgent(
        llm=AgentLLMClient(provider, agent_name="scene_planner", model="fake-model"),
        prompt_registry=get_default_registry(),
        validator=ScenePlanValidator(session),
    )
    writer = WriterAgent(
        llm=AgentLLMClient(provider, agent_name="writer", model="fake-model", temperature=0.85),
        prompt_registry=get_default_registry(),
    )
    repair = WriterRepairAgent(
        llm=AgentLLMClient(provider, agent_name="writer_repair", model="fake-model"),
        prompt_registry=get_default_registry(),
    )
    extractor = MemoryExtractorAgent(
        contract_version=2,  # explicit offline legacy replay
        llm=AgentLLMClient(provider, agent_name="memory_extractor", model="fake-model", temperature=0.1),
        prompt_registry=get_default_registry(),
    )
    return director, scene_planner, writer, repair, extractor


def _prompts_of(provider, system_prefix: str) -> list[str]:
    out = []
    for call in provider.calls:
        if call and call[0].role == "system" and call[0].content.startswith(system_prefix):
            out.append("\n".join(m.content for m in call))
    return out


async def _world(session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    # the mirror fact (secret; 林凡 naturally knows his own item)
    mirror_fact = await world["service"].add_fact(
        novel.id, MIRROR_FACT_STATEMENT, secrecy="secret"
    )
    await world["service"].grant_knowledge(
        world["characters"]["林凡"].id, mirror_fact.id
    )
    return novel, world, mirror_fact


def _enqueue_draft_generation(provider, world, *, leak_scene_3: bool = False):
    provider.enqueue_structured(_scene_batch(world))
    provider.enqueue_text(SCENE_TEXTS["scene_1"])
    provider.enqueue_text(SCENE_TEXTS["scene_2"])
    provider.enqueue_text(
        SCENE_TEXTS["scene_3"] + (LEAK_SENTENCE if leak_scene_3 else "")
    )
    provider.enqueue_text(SCENE_TEXTS["scene_4"])


# ------------------------------------------------------------------- test 1

async def test_happy_path_full_preview_pipeline(session, tmp_path):
    novel, world, mirror_fact = await _world(session)
    fingerprint_before = await db_fingerprint(session)

    provider = FakeLLMProvider()
    provider.enqueue_structured(build_valid_plan_dict(world))
    _, scene_planner, writer, _repair, _extractor = _build_agents(session, provider)
    planning_service = ChapterPlanningService(
        session, director=build_director_agent(
            session,
            AgentLLMClient(provider, agent_name="director", model="fake-model"),
        )
    )

    # 1. planning (Director inside the production service)
    planning = await planning_service.generate_preview(
        novel.id, USER_DIRECTION, chapter_number=5
    )
    plan: ChapterPlan = planning.plan
    assert planning.validation_report.valid
    assert any(p.temporary_ref == "new_character_1" for p in plan.proposed_entities)
    assert any(r.policy == "hint_only" for r in plan.revelations)
    plan_dump = json.dumps(plan.model_dump(mode="json"), ensure_ascii=False)
    assert "苏青" in plan_dump
    assert "神秘女子" in plan_dump or any(
        p.name_hint == "神秘女子" for p in plan.proposed_entities
    )
    assert "突破" not in json.dumps(plan.expected_state_changes, ensure_ascii=False)

    # 2. draft (ScenePlanner + per-scene Writer inside the production service)
    _enqueue_draft_generation(provider, world)
    draft_service = ChapterDraftService(
        session, scene_planner, writer, previous_scene_tail_chars=200
    )
    draft = await draft_service.generate_preview(novel.id, plan)

    assert draft.status == "preview"
    assert len(draft.scenes) == 4
    assert "苏青" in draft.scenes[2].text and "渗出血迹" in draft.scenes[2].text
    assert "顾清璃" in draft.scenes[0].text
    assert "共鸣" in draft.scenes[3].text
    assert "突破" not in draft.full_text
    assert SECRET_STATEMENT not in draft.full_text

    # 3. continuity check (deterministic layer)
    continuity = ContinuityService(
        session, llm_checker=None, repair_agent=None, mode="deterministic_only"
    )
    report = await continuity.check_draft(novel.id, draft, plan)
    assert report.valid, f"{[i.code for i in report.issues if i.blocking]}"

    # 4. memory extraction + validation + dry-run
    provider.enqueue_structured(
        _memory_batch(world, str(mirror_fact.id)).model_dump(exclude_none=True)
    )
    extractor_service = MemoryExtractionService(
        session,
        MemoryExtractorAgent(
        contract_version=2,  # explicit offline legacy replay
            llm=AgentLLMClient(provider, agent_name="memory_extractor", model="fake-model"),
            prompt_registry=get_default_registry(),
        ),
    )
    memory = await extractor_service.extract_preview(novel.id, draft, plan, report)

    assert memory.validation_report.valid, (
        f"{[i.code for i in memory.validation_report.errors()]}"
    )
    assert memory.dry_run is not None
    assert memory.dry_run.applicable

    # dry-run after-state contains the expected changes (§9)
    after = memory.dry_run.proposed_after_state
    su_state = next(c for c in after["characters"] if c["name"] == "苏青")
    assert su_state["state"]["physical_condition"] == "injured"
    assert any(
        k["character_id"] == str(world["characters"]["苏青"].id)
        for k in after["knowledge_updates"]
    )
    assert any(
        e["author_summary"] == "苏青替林凡挡下一击并受伤"
        for e in after["candidate_events"]
    )
    thread = next(t for t in after["plot_threads"] if t["title"] == "父亲失踪之谜")
    assert thread["status"] in ("active", "progressing")
    assert thread["last_advanced_chapter"] == 5
    assert after["proposed_entities"][0]["proposed_name"] == "顾清璃"

    # 5. canonical DB zero mutation (deep content fingerprint)
    assert await db_fingerprint(session) == fingerprint_before

    # 6. secret isolation across the whole pipeline (§11)
    mirror_secret = MIRROR_FACT_STATEMENT
    for prompt in _prompts_of(provider, "# SYSTEM — Scene Planner"):
        assert SECRET_STATEMENT not in prompt and mirror_secret not in prompt
    for prompt in _prompts_of(provider, "# SYSTEM — Xuanhuan Chapter Writer"):
        assert SECRET_STATEMENT not in prompt, "Writer 不得看到与 POV 无关的 secret"
    for prompt in _prompts_of(provider, "# SYSTEM — Scene Repair Writer"):
        assert SECRET_STATEMENT not in prompt
    # author-level agents MAY see secrets
    assert any(SECRET_STATEMENT in p for p in _prompts_of(provider, "# SYSTEM — Story Director"))
    assert any(
        SECRET_STATEMENT in p
        for p in _prompts_of(provider, "# SYSTEM — Story Memory Extractor")
    )

    # 7. provenance chain (§12)
    assert planning.context_hash == draft.story_context_hash
    assert draft.chapter_plan_hash == hashlib.sha256(
        plan.model_dump_json().encode("utf-8")
    ).hexdigest()
    assert report.draft_hash == hashlib.sha256(draft.full_text.encode()).hexdigest()
    assert memory.draft_hash == report.draft_hash

    # 8. the DRAFT chapter (5) and snapshots are not persisted
    chapters = (await session.execute(select(Chapter))).scalars().all()
    assert all(c.number != 5 for c in chapters), "draft 不得写入 chapters"
    assert (await session.execute(select(StorySnapshot))).scalars().all() == []

    # 9. development-only artifact export (Phase 7.6): DTO -> files, no DB
    exporter = PreviewExportService(tmp_path)
    export1 = exporter.export(
        novel_id=str(novel.id),
        draft=draft,
        chapter_plan=plan,
        continuity_report=report,
        planning_result=planning,
        memory_result=memory,
    )
    export2 = exporter.export(
        novel_id=str(novel.id),
        draft=draft,
        chapter_plan=plan,
        continuity_report=report,
        planning_result=planning,
        memory_result=memory,
    )
    assert export1.run_id != export2.run_id, "每次 run 必须唯一，不得覆盖"

    expected_files = {
        "chapter.txt", "chapter_plan.json", "scene_plans.json",
        "continuity_report.json", "memory_preview.json", "dry_run.json",
        "metadata.json",
    }
    assert set(export1.files) == expected_files
    chapter_txt = (pathlib.Path(export1.path) / "chapter.txt").read_text(encoding="utf-8")
    assert chapter_txt == draft.full_text
    # chapter.txt contains the actual fake-writer prose
    for marker in ("顾清璃", "苏青一步挡在林凡身前", "肩头顿时渗出血迹",
                   "苏青看见了那面古镜", "发出一阵轻微的共鸣"):
        assert marker in chapter_txt, f"chapter.txt 缺少正文片段: {marker}"
    assert SECRET_STATEMENT not in chapter_txt
    metadata = json.loads((pathlib.Path(export1.path) / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["status"] == "preview"
    assert metadata["novel_id"] == str(novel.id)
    assert metadata["chapter_number"] == 5
    assert metadata["story_state_hash"] == draft.story_context_hash
    assert metadata["chapter_plan_hash"] == draft.chapter_plan_hash
    assert metadata["draft_hash"] == hashlib.sha256(draft.full_text.encode()).hexdigest()
    assert "writer_v2" in metadata["prompt_versions"]
    assert "director_v2" in metadata["prompt_versions"]
    assert "scene_planner_v1" in metadata["prompt_versions"]
    from app.agents.memory_extractor import PROMPT_VERSION as memory_prompt_version
    assert memory_prompt_version in metadata["prompt_versions"]

    # export is filesystem-only: canonical DB fingerprint still unchanged
    assert await db_fingerprint(session) == fingerprint_before


# ------------------------------------------------------------------- test 2

async def test_continuity_repair_path_integrated(session):
    """#7: leaky scene -> deterministic detection -> redacted targeted repair
    -> re-check -> valid; memory extraction runs on the repaired draft."""
    novel, world, mirror_fact = await _world(session)
    fingerprint_before = await db_fingerprint(session)

    provider = FakeLLMProvider()
    provider.enqueue_structured(build_valid_plan_dict(world))
    _, scene_planner, writer, repair, extractor = _build_agents(session, provider)
    planning_service = ChapterPlanningService(
        session, director=build_director_agent(
            session,
            AgentLLMClient(provider, agent_name="director", model="fake-model"),
        )
    )
    planning = await planning_service.generate_preview(
        novel.id, USER_DIRECTION, chapter_number=5
    )
    plan = planning.plan

    _enqueue_draft_generation(provider, world, leak_scene_3=True)
    draft_service = ChapterDraftService(
        session, scene_planner, writer, previous_scene_tail_chars=200
    )
    draft = await draft_service.generate_preview(novel.id, plan)
    assert "玄清真人是魔教卧底" in draft.scenes[2].text

    # continuity repair (deterministic detection + redacted targeted repair)
    provider.enqueue_text(REPAIRED_SCENE_3)
    continuity = ContinuityService(
        session, llm_checker=None, repair_agent=repair, mode="deterministic_only"
    )
    repair_result = await continuity.repair_preview(novel.id, draft, plan)

    assert repair_result.report.valid, (
        f"{[i.code for i in repair_result.report.issues if i.blocking]}"
    )
    assert repair_result.exhausted is False
    assert repair_result.repair_history[0].scene_ids_repaired == ["scene_3"]
    repaired_text = next(
        s.text for s in repair_result.draft.scenes if s.scene_id == "scene_3"
    )
    assert "玄清真人是魔教卧底" not in repaired_text
    # plot points preserved (§55)
    assert "挡在林凡身前" in repaired_text

    # capture: repair prompt contains the redaction marker, never the secret
    repair_prompts = _prompts_of(provider, "# SYSTEM — Scene Repair Writer")
    assert repair_prompts
    for prompt in repair_prompts:
        assert "玄清真人是魔教卧底" not in prompt
        assert REDACTED_MARKER in prompt

    # memory extraction runs on the CLEAN repaired draft + fresh report;
    # evidence quotes still exist in the repaired text (repair preserved them)
    provider.enqueue_structured(
        _memory_batch(world, str(mirror_fact.id)).model_dump(exclude_none=True)
    )
    extractor_service = MemoryExtractionService(session, extractor)
    memory = await extractor_service.extract_preview(
        novel.id, repair_result.draft, plan, repair_result.report
    )
    assert memory.validation_report.valid, (
        f"{[i.code for i in memory.validation_report.errors()]}"
    )
    assert memory.dry_run is not None

    assert await db_fingerprint(session) == fingerprint_before


# ------------------------------------------------------------------- test 3

async def test_continuity_repair_exhausted_blocks_memory_pipeline(session):
    """#13: repair fails twice -> exhausted; extractor zero calls; DB unchanged."""
    novel, world, mirror_fact = await _world(session)
    fingerprint_before = await db_fingerprint(session)

    provider = FakeLLMProvider()
    provider.enqueue_structured(build_valid_plan_dict(world))
    _, scene_planner, writer, repair, _extractor = _build_agents(session, provider)
    planning_service = ChapterPlanningService(
        session, director=build_director_agent(
            session,
            AgentLLMClient(provider, agent_name="director", model="fake-model"),
        )
    )
    planning = await planning_service.generate_preview(
        novel.id, USER_DIRECTION, chapter_number=5
    )
    plan = planning.plan

    _enqueue_draft_generation(provider, world, leak_scene_3=True)
    draft_service = ChapterDraftService(
        session, scene_planner, writer, previous_scene_tail_chars=200
    )
    draft = await draft_service.generate_preview(novel.id, plan)

    # repair keeps re-leaking -> exhausted after 2 rounds
    provider.enqueue_text("修复失败。" + LEAK_SENTENCE)
    provider.enqueue_text("再次修复失败。" + LEAK_SENTENCE)
    continuity = ContinuityService(
        session, llm_checker=None, repair_agent=repair, mode="deterministic_only"
    )
    repair_result = await continuity.repair_preview(novel.id, draft, plan)

    assert repair_result.exhausted is True
    assert repair_result.report.valid is False
    assert len(repair_result.repair_history) == 2

    # memory pipeline must refuse: zero extractor calls
    extractor_provider = FakeLLMProvider()  # separate counter; nothing enqueued
    extractor_service = MemoryExtractionService(
        session,
        MemoryExtractorAgent(
        contract_version=2,  # explicit offline legacy replay
            llm=AgentLLMClient(
                extractor_provider, agent_name="memory_extractor", model="fake-model"
            ),
            prompt_registry=get_default_registry(),
        ),
    )
    with pytest.raises(BlockingContinuityError):
        await extractor_service.extract_preview(
            novel.id, repair_result.draft, plan, repair_result.report
        )
    assert extractor_provider.calls == []

    assert await db_fingerprint(session) == fingerprint_before


# ------------------------------------------------------------------- test 4

async def test_invalid_memory_extraction_path(session):
    """#14: hallucinated evidence -> invalid report, no dry-run, DB unchanged."""
    novel, world, mirror_fact = await _world(session)
    fingerprint_before = await db_fingerprint(session)

    provider = FakeLLMProvider()
    provider.enqueue_structured(build_valid_plan_dict(world))
    _, scene_planner, writer, _repair, extractor = _build_agents(session, provider)
    planning_service = ChapterPlanningService(
        session, director=build_director_agent(
            session,
            AgentLLMClient(provider, agent_name="director", model="fake-model"),
        )
    )
    planning = await planning_service.generate_preview(
        novel.id, USER_DIRECTION, chapter_number=5
    )
    plan = planning.plan

    _enqueue_draft_generation(provider, world)
    draft_service = ChapterDraftService(session, scene_planner, writer)
    draft = await draft_service.generate_preview(novel.id, plan)

    continuity = ContinuityService(
        session, llm_checker=None, repair_agent=None, mode="deterministic_only"
    )
    report = await continuity.check_draft(novel.id, draft, plan)
    assert report.valid

    # fake extractor hallucinates an event that never happened in the draft
    hallucinated = MemoryUpdateBatch(
        operations=[
            MemoryUpdateOperation(
                operation_id="op-hallucinated",
                update_type=MemoryUpdateType.create_event,
                source_scene_id="scene_1",
                evidence=[EvidenceSpan(
                    scene_id="scene_1", exact_quote="林凡一剑杀死陈海。")],
                event=EventCandidate(
                    event_type="death",
                    author_summary="林凡杀死陈海",
                    reader_safe_summary="林凡杀死陈海",
                    importance=0.9,
                ),
            )
        ]
    )
    provider.enqueue_structured(hallucinated.model_dump(exclude_none=True))
    extractor_service = MemoryExtractionService(session, extractor)
    memory = await extractor_service.extract_preview(novel.id, draft, plan, report)

    assert not memory.validation_report.valid
    assert any(
        i.code == "evidence_not_in_draft" for i in memory.validation_report.issues
    )
    assert memory.dry_run is None
    assert await db_fingerprint(session) == fingerprint_before


# ------------------------------------------------------------------- test 5

async def test_provenance_hash_mismatch_fails_pipeline(session):
    """#12: a plan from a different story state must fail the draft stage."""
    novel, world, mirror_fact = await _world(session)
    provider = FakeLLMProvider()
    provider.enqueue_structured(build_valid_plan_dict(world))
    _, scene_planner, writer, _repair, _extractor = _build_agents(session, provider)
    planning_service = ChapterPlanningService(
        session, director=build_director_agent(
            session,
            AgentLLMClient(provider, agent_name="director", model="fake-model"),
        )
    )
    planning = await planning_service.generate_preview(
        novel.id, USER_DIRECTION, chapter_number=5
    )
    plan = planning.plan
    calls_before_draft = len(provider.calls)

    draft_service = ChapterDraftService(session, scene_planner, writer)
    with pytest.raises(Exception) as excinfo:
        await draft_service.generate_preview(
            novel.id, plan, expected_context_hash="0" * 64
        )
    assert "state" in str(excinfo.value).lower()
    assert len(provider.calls) == calls_before_draft, (
        "staleness gate must fire before any LLM call"
    )
