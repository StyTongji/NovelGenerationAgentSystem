"""ChapterDraftService pipeline tests: continuity, isolation, safety.

Prompt capture tests (§52/§53) assert on what the MODEL actually received,
not just on intermediate DTOs.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from app.agents.scene_planner import ScenePlannerAgent
from app.agents.writer import WriterAgent
from app.context.builder import ContextBuilder
from app.draft.models import ChapterDraftGenerationError, StalePlanError
from app.llm.client import AgentLLMClient
from app.llm.errors import ProviderRequestError
from app.llm.fake import FakeLLMProvider
from app.llm.prompts import get_default_registry
from app.models import Chapter, CharacterState, Event, Item, PlotDebt, PlotThread
from app.planning.scene_validator import ScenePlanValidator
from app.services.chapter_draft import ChapterDraftService
from app.schemas.agent_outputs import ChapterPlan
from tests.draft_helpers import build_scene_batch_dict, build_valid_plan_dict
from tests.helpers import SECRET_STATEMENT, create_novel, create_rich_world


def _make_agents(session, provider):
    planner = ScenePlannerAgent(
        llm=AgentLLMClient(provider, agent_name="scene_planner", model="fake-model"),
        prompt_registry=get_default_registry(),
        validator=ScenePlanValidator(session),
    )
    writer = WriterAgent(
        llm=AgentLLMClient(provider, agent_name="writer", model="fake-model", temperature=0.85),
        prompt_registry=get_default_registry(),
    )
    return planner, writer


def _service(session, provider):
    planner, writer = _make_agents(session, provider)
    return ChapterDraftService(session, planner, writer, previous_scene_tail_chars=200)


def _writer_calls(provider):
    """Provider calls whose system prompt is the writer prompt."""
    out = []
    for call in provider.calls:
        if call and call[0].role == "system" and call[0].content.startswith(
            "# SYSTEM — Xuanhuan Chapter Writer"
        ):
            out.append(call)
    return out


async def _db_counts(session):
    out = {}
    for model in (Chapter, Event, CharacterState, PlotThread, PlotDebt, Item):
        total = await session.execute(select(func.count()).select_from(model))
        out[model.__name__] = total.scalar_one()
    return out


# ------------------------------------------------------------ full pipeline


async def test_chapter_draft_full_pipeline_preview(session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    plan = ChapterPlan.model_validate(build_valid_plan_dict(world))
    provider = FakeLLMProvider()
    provider.enqueue_structured(build_scene_batch_dict(world))
    provider.enqueue_text("场景一正文：林凡入场，神秘女子坐在角落。" * 5)
    provider.enqueue_text("场景二正文：玉佩登场，与古镜共鸣。" * 5)
    service = _service(session, provider)

    draft = await service.generate_preview(novel.id, plan)

    assert draft.status == "preview"
    assert len(draft.scenes) == 2
    assert "场景一正文" in draft.full_text and "场景二正文" in draft.full_text
    assert len(draft.chapter_plan_hash) == 64
    assert len(draft.story_context_hash) == 64
    assert draft.temporary_entities[0].temporary_ref == "new_character_1"
    agents = [m.agent for m in draft.generation_metadata]
    assert agents[0] == "scene_planner", "场景规划元数据应记录在 draft provenance 中"
    assert agents.count("writer") == 2


async def test_l_canonical_db_unchanged_after_draft(session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    plan = ChapterPlan.model_validate(build_valid_plan_dict(world))
    provider = FakeLLMProvider()
    provider.enqueue_structured(build_scene_batch_dict(world))
    provider.enqueue_text("场景一。")
    provider.enqueue_text("场景二。")
    service = _service(session, provider)

    before = await _db_counts(session)
    thread_before = {
        t.id: t.status for t in (await session.execute(select(PlotThread))).scalars().all()
    }
    await service.generate_preview(novel.id, plan)
    after = await _db_counts(session)
    thread_after = {
        t.id: t.status for t in (await session.execute(select(PlotThread))).scalars().all()
    }
    assert before == after
    assert thread_before == thread_after


async def test_i_previous_scene_ending_flows_to_next_scene(session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    plan = ChapterPlan.model_validate(build_valid_plan_dict(world))
    provider = FakeLLMProvider()
    provider.enqueue_structured(build_scene_batch_dict(world))
    scene1_text = "开头部分应该被截断。" + "填充" * 300 + "结尾标记：林凡握紧玉佩。"
    provider.enqueue_text(scene1_text)
    provider.enqueue_text("场景二正文。")
    service = _service(session, provider)

    await service.generate_preview(novel.id, plan)

    writer_calls = _writer_calls(provider)
    assert len(writer_calls) == 2
    scene2_prompt = "\n".join(m.content for m in writer_calls[1])
    assert "结尾标记：林凡握紧玉佩。" in scene2_prompt
    assert "开头部分应该被截断。" not in scene2_prompt, "不得把上一场景全文塞回"


async def test_j_no_context_explosion_across_scenes(session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    plan = ChapterPlan.model_validate(build_valid_plan_dict(world))
    provider = FakeLLMProvider()
    batch = build_scene_batch_dict(world)
    batch["scenes"] = batch["scenes"][:1] + [
        {**batch["scenes"][1], "source_beat_ids": ["2"], "scene_index": 2}
    ]
    # build a 5-scene plan manually via 5 scenes in batch
    big_batch = build_scene_batch_dict(world)
    scene_template = big_batch["scenes"][0]
    big_batch["scenes"] = [
        {**scene_template, "scene_id": f"scene_{i}", "scene_index": i, "source_beat_ids": ["1", "2"]}
        for i in range(1, 6)
    ]
    provider.enqueue_structured(big_batch)
    for i in range(1, 6):
        # long enough that the 200-char tail excludes the leading marker
        provider.enqueue_text(f"SCENE_{i}_MARKER" + "叙事内容。" * 100)
    service = _service(session, provider)

    await service.generate_preview(novel.id, plan)

    writer_calls = _writer_calls(provider)
    scene5_prompt = "\n".join(m.content for m in writer_calls[4])
    for i in (1, 2, 3, 4):
        assert f"SCENE_{i}_MARKER" not in scene5_prompt, (
            f"scene {i} 全文不得进入 scene 5 的 prompt"
        )


async def test_k_temporary_entity_consistent_across_scenes(session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    plan = ChapterPlan.model_validate(build_valid_plan_dict(world))
    provider = FakeLLMProvider()
    provider.enqueue_structured(build_scene_batch_dict(world))
    provider.enqueue_text("场景一：神秘女子首次现身。")
    provider.enqueue_text("场景二：神秘女子再次出手。")
    service = _service(session, provider)

    draft = await service.generate_preview(novel.id, plan)

    # working memory keeps one registration, introduced in scene 1
    assert len(draft.temporary_entities) == 1
    entity = draft.temporary_entities[0]
    assert entity.temporary_ref == "new_character_1"
    assert entity.introduced_in_scene == "scene_1"
    # both writer prompts reference the same temporary entity, no UUID invented
    writer_calls = _writer_calls(provider)
    for call in writer_calls:
        prompt = "\n".join(m.content for m in call)
        assert "new_character_1" in prompt
        assert "神秘女子" in prompt


async def test_m_writer_failure_mid_chapter_raises_error_db_unchanged(session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    plan = ChapterPlan.model_validate(build_valid_plan_dict(world))
    provider = FakeLLMProvider()
    provider.enqueue_structured(build_scene_batch_dict(world))
    provider.enqueue_text("场景一成功。")
    provider.enqueue_text(ProviderRequestError("LLM provider down", status_code=503))
    service = _service(session, provider)

    before = await _db_counts(session)
    with pytest.raises(ChapterDraftGenerationError) as excinfo:
        await service.generate_preview(novel.id, plan)
    after = await _db_counts(session)

    assert before == after
    assert excinfo.value.failed_scene_id == "scene_2"
    assert [d.scene_id for d in excinfo.value.completed_scene_drafts] == ["scene_1"]
    assert excinfo.value.completed_scene_drafts[0].text == "场景一成功。"


async def test_invalid_plan_rejected_before_any_generation(session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    plan_dict = build_valid_plan_dict(world)
    plan_dict["required_events"] = ["苏青战死沙场"]  # hard constraint violation
    plan = ChapterPlan.model_validate(plan_dict)
    provider = FakeLLMProvider()  # nothing enqueued: no LLM call may happen
    service = _service(session, provider)

    with pytest.raises(ChapterDraftGenerationError, match="hard_constraint_violation"):
        await service.generate_preview(novel.id, plan)
    assert provider.calls == []


async def test_stale_plan_detected_via_context_hash(session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    plan = ChapterPlan.model_validate(build_valid_plan_dict(world))
    provider = FakeLLMProvider()
    service = _service(session, provider)

    with pytest.raises(StalePlanError):
        await service.generate_preview(novel.id, plan, expected_context_hash="0" * 64)


# ------------------------------------------------- multi-POV knowledge firewall


async def test_o_multi_pov_secret_per_scene(session):
    """#35: 林凡 -> 魔教长老 -> 林凡。secret 只在第二个 scene 的 writer prompt 出现。"""
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    elder = world["elder"]
    plan = ChapterPlan.model_validate(build_valid_plan_dict(world))
    provider = FakeLLMProvider()
    batch = build_scene_batch_dict(world, pov_ids=[
        str(world["characters"]["林凡"].id),
        str(elder.id),
        str(world["characters"]["林凡"].id),
    ])
    # batch has 2 scenes by default; rebuild with 3
    template = batch["scenes"][0]
    batch["scenes"] = [
        {**template, "scene_id": "scene_1", "scene_index": 1, "source_beat_ids": ["1"],
         "pov": {"character_id": str(world["characters"]["林凡"].id), "name": "林凡"}},
        {**template, "scene_id": "scene_2", "scene_index": 2, "source_beat_ids": ["2"],
         "pov": {"character_id": str(elder.id), "name": "魔教长老"}},
        {**template, "scene_id": "scene_3", "scene_index": 3, "source_beat_ids": ["2"],
         "pov": {"character_id": str(world["characters"]["林凡"].id), "name": "林凡"}},
    ]
    provider.enqueue_structured(batch)
    provider.enqueue_text("场景一。")
    provider.enqueue_text("场景二。")
    provider.enqueue_text("场景三。")
    service = _service(session, provider)

    draft = await service.generate_preview(novel.id, plan)

    writer_calls = _writer_calls(provider)
    assert len(writer_calls) == 3
    prompts = ["\n".join(m.content for m in call) for call in writer_calls]
    assert SECRET_STATEMENT not in prompts[0], "林凡 POV 不得看到秘密"
    assert SECRET_STATEMENT in prompts[1], "魔教长老 POV 应看到秘密"
    assert SECRET_STATEMENT not in prompts[2], "回到林凡 POV 后秘密必须再次消失"


# --------------------------------------------------- writer boundary captures


async def test_capture_writer_prompt_never_contains_author_secrets(session):
    """§53: capture the FINAL writer request for all secret sources."""
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    plan_dict = build_valid_plan_dict(world)
    # user-direction style secret lives ONLY in director_notes/forbidden info
    plan = ChapterPlan.model_validate(plan_dict)
    provider = FakeLLMProvider()
    provider.enqueue_structured(build_scene_batch_dict(world))
    provider.enqueue_text("场景一。")
    provider.enqueue_text("场景二。")
    service = _service(session, provider)

    await service.generate_preview(novel.id, plan)

    for index, call in enumerate(_writer_calls(provider), start=1):
        prompt = "\n".join(m.content for m in call)
        assert "父亲仍然活着" not in prompt, f"scene {index}: director secret leaked"
        assert "轮回秘境" not in prompt, f"scene {index}: forbidden_information leaked"
        assert "为下卷引出中州势力" not in prompt, f"scene {index}: director_notes leaked"
        assert "父亲仍然活着并进入了轮回秘境" not in prompt
        # thread truth never reaches writer context
        assert "林父主动进入轮回秘境寻找某人" not in prompt


async def test_p_previous_chapter_ending_scrubbed_for_pov(session):
    """#34/P: reader knows the secret from previous chapter; 林凡 POV must
    not receive it via the previous chapter ending."""
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    # previous chapter ends with the secret revealed to the reader
    session.add(
        Chapter(
            novel_id=novel.id, number=4, version=1, status="final",
            title="第4章",
            body="正文。" * 20 + f"章末：魔教长老确认，{SECRET_STATEMENT}。",
        )
    )
    await session.flush()

    builder = ContextBuilder(session)
    context = await builder.build_writer_context(
        novel.id, chapter_number=5, pov_character_id=world["characters"]["林凡"].id
    )
    ending = context.recent_context.previous_chapter_ending
    assert ending is not None
    assert SECRET_STATEMENT not in ending
    assert "……" in ending  # masked

    # POV who knows the secret sees it unmasked
    elder_context = await builder.build_writer_context(
        novel.id, chapter_number=5, pov_character_id=world["elder"].id
    )
    assert SECRET_STATEMENT in elder_context.recent_context.previous_chapter_ending


async def test_g_raw_user_direction_never_reaches_writer(session):
    """#37/#1: build_writer_context has no raw-direction parameter at all."""
    import inspect

    from app.context.builder import ContextBuilder

    params = inspect.signature(ContextBuilder.build_writer_context).parameters
    assert "user_direction" not in params
    from app.context.models import WriterContext

    assert "user_direction" not in WriterContext.model_fields
