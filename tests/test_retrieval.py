"""Retrieval layer tests: scoring, entity linking, event retrieval, hybrid."""

from __future__ import annotations

import uuid

from app.retrieval import (
    EntityLinker,
    HybridRetriever,
    NoOpVectorRetriever,
    RetrievalQuery,
    SourceType,
    StructuredEventRetriever,
    recency_score,
    semantic_score,
)
from tests.helpers import create_rich_world, create_novel


# ------------------------------------------------------------------ scoring


def test_recency_decay_relative_to_current_chapter():
    assert recency_score(83, 83, 10.0) == 1.0
    assert abs(recency_score(73, 83, 10.0) - 0.5) < 1e-9
    assert recency_score(3, 83, 10.0) < 0.2
    # deterministic: same inputs, same output
    assert recency_score(50, 83, 10.0) == recency_score(50, 83, 10.0)


def test_semantic_score_bigram_containment():
    assert semantic_score("轮回古镜发光了", "轮回古镜") > 0.5
    assert semantic_score("林凡吃饭", "拍卖会风云") == 0.0
    assert semantic_score("anything", None) == 0.0


# ------------------------------------------------------------ entity linker


async def test_entity_linker_matches_names_and_aliases(session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    linker = EntityLinker(session)

    linked = await linker.link(novel.id, "下一章让林凡去调查轮回古镜")
    assert world["characters"]["林凡"].id in linked.character_ids
    assert world["mirror"].id in linked.item_ids
    assert "林凡" in linked.matched_names
    assert "古镜" in linked.matched_names  # alias matched

    empty = await linker.link(novel.id, None)
    assert not empty.merged_ids()


# -------------------------------------------------------- event retriever


async def test_event_retrieval_by_participant_location_and_recency(session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    retriever = StructuredEventRetriever(session)
    lin_fan = world["characters"]["林凡"]
    su_qing = world["characters"]["苏青"]
    xuan_qing = world["characters"]["玄清真人"]

    query = RetrievalQuery(novel_id=novel.id, chapter_number=4, character_ids=[lin_fan.id], top_k=10)
    results = await retriever.retrieve(query)
    ids = {m.source_id for m in results}
    assert str(world["events"]["auction"].id) in ids
    assert str(world["events"]["rescue"].id) in ids
    # secret meeting does not involve 林凡
    assert str(world["events"]["secret"].id) not in ids

    by_su = await retriever.retrieve(
        RetrievalQuery(novel_id=novel.id, chapter_number=4, character_ids=[su_qing.id], top_k=10)
    )
    assert str(world["events"]["rescue"].id) in {m.source_id for m in by_su}

    by_xuan = await retriever.retrieve(
        RetrievalQuery(novel_id=novel.id, chapter_number=4, character_ids=[xuan_qing.id], top_k=10)
    )
    assert str(world["events"]["secret"].id) in {m.source_id for m in by_xuan}


async def test_event_retrieval_importance_ranking(session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    retriever = StructuredEventRetriever(session)
    lin_fan = world["characters"]["林凡"]

    memories = await retriever.retrieve(
        RetrievalQuery(novel_id=novel.id, chapter_number=4, character_ids=[lin_fan.id], top_k=10)
    )
    by_id = {m.source_id: m for m in memories}
    rescue = by_id[str(world["events"]["rescue"].id)]
    auction = by_id[str(world["events"]["auction"].id)]
    assert rescue.importance_score == 0.9 > auction.importance_score == 0.6


# ------------------------------------------------------------ hybrid scoring


async def test_hybrid_topk_and_deterministic_order(session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    retriever = HybridRetriever(StructuredEventRetriever(session), NoOpVectorRetriever())

    query = RetrievalQuery(
        novel_id=novel.id,
        query_text="苏青在黑风谷挡剑",
        chapter_number=4,
        character_ids=[world["characters"]["林凡"].id],
        top_k=2,
    )
    top2 = await retriever.retrieve(query)
    assert len(top2) == 2
    # scores strictly descending (tie broken by ids)
    scores = [m.final_score for m in top2]
    assert scores == sorted(scores, reverse=True)
    first_run = [m.source_id for m in top2]

    second_run = [m.source_id for m in await retriever.retrieve(query)]
    assert first_run == second_run, "相同状态与查询必须输出稳定顺序"


async def test_hybrid_source_type_filter(session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    retriever = HybridRetriever(StructuredEventRetriever(session))
    results = await retriever.retrieve(
        RetrievalQuery(
            novel_id=novel.id,
            chapter_number=4,
            character_ids=[world["characters"]["林凡"].id],
            source_types=[SourceType.event],
            top_k=10,
        )
    )
    assert all(m.source_type == SourceType.event for m in results)


async def test_noop_vector_retriever_returns_empty(session):
    novel = await create_novel(session)
    results = await NoOpVectorRetriever().retrieve(
        RetrievalQuery(novel_id=novel.id, chapter_number=1)
    )
    assert results == []
