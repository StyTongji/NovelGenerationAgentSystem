"""Retriever protocol + vector retriever abstraction.

MVP ships deterministic structured retrieval only; vector backends plug in
behind the same protocol later (pgvector / FAISS / Qdrant) without touching
the ContextBuilder.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.retrieval.models import RetrievalQuery, RetrievedMemory


@runtime_checkable
class Retriever(Protocol):
    async def retrieve(self, query: RetrievalQuery) -> list[RetrievedMemory]:
        ...


class NoOpVectorRetriever:
    """Placeholder semantic retriever: returns nothing.

    Replaced by a real embedding backend later; the ContextBuilder must
    keep working identically when this is swapped in or out.
    """

    name = "noop_vector"

    async def retrieve(self, query: RetrievalQuery) -> list[RetrievedMemory]:
        return []
