"""Deterministic continuity checker regression tests (cases 1-10 + extras)."""

from __future__ import annotations

import uuid

import pytest

from app.context.builder import ContextBuilder
from app.continuity.deterministic import DeterministicContinuityChecker
from app.draft.models import SceneDraft
from app.schemas.agent_outputs import EntityRef, ScenePlan
from tests.draft_helpers import build_valid_plan_dict
from tests.helpers import (
    SECRET_STATEMENT,
    create_character_with_state,
    create_novel,
    create_rich_world,
)


async def _make_context(session, novel, world, scenes: list[ScenePlan], scene_texts: dict[str, str]):
    from app.draft.models import ChapterDraft
    from datetime import UTC, datetime

    builder = ContextBuilder(session)
    plan_dict = build_valid_plan_dict(world)
    plan = __import__("app.schemas.agent_outputs", fromlist=["ChapterPlan"]).ChapterPlan.model_validate(plan_dict)
    from app.draft.models import TemporaryEntityInfo

    draft = ChapterDraft(
        novel_id=str(novel.id),
        chapter_number=5,
        temporary_entities=[
            TemporaryEntityInfo(
                temporary_ref=p.temporary_ref,
                entity_type=p.entity_type,
                name_hint=p.name_hint,
                role=p.role,
            )
            for p in plan.proposed_entities
        ],
        scenes=[
            SceneDraft(scene_id=sp.scene_id, scene_index=sp.scene_index, text=scene_texts[sp.scene_id])
            for sp in scenes
        ],
        full_text="\n\n".join(scene_texts[sp.scene_id] for sp in scenes),
        chapter_plan_hash="0" * 64,
        story_context_hash="0" * 64,
        created_at=datetime.now(UTC),
    )
    context = await builder.build_continuity_context(
        novel.id,
        chapter_number=5,
        chapter_text=draft.full_text,
        chapter_plan=plan,
        scene_plans=scenes,
        draft=draft,
    )
    return context, draft, plan


def _lin_fan(world):
    return str(world["characters"]["林凡"].id)


def _scene(scene_id, index, world, *, pov="林凡", location="天剑城", temporary=None, mode=None, story_time=None) -> ScenePlan:
    return ScenePlan(
        scene_id=scene_id,
        scene_index=index,
        source_beat_ids=[str(index)],
        pov=EntityRef(character_id=_lin_fan(world) if pov == "林凡" else None, name=pov),
        location=EntityRef(name=location),
        participants=[EntityRef(name="林凡")],
        scene_goal="场景目标",
        narrative_mode=mode,
        story_time=story_time,
        temporary_entities=temporary or [],
        required_outcomes=[],
        target_chars=800,
    )


# ----------------------------------------------------------------- case 1

async def test_case1_dead_character_detected_flashback_ok(session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    await create_character_with_state(
        session, novel.id, "陈海",
        alive_status=__import__("app.models.enums", fromlist=["AliveStatus"]).AliveStatus.dead,
    )
    scenes = [_scene("s1", 1, world)]
    texts = {"s1": "陈海推门走了进来，与众人正常交谈。"}
    context, _draft, _plan = await _make_context(session, novel, world, scenes, texts)
    issues = DeterministicContinuityChecker().check(context)
    assert any(
        i.category.value == "dead_character" and i.severity.value == "critical" and i.scene_id == "s1"
        for i in issues
    )

    # flashback mode -> valid
    scenes_fb = [_scene("s1", 1, world, mode="flashback")]
    context_fb, _, _ = await _make_context(session, novel, world, scenes_fb, texts)
    issues_fb = DeterministicContinuityChecker().check(context_fb)
    assert not any(i.category.value == "dead_character" for i in issues_fb)


async def test_case2_item_ownership_detected(session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    scenes = [_scene("s1", 1, world, pov="苏青" if False else "林凡")]
    # scene participants only 林凡; 苏青 uses the mirror
    scenes[0].participants = [EntityRef(name="林凡")]
    texts = {"s1": "苏青取出自己的轮回古镜催动，镜光大盛。"}
    context, _, _ = await _make_context(session, novel, world, scenes, texts)
    issues = DeterministicContinuityChecker().check(context)
    assert any(i.category.value == "item_ownership" and i.severity.value == "error" for i in issues)


async def test_case3_ability_ownership_detected(session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    from app.models import Ability

    world["service"].session.add(
        Ability(novel_id=novel.id, name="玄天九剑", owner_character_id=world["characters"]["玄清真人"].id)
    )
    await world["service"].session.flush()
    scenes = [_scene("s1", 1, world)]
    texts = {"s1": "林凡施展玄天九剑，剑光漫天。"}
    context, _, _ = await _make_context(session, novel, world, scenes, texts)
    issues = DeterministicContinuityChecker().check(context)
    assert any(i.category.value == "ability_ownership" and i.severity.value == "error" for i in issues)


async def test_case4_realm_contradiction_detected(session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    scenes = [_scene("s1", 1, world)]
    texts = {"s1": "元婴境的林凡一掌拍出，虚空震颤。"}
    context, _, _ = await _make_context(session, novel, world, scenes, texts)
    issues = DeterministicContinuityChecker().check(context)
    assert any(i.category.value == "realm_state" and i.severity.value == "error" for i in issues)


async def test_case5_hard_constraint_death_critical(session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    scenes = [_scene("s1", 1, world)]
    texts = {"s1": "苏青魂飞魄散，形神俱灭。"}
    context, _, _ = await _make_context(session, novel, world, scenes, texts)
    issues = DeterministicContinuityChecker().check(context)
    assert any(
        i.category.value == "hard_constraint" and i.severity.value == "critical"
        for i in issues
    )


async def test_case6_explicit_knowledge_leak_critical(session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    # a secret fact 林凡 does not know
    fact = await world["service"].add_fact(
        novel.id, "林凡的父亲仍然活着", secrecy="secret"
    )
    scenes = [_scene("s1", 1, world)]
    texts = {"s1": "林凡知道父亲仍然活着，心中五味杂陈。"}
    context, _, _ = await _make_context(session, novel, world, scenes, texts)
    assert fact.statement in context.secret_fact_statements
    issues = DeterministicContinuityChecker().check(context)
    assert any(i.category.value == "knowledge_leak" and i.severity.value == "critical" for i in issues)


async def test_case8_reveal_policy_verbatim_leak_detected(session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    plan = __import__("app.schemas.agent_outputs", fromlist=["ChapterPlan"]).ChapterPlan.model_validate(
        {
            **build_valid_plan_dict(world),
            "revelations": [{
                "policy": "hint_only",
                "allowed_information": "行为异常",
                "forbidden_information": SECRET_STATEMENT,
            }],
        }
    )
    scenes = [_scene("s1", 1, world)]
    texts = {"s1": f"林凡终于确定，{SECRET_STATEMENT}，怒火中烧。"}
    context, _, _ = await _make_context(session, novel, world, scenes, texts)
    context = context.model_copy(update={"chapter_plan": plan})
    issues = DeterministicContinuityChecker().check(context)
    assert any(
        i.category.value == "reveal_policy" and i.severity.value == "critical" for i in issues
    )


async def test_case9_impossible_travel_detected(session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    scenes = [
        _scene("s1", 1, world, location="天剑城"),
        _scene("s2", 2, world, location="青玄大陆"),
    ]
    texts = {"s1": "拍卖会散场。", "s2": "片刻后林凡已身处万里之外的青云宗大殿。"}
    context, _, _ = await _make_context(session, novel, world, scenes, texts)
    issues = DeterministicContinuityChecker().check(context)
    assert any(i.category.value == "location_travel" and i.severity.value == "error" for i in issues)

    # with justification -> valid
    texts2 = {"s1": "拍卖会散场。", "s2": "片刻后林凡通过传送阵抵达万里之外的青云宗。"}
    context2, _, _ = await _make_context(session, novel, world, scenes, texts2)
    issues2 = DeterministicContinuityChecker().check(context2)
    assert not any(i.category.value == "location_travel" for i in issues2)


async def test_case10_repeated_first_acquisition_detected(session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    from app.schemas.agent_outputs import ScenePlan as SP

    from app.models import Item

    await world["service"].items.add(
        Item(novel_id=novel.id, name="玉佩", type="treasure")
    )
    scenes = [
        _scene("s1", 1, world),
        _scene("s2", 2, world),
    ]
    scenes[0].required_outcomes = ["林凡拍得玉佩"]
    texts = {"s1": "林凡成功拍得玉佩。", "s2": "台上再次展示，林凡第一次见到这枚正在拍卖的玉佩。"}
    context, _, _ = await _make_context(session, novel, world, scenes, texts)
    issues = DeterministicContinuityChecker().check(context)
    assert any(i.category.value == "scene_continuity" and i.severity.value == "error" for i in issues)


async def test_unauthorized_breakthrough_detected(session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    scenes = [_scene("s1", 1, world)]
    texts = {"s1": "轰的一声，林凡成功突破金丹，气息暴涨。"}
    context, _, _ = await _make_context(session, novel, world, scenes, texts)
    issues = DeterministicContinuityChecker().check(context)
    assert any(
        i.category.value == "illegal_breakthrough" and i.severity.value == "critical"
        for i in issues
    )


async def test_two_rank_breakthrough_critical(session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    scenes = [_scene("s1", 1, world)]
    texts = {"s1": "林凡竟直接突破元婴，天下震动。"}
    context, _, _ = await _make_context(session, novel, world, scenes, texts)
    issues = DeterministicContinuityChecker().check(context)
    assert any(
        i.category.value == "illegal_breakthrough" and "more than one rank" in i.author_explanation
        for i in issues
    )


async def test_temporary_entity_identity_conflict_detected(session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    scenes = [
        _scene("s1", 1, world, temporary=["new_character_1"]),
        _scene("s3", 3, world, temporary=["new_character_1"]),
    ]
    texts = {
        "s1": "来自中州的年轻女子站在角落。",
        "s3": "神秘女子再出现时，竟是一名来自北域的老者。",
    }
    context, _, _ = await _make_context(session, novel, world, scenes, texts)
    issues = DeterministicContinuityChecker().check(context)
    assert any(
        i.category.value == "proposed_entity_identity" and i.severity.value == "error"
        for i in issues
    )


async def test_timeline_regression_detected(session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    scenes = [
        _scene("s1", 1, world, story_time="黄昏"),
        _scene("s2", 2, world, story_time="正午"),
    ]
    texts = {"s1": "黄昏时分。", "s2": "正午时分。"}
    context, _, _ = await _make_context(session, novel, world, scenes, texts)
    issues = DeterministicContinuityChecker().check(context)
    assert any(i.category.value == "timeline" and i.severity.value == "error" for i in issues)


async def test_clean_draft_produces_no_issues(session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    scenes = [_scene("s1", 1, world)]
    texts = {"s1": "林凡在天剑城的拍卖会上安静观察，谨慎出价。"}
    context, _, _ = await _make_context(session, novel, world, scenes, texts)
    issues = DeterministicContinuityChecker().check(context)
    assert issues == []
