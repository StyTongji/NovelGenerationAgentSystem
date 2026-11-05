"""DirectorAgent tests: happy path, bounded plan repair, provenance,
no-DB-write guarantee. All driven by FakeLLMProvider."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select

from app.agents.director import DirectorAgent
from app.context.builder import ContextBuilder
from app.llm.client import AgentLLMClient
from app.llm.fake import FakeLLMProvider
from app.llm.prompts import get_default_registry
from app.planning.validator import ChapterPlanValidator
from tests.helpers import create_novel, create_rich_world


async def _setup(session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    builder = ContextBuilder(session)
    context = await builder.build_director_context(
        novel.id,
        chapter_number=5,
        user_direction="下一章去拍卖会，推进轮回古镜线，但不要揭露父亲去向。",
    )
    provider = FakeLLMProvider()
    llm = AgentLLMClient(provider, agent_name="director", model="fake-model", temperature=0.5)
    agent = DirectorAgent(
        llm=llm,
        prompt_registry=get_default_registry(),
        validator=ChapterPlanValidator(session),
        max_plan_repairs=2,
    )
    return novel, world, context, provider, agent


def _valid_plan_dict(world) -> dict:
    lin_fan = world["characters"]["林凡"]
    threads = world["result"].plot_thread_ids
    father_id = str(threads["父亲失踪之谜"])
    return {
        "chapter_number": 5,
        "chapter_goal": "通过拍卖会引入玉佩，推进父亲失踪线",
        "participating_characters": [{"character_id": str(lin_fan.id), "name": "林凡"}],
        "primary_locations": [{"name": "天剑城"}],
        "proposed_entities": [
            {"entity_type": "character", "temporary_ref": "new_character_1",
             "name_hint": "神秘女子", "role": "来自中州"}
        ],
        "scenes": [
            {"index": 1, "goal": "到达拍卖会", "participants": ["林凡"], "location": "天剑城", "beats": ["入场"]},
            {"index": 2, "goal": "玉佩出现", "participants": ["林凡", "神秘女子"], "location": "天剑城", "beats": ["竞价"]},
        ],
        "plot_threads_to_progress": [
            {"plot_thread_id": father_id, "action": "advance", "instruction": "玉佩带来新线索"}
        ],
        "revelations": [
            {"policy": "hint_only", "allowed_information": "玉佩与父亲旧物相关",
             "forbidden_information": "父亲真实去向"}
        ],
        "must_not_reveal": ["父亲真实去向"],
        "ending_hook": "玉佩与轮回古镜短暂共鸣",
        "conflicts": [],
        "director_notes": {
            "strategic_rationale": "为下卷引出中州势力",
            "secret_context": ["父亲实际进入轮回秘境"],
        },
    }


def _invalid_realm_plan_dict(world) -> dict:
    lin_fan = world["characters"]["林凡"]
    plan = _valid_plan_dict(world)
    plan["character_state_expectations"] = [
        {"character_id": str(lin_fan.id), "expectation": "林凡当场突破元婴"}
    ]
    return plan


async def test_happy_path_valid_plan_with_provenance(session):
    novel, world, context, provider, agent = await _setup(session)
    provider.enqueue_structured(_valid_plan_dict(world))

    result = await agent.generate_plan(context)

    assert result.validation_report.valid
    assert result.repair_count == 0
    assert result.metadata is not None
    assert result.metadata.provider == "fake"
    assert result.metadata.agent == "director"
    assert result.prompt_version == "director_v2"
    assert len(result.context_hash) == 64
    assert result.based_on_chapter_number == 5


async def test_writer_safe_view_excludes_director_notes(session):
    novel, world, context, provider, agent = await _setup(session)
    provider.enqueue_structured(_valid_plan_dict(world))
    result = await agent.generate_plan(context)

    dump = result.writer_safe_plan.model_dump_json()
    assert "director_notes" not in type(result.writer_safe_plan).model_fields
    assert "为下卷引出中州势力" not in dump
    assert "父亲实际进入轮回秘境" not in dump, "director-only secret 不得进入 writer-safe plan"
    # writer-facing instructions survive
    assert result.writer_safe_plan.ending_hook == "玉佩与轮回古镜短暂共鸣"
    assert result.writer_safe_plan.revelations[0].policy == "hint_only"


async def test_domain_plan_repair_succeeds_once(session):
    """Task #32: first plan breaks realm rules, repair fixes it."""
    novel, world, context, provider, agent = await _setup(session)
    provider.enqueue_structured(_invalid_realm_plan_dict(world), _valid_plan_dict(world))

    result = await agent.generate_plan(context)

    assert result.validation_report.valid
    assert result.repair_count == 1
    # the repair prompt contained the validation issues
    repair_call = provider.calls[-1]
    assert any("realm_jump_violation" in m.content for m in repair_call)


async def test_domain_plan_repair_exhaustion_returns_invalid_report(session):
    novel, world, context, provider, agent = await _setup(session)
    bad = _invalid_realm_plan_dict(world)
    provider.enqueue_structured(bad, bad, bad)

    result = await agent.generate_plan(context)

    assert not result.validation_report.valid
    assert result.repair_count == 2  # bounded, no infinite loop


async def test_hard_constraint_repair(session):
    """Task #33: Su Qing death -> repaired to serious injury but alive."""
    novel, world, context, provider, agent = await _setup(session)
    lin_fan = world["characters"]["林凡"]
    bad = _valid_plan_dict(world)
    bad["required_events"] = ["苏青为掩护林凡战死沙场"]
    bad["participating_characters"] = [
        {"character_id": str(lin_fan.id), "name": "林凡"},
    ]
    good = _valid_plan_dict(world)
    good["required_events"] = ["苏青为掩护林凡重伤", "苏青保住性命"]
    provider.enqueue_structured(bad, good)

    result = await agent.generate_plan(context)

    assert result.validation_report.valid
    assert result.repair_count == 1
    assert all("战死" not in e for e in result.plan.required_events)


async def test_generate_plan_does_not_touch_database(session):
    """Task #20/#41: preview generation is read-only."""
    novel, world, context, provider, agent = await _setup(session)
    provider.enqueue_structured(_valid_plan_dict(world))

    from app.models import Chapter, ChapterSummary, Event, StorySnapshot

    async def counts():
        out = {}
        for model in (Chapter, Event, ChapterSummary, StorySnapshot):
            total = await session.execute(select(func.count()).select_from(model))
            out[model.__name__] = total.scalar_one()
        return out

    before = await counts()
    await agent.generate_plan(context)
    after = await counts()
    assert before == after


async def test_user_direction_wrapped_not_concatenated_into_system(session):
    novel, world, context, provider, agent = await _setup(session)
    provider.enqueue_structured(_valid_plan_dict(world))
    await agent.generate_plan(context)

    # system prompt is exactly the prompt file; user direction lives in the
    # delimited USER_DIRECTION block of the user message only
    first_call = provider.calls[0]
    system_msg = first_call[0]
    assert system_msg.role == "system"
    assert "下一章去拍卖会" not in system_msg.content
    user_msg = next(m for m in first_call if m.role == "user")
    assert "<<<USER_DIRECTION" in user_msg.content
    assert "下一章去拍卖会" in user_msg.content
