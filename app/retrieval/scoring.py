"""Deterministic scoring for hybrid retrieval.

semantic  : character-bigram containment of query in content (no embeddings
            yet; swapped for a vector backend later without interface change)
recency   : 1 / (1 + distance / scale) relative to the CURRENT chapter —
            never 1/chapter_number (documented, testable decay)
importance: stored on Event (0~1); other sources supply explicit values

final = semantic_weight * semantic + recency_weight * recency + importance_weight * importance
"""

from __future__ import annotations

import re

from app.retrieval.models import RetrievalQuery, RetrievedMemory

_STRIP = re.compile(r"[\s，。、！？：；「」『’“”（）()\[\]{}\"',.!?;:<>\\-]+")


def _bigrams(text: str) -> set[str]:
    cleaned = _STRIP.sub("", text)
    return {cleaned[i : i + 2] for i in range(len(cleaned) - 1)}


def semantic_score(content: str, query_text: str | None) -> float:
    """Containment of the query's bigrams in the content's bigrams (0~1)."""
    if not query_text:
        return 0.0
    query_bigrams = _bigrams(query_text)
    content_bigrams = _bigrams(content)
    if not query_bigrams or not content_bigrams:
        return 0.0
    return len(query_bigrams & content_bigrams) / len(query_bigrams)


def recency_score(event_chapter: int | None, current_chapter: int, scale: float) -> float:
    """Deterministic decay relative to the current chapter.

    distance 0 -> 1.0; distance == scale -> 0.5; larger distances decay
    smoothly and never hit exactly zero.
    """
    if event_chapter is None:
        return 0.0
    distance = max(0, current_chapter - event_chapter)
    return 1.0 / (1.0 + distance / scale)


def apply_hybrid_scores(
    memories: list[RetrievedMemory], query: RetrievalQuery
) -> list[RetrievedMemory]:
    """Fill in scores and return memories sorted deterministically."""
    scored: list[RetrievedMemory] = []
    for memory in memories:
        memory.semantic_score = semantic_score(memory.content, query.query_text)
        memory.recency_score = recency_score(
            memory.chapter_number, query.chapter_number, query.recency_scale
        )
        memory.final_score = (
            query.semantic_weight * memory.semantic_score
            + query.recency_weight * memory.recency_score
            + query.importance_weight * memory.importance_score
        )
        scored.append(memory)
    scored.sort(key=lambda m: m.sort_key())
    return scored
