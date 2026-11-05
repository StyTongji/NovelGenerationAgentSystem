"""ContextBuilder assembly tests: threads, debts, constraints, budget,
previous chapter ending, determinism, debug."""

from __future__ import annotations

import uuid

from app.context import ContextBudget, ContextBuilder
from app.context.budget import trim_events
from app.context.models import EventView
from tests.helpers import create_character_with_state, create_novel, create_rich_world


# ------------------------------------------------------------------ threads (F)


async def test_f_plot_thread_views_and_staleness(session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    builder = ContextBuilder(session)

    context = await builder.build_director_context(novel.id, chapter_number=20)
    father = next(
        t for t in context.active_plot_threads if t.title == "父亲失踪之谜"
    )
    assert father.status == "active"
    # never advanced since introduction (ch1 threads) -> 19 chapters stale
    assert father.chapters_since_last_progress == 19

    # resolved threads default not in active/dormant lists
    threads = await builder.threads.list_threads(novel.id)
    threads[0].status = "resolved"
    context2 = await builder.build_director_context(novel.id, chapter_number=20)
    assert all(t.id != str(threads[0].id) for t in context2.active_plot_threads)
    assert all(t.id != str(threads[0].id) for t in context2.dormant_plot_threads)


async def test_f_dormant_thread_enters_dormant_list(session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    threads = await world["service"].threads.list_threads(novel.id)
    threads[0].status = "dormant"

    builder = ContextBuilder(session)
    context = await builder.build_director_context(novel.id, chapter_number=10)
    assert any(t.id == str(threads[0].id) for t in context.dormant_plot_threads)
    assert all(t.id != str(threads[0].id) for t in context.active_plot_threads)


# -------------------------------------------------------------------- debts (G)


async def test_g_plot_debt_urgency(session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    builder = ContextBuilder(session)

    context = await builder.build_director_context(novel.id, chapter_number=6)
    by_desc = {d.description: d for d in context.open_plot_debts}
    # overdue (due ch3 < 6)
    assert by_desc["与陈海的比武之约"].urgency == "overdue"
    # due soon (7 <= 6+3)
    assert by_desc["七日后宗门大会"].urgency == "due_soon"
    # no deadline
    assert by_desc["欠药铺掌柜一个人情"].urgency == "no_deadline"
    # fulfilled debt not among open debts
    assert "归还借来的灵石" not in by_desc
    # overdue sorts first
    assert context.open_plot_debts[0].urgency == "overdue"


async def test_g_writer_gets_open_debts_via_director_only(session):
    """Plot debts are planning information; WriterContext has no such field."""
    assert "open_plot_debts" not in __import__(
        "app.context.models", fromlist=["WriterContext"]
    ).WriterContext.model_fields


# --------------------------------------------------------------- constraints (H)


async def test_h_constraint_window_filtering(session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    builder = ContextBuilder(session)

    # ch5: both hard constraints active (no window)
    context5 = await builder.build_director_context(novel.id, chapter_number=5)
    assert len(context5.hard_constraints) == 2
    assert all(c.type == "hard" for c in context5.hard_constraints)
    assert context5.soft_constraints == []

    # expired constraint does not enter
    from app.models import Constraint

    await world["service"].constraints.add(
        Constraint(novel_id=novel.id, type="soft", statement="本章多写对话",
                   start_chapter=1, end_chapter=3)
    )
    context5 = await builder.build_director_context(novel.id, chapter_number=5)
    assert all("本章多写对话" not in c.statement for c in context5.soft_constraints)
    context2 = await builder.build_director_context(novel.id, chapter_number=2)
    assert any("本章多写对话" in c.statement for c in context2.soft_constraints)


async def test_h_hard_constraints_survive_tiny_budget(session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    tiny_budget = ContextBudget(max_events=1, max_plot_threads=1, max_knowledge_facts=1)
    builder = ContextBuilder(session, budget=tiny_budget)

    context = await builder.build_writer_context(
        novel.id, chapter_number=5, pov_character_id=world["characters"]["林凡"].id
    )
    statements = {c.statement for c in context.hard_constraints}
    assert "苏青不能死亡" in statements
    assert "父亲真实去向在150章前不得完全揭露" in statements


# ------------------------------------------------------------------- budget (I)


async def _make_many_events(session, novel_id, service, lin_fan, count=30):
    for i in range(count):
        await service.record_event(
            novel_id, chapter_number=i + 1, event_type="exploration",
            summary=f"林凡在第{i + 1}章探索区域{i}，获得一些资源。",
            participant_ids=[lin_fan.id], importance=0.3,
        )


async def test_i_budget_caps_events_deterministically(session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    service = world["service"]
    lin_fan = world["characters"]["林凡"]
    await _make_many_events(session, novel.id, service, lin_fan, count=30)

    builder = ContextBuilder(session, budget=ContextBudget(max_events=5))
    context = await builder.build_writer_context(
        novel.id, chapter_number=31, pov_character_id=lin_fan.id
    )
    assert len(context.relevant_events) == 5

    ids_first = [e.id for e in context.relevant_events]
    context_again = await builder.build_writer_context(
        novel.id, chapter_number=31, pov_character_id=lin_fan.id
    )
    assert ids_first == [e.id for e in context_again.relevant_events], "输出必须稳定"
    # recent events preferred by recency
    chapters = sorted(e.chapter_number for e in context.relevant_events)
    assert chapters[-1] == 30


def test_i_trim_events_caps_and_keeps_high_scores():
    budget = ContextBudget(max_events=3, max_event_chars=10_000)
    events = [
        EventView(id=str(uuid.UUID(int=i)), chapter_number=i, event_type="x",
                  summary=f"event {i}", importance=0.5)
        for i in range(10)
    ]
    scores = {str(uuid.UUID(int=i)): float(i) for i in range(10)}
    kept = trim_events(events, scores, budget)
    assert len(kept) == 3
    assert [e.id for e in kept] == [str(uuid.UUID(int=i)) for i in (9, 8, 7)]


# ---------------------------------------------------- previous chapter (J)


async def test_j_previous_chapter_ending_is_tail_only(session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    builder = ContextBuilder(session, budget=ContextBudget(previous_chapter_tail_chars=30))

    context = await builder.build_writer_context(
        novel.id, chapter_number=4, pov_character_id=world["characters"]["林凡"].id
    )
    ending = context.recent_context.previous_chapter_ending
    assert ending is not None
    assert len(ending) <= 30
    assert ending.endswith("第3章结尾：林凡望向远方。")
    assert not ending.startswith("正文内容。"), "不得加载整章正文"


# ------------------------------------------------------------- determinism (K)


async def test_k_full_determinism_same_state_same_query(session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    builder = ContextBuilder(session)

    dump1 = (
        await builder.build_director_context(novel.id, chapter_number=5, user_direction="让林凡调查轮回古镜")
    ).model_dump_json()
    dump2 = (
        await builder.build_director_context(novel.id, chapter_number=5, user_direction="让林凡调查轮回古镜")
    ).model_dump_json()
    assert dump1 == dump2

    dump3 = (
        await builder.build_writer_context(
            novel.id, chapter_number=5, pov_character_id=world["characters"]["林凡"].id,
            writer_instruction="让林凡调查轮回古镜",
        )
    ).model_dump_json()
    dump4 = (
        await builder.build_writer_context(
            novel.id, chapter_number=5, pov_character_id=world["characters"]["林凡"].id,
            writer_instruction="让林凡调查轮回古镜",
        )
    ).model_dump_json()
    assert dump3 == dump4


# ------------------------------------------------------------ debug + misc


async def test_debug_report_records_inclusion_and_exclusion(session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    builder = ContextBuilder(session)

    await builder.build_writer_context(
        novel.id, chapter_number=5, pov_character_id=world["characters"]["林凡"].id
    )
    debug = builder.debug
    assert debug.included, "应记录进入上下文的内容"
    assert debug.excluded, "应记录被过滤的内容"
    assert any("does not know" in e.reason or "unknown" in e.reason for e in debug.excluded)


async def test_writer_context_assembles_core_fields(session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    builder = ContextBuilder(session)

    context = await builder.build_writer_context(
        novel.id, chapter_number=4, pov_character_id=world["characters"]["林凡"].id,
        writer_instruction="让林凡在黑风谷调查轮回古镜",
    )
    assert context.pov_character.name == "林凡"
    assert context.pov_character.state.realm == "筑基"
    assert context.location is not None and context.location.name == "黑风谷"
    # public foreshadowing visible, hidden not
    assert any("玉佩" in f.visible_hint for f in context.safe_foreshadowing)
    assert all("来历" not in f.visible_hint for f in context.safe_foreshadowing)
    # mirror item included via entity link
    assert any(i.name == "轮回古镜" for i in context.relevant_items)
    assert any(i.name == "轮回古镜" for i in context.relevant_items if i.owner_id == context.pov_character.id)


async def test_continuity_and_extraction_context_stubs(session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    builder = ContextBuilder(session)

    continuity = await builder.build_continuity_context(novel.id, 4, "本章正文……")
    assert continuity.canonical_characters
    assert "玄清真人是魔教卧底" in continuity.secret_fact_statements

    extraction = await builder.build_extraction_context(novel.id, 4, "本章正文……")
    assert any(c.name == "林凡" for c in extraction.character_states)
    assert any(c.character_name == "玄清真人" for c in extraction.character_knowledge)
    assert extraction.open_plot_debts
