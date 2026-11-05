"""Hybrid retrieval: combine retrievers, dedupe, score, top-K."""

from __future__ import annotations

from app.retrieval.base import Retriever
from app.retrieval.models import RetrievalQuery, RetrievedMemory, SourceType
from app.retrieval.scoring import apply_hybrid_scores


class HybridRetriever:
    """Facade implementing the Retriever protocol.

    Combines a structured retriever (deterministic, entity-first) with
    optional vector retrievers; deduplicates by (source_type, source_id),
    applies hybrid scoring, and returns the top-K deterministically.
    """

    name = "hybrid"

    def __init__(self, structured: Retriever, *vector_retrievers: Retriever) -> None:
        self.structured = structured
        self.vector_retrievers = list(vector_retrievers)

    async def retrieve(self, query: RetrievalQuery) -> list[RetrievedMemory]:
        memories: list[RetrievedMemory] = []
        seen: set[tuple[str, str]] = set()
        for retriever in (self.structured, *self.vector_retrievers):
            for memory in await retriever.retrieve(query):
                key = (memory.source_type.value, memory.source_id)
                if key in seen:
                    continue
                seen.add(key)
                memories.append(memory)

        if query.source_types is not None:
            allowed = {st for st in query.source_types}
            memories = [m for m in memories if m.source_type in allowed]

        scored = apply_hybrid_scores(memories, query)
        return scored[: query.top_k]

    @staticmethod
    def filter_by_type(
        memories: list[RetrievedMemory], *types: SourceType
    ) -> list[RetrievedMemory]:
        return [m for m in memories if m.source_type in set(types)]
