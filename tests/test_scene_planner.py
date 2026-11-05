"""ScenePlannerAgent + ScenePlanValidator tests (A, B, C, D, Q + escalation)."""

from __future__ import annotations

import uuid

import pytest

from app.agents.scene_planner import ScenePlannerAgent
from app.context.builder import ContextBuilder
from app.llm.client import AgentLLMClient
from app.llm.fake import FakeLLMProvider
from app.llm.prompts import get_default_registry
from app.planning.scene_validator import ScenePlanValidator
from app.schemas.agent_outputs import ChapterPlan, ScenePlanBatch
from tests.draft_helpers import build_scene_batch_dict, build_valid_plan_dict
from tests.helpers import create_novel, create_rich_world


@pytest.fixture
def planner(session):
    provider = FakeLLMProvider()
    llm = AgentLLMClient(provider, agent_name="scene_planner", model="fake-model", temperature=0.5)
    return (
        ScenePlannerAgent(
            llm=llm,
            prompt_registry=get_default_registry(),
            validator=ScenePlanValidator(session),
            max_plan_repairs=2,
        ),
        provider,
    )


async def _safe_plan(session, novel):
    builder = ContextBuilder(session)
    await builder.build_director_context(novel.id, chapter_number=5)
    from app.schemas.agent_outputs import ChapterPlan

    return novel, ChapterPlan.model_validate(build_valid_plan_dict_phase5())


def build_valid_plan_dict_phase5():
    # reuse the draft helper without a world (ids patched per test)
    return {
        "chapter_number": 5,
        "chapter_goal": "拍卖会推进",
        "participating_characters": [{"temporary_ref": "new_character_1", "name": "神秘女子"}],
        "proposed_entities": [
            {"entity_type": "character", "temporary_ref": "new_character_1", "name_hint": "神秘女子"}
        ],
        "scenes": [
            {"index": 1, "goal": "beat1", "participants": [], "location": "天剑城", "beats": []},
            {"index": 2, "goal": "beat2", "participants": [], "location": "天剑城", "beats": []},
        ],
        "ending_hook": "h",
        "conflicts": [],
    }


async def test_a_valid_plan_produces_ordered_scene_batch(session, planner):
    agent, provider = planner
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    plan = ChapterPlan.model_validate(build_valid_plan_dict(world))
    safe_plan = plan.to_writer_safe()
    batch_dict = build_scene_batch_dict(world)
    provider.enqueue_structured(batch_dict)

    result = await agent.plan_scenes(safe_plan, novel.id, 5)

    assert result.validation_report.valid, f"{[i.code for i in result.validation_report.errors()]}"
    batch = result.plan
    assert isinstance(batch, ScenePlanBatch)
    assert [s.scene_index for s in batch.scenes] == [1, 2]
    assert result.repair_count == 0
    assert result.metadata.agent == "scene_planner"


async def test_b_missing_beat_triggers_repair(session, planner):
    agent, provider = planner
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    plan = ChapterPlan.model_validate(build_valid_plan_dict(world))
    safe_plan = plan.to_writer_safe()

    incomplete = build_scene_batch_dict(world)
    incomplete["scenes"] = incomplete["scenes"][:1]  # misses beat 2
    complete = build_scene_batch_dict(world)
    provider.enqueue_structured(incomplete, complete)

    result = await agent.plan_scenes(safe_plan, novel.id, 5)

    assert result.validation_report.valid
    assert result.repair_count == 1
    repair_call = provider.calls[-1]
    assert any("missing_beat" in m.content for m in repair_call)


async def test_c_invalid_pov_rejected(session, planner):
    agent, provider = planner
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    plan = ChapterPlan.model_validate(build_valid_plan_dict(world))
    safe_plan = plan.to_writer_safe()

    batch = build_scene_batch_dict(world)
    batch["scenes"][0]["pov"] = {"character_id": str(uuid.uuid4()), "name": "不存在"}
    provider.enqueue_structured(batch, batch, batch)  # initial + 2 repairs

    result = await agent.plan_scenes(safe_plan, novel.id, 5)
    assert not result.validation_report.valid
    assert any(i.code == "unknown_character_id" for i in result.validation_report.issues)


async def test_d_undeclared_temporary_ref_rejected_declared_ok(session, planner):
    agent, provider = planner
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    plan = ChapterPlan.model_validate(build_valid_plan_dict(world))
    safe_plan = plan.to_writer_safe()

    # undeclared: scene uses new_character_9
    bad = build_scene_batch_dict(world)
    bad["scenes"][0]["temporary_entities"] = ["new_character_9"]
    provider.enqueue_structured(bad, bad, bad)
    result = await agent.plan_scenes(safe_plan, novel.id, 5)
    assert not result.validation_report.valid
    assert any(i.code == "undeclared_temporary_ref" for i in result.validation_report.issues)

    # declared: consistent with plan.proposed_entities
    good = build_scene_batch_dict(world)
    provider.enqueue_structured(good)
    result2 = await agent.plan_scenes(safe_plan, novel.id, 5)
    assert result2.validation_report.valid


async def test_reveal_scope_escalation_rejected(session, planner):
    agent, provider = planner
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    plan = ChapterPlan.model_validate(build_valid_plan_dict(world))
    safe_plan = plan.to_writer_safe()

    batch = build_scene_batch_dict(world)
    batch["scenes"][0]["allowed_information"] = ["父亲真实去向"]
    provider.enqueue_structured(batch, batch, batch)

    result = await agent.plan_scenes(safe_plan, novel.id, 5, author_plan=plan)
    assert not result.validation_report.valid
    assert any(i.code == "reveal_scope_escalation" for i in result.validation_report.issues)


async def test_q_scene_planning_deterministic(session, planner):
    agent, provider = planner
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    plan = ChapterPlan.model_validate(build_valid_plan_dict(world))

    provider.enqueue_structured(build_scene_batch_dict(world))
    first = await agent.plan_scenes(plan.to_writer_safe(), novel.id, 5)
    provider.enqueue_structured(build_scene_batch_dict(world))
    second = await agent.plan_scenes(plan.to_writer_safe(), novel.id, 5)
    assert first.plan.model_dump_json() == second.plan.model_dump_json()


async def test_scene_planner_never_sees_director_notes(session, planner):
    """#38: planner prompt built only from the writer-safe plan."""
    agent, provider = planner
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    plan = ChapterPlan.model_validate(build_valid_plan_dict(world))
    provider.enqueue_structured(build_scene_batch_dict(world))

    await agent.plan_scenes(plan.to_writer_safe(), novel.id, 5)

    planner_call = provider.calls[0]
    all_content = "\n".join(m.content for m in planner_call)
    assert "为下卷引出中州势力" not in all_content
    assert "父亲仍然活着" not in all_content
    assert "父亲仍然活着并进入了轮回秘境" not in all_content


async def test_scene_plan_schema_file_matches_model(session):
    import json
    from pathlib import Path

    from jsonschema import Draft202012Validator

    schema = json.loads(
        (Path(__file__).parent.parent / "schemas" / "scene_plan.schema.json").read_text(encoding="utf-8")
    )
    batch = ScenePlanBatch.model_validate(
        build_scene_batch_dict(None) if False else {
            "chapter_number": 5,
            "scenes": [
                {
                    "scene_id": "s1", "scene_index": 1, "source_beat_ids": ["1"],
                    "pov": {"temporary_ref": "new_character_1"},
                    "scene_goal": "g", "target_chars": 800,
                    "temporary_entities": ["new_character_1"],
                }
            ],
        }
    )
    Draft202012Validator(schema).validate(batch.model_dump(exclude_none=True))
