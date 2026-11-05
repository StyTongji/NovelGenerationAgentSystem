"""Retrieval layer: entity-first structured retrieval + scoring.

Boundary: retrieval talks to repositories, never to agents; agents talk to
the ContextBuilder, never to retrieval or repositories directly.
"""

from app.retrieval.base import NoOpVectorRetriever, Retriever
from app.retrieval.entity_linker import EntityLinker, LinkedEntities
from app.retrieval.event_retriever import StructuredEventRetriever
from app.retrieval.hybrid import HybridRetriever
from app.retrieval.models import (
    RetrievalQuery,
    RetrievedMemory,
    SourceType,
)
from app.retrieval.scoring import apply_hybrid_scores, recency_score, semantic_score

__all__ = [
    "Retriever",
    "NoOpVectorRetriever",
    "EntityLinker",
    "LinkedEntities",
    "StructuredEventRetriever",
    "HybridRetriever",
    "RetrievalQuery",
    "RetrievedMemory",
    "SourceType",
    "apply_hybrid_scores",
    "recency_score",
    "semantic_score",
]
