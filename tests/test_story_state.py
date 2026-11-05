"""Phase 1 acceptance tests: canonical story state domain."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app.models import Constraint, Foreshadowing, Item, PlotDebt
from app.models.enums import (
    AliveStatus,
    DebtStatus,
    ForeshadowStatus,
    ItemStatus,
    KnowledgeStatus,
    ThreadStatus,
)
from app.services.power import RealmOrderError
from app.services.story_state import StoryStateError
from tests.helpers import create_character_with_state, create_novel, create_world


# ---------------------------------------------------------------- characters

async def test_create_character_and_realm(session):
    novel = await create_novel(session)
    world = await create_world(session, novel.id)
    assert set(world["characters"]) == {"林凡", "苏青", "玄清真人"}

    lin_fan = world["characters"]["林凡"]
    state = await world["service"].get_current_state(lin_fan.id)
    assert state is not None
    assert state.alive_status == AliveStatus.alive
    assert state.realm.name == "筑基"
    assert state.subrealm == "后期"


async def test_update_character_state_appends_new_version(session):
    novel = await create_novel(session)
    world = await create_world(session, novel.id)
    service = world["service"]
    su_qing = world["characters"]["苏青"]

    updated = await service.update_character_state(
        su_qing.id,
        chapter_number=5,
        physical_condition="重伤",
        location_name="天剑城",
    )

    assert updated.chapter_number == 5
    assert updated.physical_condition == "重伤"
    # history preserved: chapter-0 row still intact
    states = await world["service"].characters.list_states(su_qing.id)
    assert len(states) == 2
    assert states[0].chapter_number == 0
    assert states[0].physical_condition is None
    current = await service.get_current_state(su_qing.id)
    assert current.chapter_number == 5


async def test_state_rows_are_immutable_no_duplicate_chapter(session):
    novel = await create_novel(session)
    world = await create_world(session, novel.id)
    lin_fan = world["characters"]["林凡"]
    with pytest.raises(StoryStateError):
        await world["service"].update_character_state(lin_fan.id, chapter_number=0)


async def test_death_state_recorded(session):
    novel = await create_novel(session)
    world = await create_world(session, novel.id)
    service = world["service"]
    lin_fan = world["characters"]["林凡"]
    await service.update_character_state(
        lin_fan.id, chapter_number=3, alive_status=AliveStatus.dead
    )
    current = await service.get_current_state(lin_fan.id)
    assert current.alive_status == AliveStatus.dead


# ------------------------------------------------------------------- events

async def test_event_log_append_only_with_effects(session):
    novel = await create_novel(session)
    world = await create_world(session, novel.id)
    service = world["service"]
    lin_fan = world["characters"]["林凡"]
    chen_hai = await create_character_with_state(session, novel.id, "陈海")

    event = await service.record_event(
        novel.id,
        chapter_number=35,
        event_type="combat",
        title="黑风谷之战",
        summary="林凡击败陈海，但没有杀死他。",
        participant_ids=[lin_fan.id, chen_hai.id],
        importance=0.8,
        effects=[
            {
                "target_type": "character",
                "target_id": str(chen_hai.id),
                "field": "injury",
                "old_value": "none",
                "new_value": "serious",
            }
        ],
    )
    assert event.chapter_number == 35
    assert len(event.effects) == 1
    assert event.effects[0].new_value == "serious"

    events = await service.events.list_for_novel(novel.id)
    assert len(events) == 1


# ---------------------------------------------------------------- knowledge

async def test_villain_only_secret_isolation(session):
    """『玄清真人是魔教卧底』：反派知道，主角不知道，苏青怀疑。"""
    novel = await create_novel(session)
    world = await create_world(session, novel.id)
    service = world["service"]

    lin_fan = world["characters"]["林凡"]
    su_qing = world["characters"]["苏青"]
    xuan_qing = world["characters"]["玄清真人"]

    lin_visible = {f.statement for f in await service.visible_facts_for(lin_fan.id)}
    xuan_visible = {f.statement for f in await service.visible_facts_for(xuan_qing.id)}
    su_visible = {f.statement for f in await service.visible_facts_for(su_qing.id)}

    secret = "玄清真人是魔教卧底"
    assert secret not in lin_visible, "POV 角色不能看到他不知道的秘密"
    assert secret not in su_visible, "怀疑不等于知道"
    assert secret in xuan_visible, "知道该秘密的角色可以看到"

    # knowledge rows reflect status
    facts = await service.facts.list_for_novel(novel.id)
    secret_fact = next(f for f in facts if f.statement == secret)
    su_knowledge = await service.characters.get_knowledge(su_qing.id, secret_fact.id)
    assert su_knowledge is not None
    assert su_knowledge.status == KnowledgeStatus.suspected


async def test_grant_knowledge_then_visible(session):
    novel = await create_novel(session)
    world = await create_world(session, novel.id)
    service = world["service"]
    lin_fan = world["characters"]["林凡"]
    facts = await service.facts.list_for_novel(novel.id)
    secret_fact = next(f for f in facts if f.statement == "玄清真人是魔教卧底")

    await service.grant_knowledge(lin_fan.id, secret_fact.id, learned_chapter=12)
    visible = {f.statement for f in await service.visible_facts_for(lin_fan.id)}
    assert "玄清真人是魔教卧底" in visible


# ------------------------------------------------------------- plot threads

async def test_plot_thread_and_foreshadowing_lifecycle(session):
    novel = await create_novel(session)
    world = await create_world(session, novel.id)
    service = world["service"]

    threads = await service.threads.list_threads(novel.id)
    father = next(t for t in threads if t.title == "父亲失踪之谜")
    assert father.status == ThreadStatus.active
    assert father.truth == "林父主动进入轮回秘境寻找某人"

    foreshadow = await service.foreshadows.add(
        Foreshadowing(
            novel_id=novel.id,
            plot_thread_id=father.id,
            setup="林凡发现父亲留下的半块玉佩",
            intended_payoff="揭示父亲进入轮回秘境的真正原因",
            status=ForeshadowStatus.planted,
            planted_chapter=3,
        )
    )
    unresolved = await service.foreshadows.list_unresolved(novel.id)
    assert foreshadow in unresolved

    # pay it off -> no longer unresolved
    foreshadow.status = ForeshadowStatus.paid_off
    foreshadow.payoff_chapter = 30
    unresolved = await service.foreshadows.list_unresolved(novel.id)
    assert foreshadow not in unresolved


async def test_plot_debt_lifecycle(session):
    novel = await create_novel(session)
    world = await create_world(session, novel.id)
    service = world["service"]

    debt = await service.debts.add(
        PlotDebt(
            novel_id=novel.id,
            debt_type="promise",
            description="七日后宗门大会",
            created_chapter=10,
            due_chapter=14,
        )
    )
    open_debts = await service.debts.list_open(novel.id)
    assert debt in open_debts

    debt.status = DebtStatus.fulfilled
    debt.resolved_chapter = 13
    assert debt not in await service.debts.list_open(novel.id)


async def test_constraint_window_filtering(session):
    novel = await create_novel(session)
    world = await create_world(session, novel.id)
    service = world["service"]

    active_all = await service.constraints.list_active(novel.id)
    assert len(active_all) == 2
    assert all(c.type == "hard" for c in active_all)

    # a constraint that only applies to chapters 100..150
    await service.constraints.add(
        Constraint(
            novel_id=novel.id,
            type="hard",
            statement="第150章以前不能揭示轮回古镜真相",
            start_chapter=100,
            end_chapter=150,
        )
    )
    active_at_50 = await service.constraints.list_active(novel.id, chapter_number=50)
    assert all("轮回古镜" not in c.statement for c in active_at_50)
    active_at_120 = await service.constraints.list_active(novel.id, chapter_number=120)
    assert any("轮回古镜" in c.statement for c in active_at_120)


# -------------------------------------------------------------- power rules

async def test_realm_ordering_and_illegal_jump(session):
    novel = await create_novel(session)
    world = await create_world(session, novel.id)
    service = world["service"]
    lin_fan = world["characters"]["林凡"]

    # 筑基(3) -> 金丹(4): one-rank breakthrough is legal
    await service.update_character_state(lin_fan.id, chapter_number=8, realm_name="金丹")

    # 金丹(4) -> 元婴(5) again legal; but a two-rank jump is rejected
    su_qing = world["characters"]["苏青"]
    with pytest.raises(RealmOrderError):
        await service.update_character_state(
            su_qing.id, chapter_number=8, realm_name="元婴"
        )


# ------------------------------------------------------------------- items

async def test_item_ownership_recorded(session):
    novel = await create_novel(session)
    world = await create_world(session, novel.id)
    service = world["service"]
    lin_fan = world["characters"]["林凡"]

    mirror = await service.items.add(
        Item(
            novel_id=novel.id,
            name="轮回古镜",
            aliases=["古镜"],
            type="artifact",
            grade="仙品",
            current_owner_character_id=lin_fan.id,
            current_location_id=None,
            status=ItemStatus.intact,
            secret_fact_ids=[],
        )
    )
    found = await service.items.find_by_name(novel.id, "古镜")
    assert found is not None
    assert found.id == mirror.id
    assert found.current_owner_character_id == lin_fan.id


# ---------------------------------------------------------------- snapshot

async def test_snapshot_serializable(session):
    novel = await create_novel(session)
    world = await create_world(session, novel.id)
    service = world["service"]

    snapshot = await service.create_snapshot(novel.id, after_chapter_number=1)
    # JSON round-trip must work
    raw = json.dumps(snapshot.serialized_state, ensure_ascii=False)
    assert len(raw) > 0
    assert snapshot.state_hash and len(snapshot.state_hash) == 64

    assert snapshot.serialized_state["novel"]["title"] == "九霄问道"
    names = {c["name"] for c in snapshot.serialized_state["characters"]}
    assert names == {"林凡", "苏青", "玄清真人"}
    lin = next(c for c in snapshot.serialized_state["characters"] if c["name"] == "林凡")
    assert lin["state"]["realm"] == "筑基"

    latest = await service.snapshots.latest_for_novel(novel.id)
    assert latest.id == snapshot.id
