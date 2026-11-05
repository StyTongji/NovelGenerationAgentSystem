"""Knowledge isolation tests (Phase 3 critical acceptance).

The secret "玄清真人是魔教卧底" must not reach WriterContexts of characters
who do not know it — through ANY path: StoryFact, Event summary,
ChapterSummary, PlotThread, Foreshadowing, Relationship, Item, Location.
"""

from __future__ import annotations

import uuid

from app.context import KnowledgeFilter
from app.context.builder import ContextBuilder
from app.models.enums import KnowledgeStatus
from tests.helpers import SECRET_STATEMENT, create_novel, create_rich_world


def _assert_secret_absent(context_dump: str) -> None:
    assert SECRET_STATEMENT not in context_dump, (
        "POV-unknown secret leaked into writer context: " + context_dump[:2000]
    )
    # not even in obvious paraphrase form as a bare assertion
    assert "魔教卧底" not in context_dump


async def test_a_pov_knowledge_firewall_lin_fan_excluded(session):
    """林凡 (unknown): must see neither the fact nor any paraphrase."""
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    builder = ContextBuilder(session)

    context = await builder.build_writer_context(
        novel.id, chapter_number=5, pov_character_id=world["characters"]["林凡"].id
    )
    dump = context.model_dump_json()
    _assert_secret_absent(dump)

    # the event that mentions the secret is masked, not silently included
    assert all("魔教" not in e.summary or "……" in e.summary for e in context.relevant_events)
    # the tagged secret event is dropped entirely for 林凡
    assert str(world["events"]["secret"].id) not in {e.id for e in context.relevant_events}


async def test_a_pov_knowledge_firewall_su_qing_suspected(session):
    """苏青 (suspected): may see suspicion framing, never the plain fact."""
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    builder = ContextBuilder(session)

    context = await builder.build_writer_context(
        novel.id, chapter_number=5, pov_character_id=world["characters"]["苏青"].id
    )
    knowledge_texts = [f.content for f in context.pov_knowledge]
    suspicion = [t for t in knowledge_texts if SECRET_STATEMENT in t]
    assert suspicion, "苏青应得到怀疑框架下的信息"
    assert all("怀疑" in t and "未经确认" in t for t in suspicion), (
        "suspected 不得升级为确定事实"
    )


async def test_a_pov_knowledge_firewall_elder_known(session):
    """魔教长老 (known): may receive the fact as certain knowledge."""
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    builder = ContextBuilder(session)

    context = await builder.build_writer_context(
        novel.id, chapter_number=5, pov_character_id=world["elder"].id
    )
    knowledge_texts = [f.content for f in context.pov_knowledge]
    assert any(
        t == SECRET_STATEMENT for t in knowledge_texts
    ), "known 状态应作为确定知识提供"


async def test_b_reader_visibility_does_not_relax_pov_filter(session):
    """读者已知（revealed_to_reader_chapter）≠ 角色已知。"""
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    # reveal to reader in chapter 2, 林凡 still does not know
    world["secret_fact"].revealed_to_reader_chapter = 2
    await session.flush()

    builder = ContextBuilder(session)
    context = await builder.build_writer_context(
        novel.id, chapter_number=5, pov_character_id=world["characters"]["林凡"].id
    )
    _assert_secret_absent(context.model_dump_json())


async def test_c_forgotten_knowledge_excluded(session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    lin_fan = world["characters"]["林凡"]
    # 林凡 previously knew a (non-secret) fact, now forgot it
    fact = await world["service"].add_fact(
        novel.id, "林凡幼年在青石村长大", secrecy="limited"
    )
    await world["service"].grant_knowledge(lin_fan.id, fact.id, status=KnowledgeStatus.forgotten)

    builder = ContextBuilder(session)
    context = await builder.build_writer_context(
        novel.id, chapter_number=5, pov_character_id=lin_fan.id
    )
    knowledge_texts = [f.content for f in context.pov_knowledge]
    assert not any("青石村" in t for t in knowledge_texts), "forgotten 默认不得作为当前知识"

    # excluded reason is visible in the debug report
    assert any(
        "forgotten" in e.reason.lower() or "does not know" in e.reason
        for e in builder.debug.excluded
    )


async def test_d_believed_false_provides_wrong_belief_not_truth(session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    lin_fan = world["characters"]["林凡"]
    # world truth: treasure in 黑风谷; 林凡 wrongly believes it was taken away
    fact = await world["service"].add_fact(
        novel.id, "宝物藏在黑风谷深处的洞窟中", secrecy="secret"
    )
    await world["service"].grant_knowledge(
        lin_fan.id, fact.id, status=KnowledgeStatus.believed_false
    )

    builder = ContextBuilder(session)
    context = await builder.build_writer_context(
        novel.id, chapter_number=5, pov_character_id=lin_fan.id
    )
    knowledge_texts = [f.content for f in context.pov_knowledge]
    belief = [t for t in knowledge_texts if "黑风谷" in t and "洞窟" in t]
    assert belief, "Writer 应得到角色的（错误）认知"
    assert all("错误认知" in t or "是假的" in t for t in belief), (
        "believed_false 必须以角色认知框架呈现，而不是世界真相"
    )


async def test_event_summary_scrubbed_for_unaware_pov(session):
    """Event summary containing the secret is masked for 林凡, intact for
    魔教长老 via the KnowledgeFilter text scrubbing."""
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    kf_lin = KnowledgeFilter(session, novel.id)
    await kf_lin.load(world["characters"]["林凡"].id)
    scrubbed, masked = kf_lin.scrub_text(f"密会记录：{SECRET_STATEMENT}，此后行动。")
    assert SECRET_STATEMENT not in scrubbed
    assert "POV未知信息" in scrubbed
    assert masked == [SECRET_STATEMENT]

    kf_elder = KnowledgeFilter(session, novel.id)
    await kf_elder.load(world["elder"].id)
    scrubbed_elder, masked_elder = kf_elder.scrub_text(f"密会记录：{SECRET_STATEMENT}。")
    assert scrubbed_elder == f"密会记录：{SECRET_STATEMENT}。"
    assert masked_elder == []


async def test_author_summary_never_enters_writer_context(session):
    """ChapterSummary containing the secret exists, but WriterContext never
    receives author-level summaries at all."""
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    builder = ContextBuilder(session)

    context = await builder.build_writer_context(
        novel.id, chapter_number=5, pov_character_id=world["characters"]["林凡"].id
    )
    dump = context.model_dump_json()
    _assert_secret_absent(dump)
    # structurally: no summaries field at all
    assert "recent_chapter_summaries" not in type(context).model_fields
    assert "幕后" not in dump  # summary phrasing never leaks via any field

    # ...while Director sees them
    director = await builder.build_director_context(novel.id, chapter_number=5)
    assert director.recent_chapter_summaries
    assert any("幕后" in s.short_summary for s in director.recent_chapter_summaries)


async def test_relationship_hidden_description_never_leaks(session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    builder = ContextBuilder(session)
    context = await builder.build_writer_context(
        novel.id, chapter_number=5, pov_character_id=world["characters"]["林凡"].id
    )
    dump = context.model_dump_json()
    assert "hidden_description" not in type(context).model_fields
    assert "受魔教指派" not in dump
    # public description is fine
    assert any("并肩作战" in (r.public_description or "") for r in context.relevant_relationships)


async def test_director_may_see_truth_but_writer_never(session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    builder = ContextBuilder(session)

    director = await builder.build_director_context(novel.id, chapter_number=5)
    assert any(
        f.content == SECRET_STATEMENT for f in director.planning_secrets
    ), "Director 规划需要看到秘密"

    writer = await builder.build_writer_context(
        novel.id, chapter_number=5, pov_character_id=world["characters"]["林凡"].id
    )
    _assert_secret_absent(writer.model_dump_json())
    # thread truth structurally absent from writer-safe threads
    assert "truth" not in __import__(
        "app.context.models", fromlist=["WriterSafePlotThread"]
    ).WriterSafePlotThread.model_fields
    # thread public summaries scrubbed / safe
    for thread in writer.safe_plot_threads:
        assert SECRET_STATEMENT not in thread.current_visible_state
