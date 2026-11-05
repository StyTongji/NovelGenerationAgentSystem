"""Retrieval layer models: query, retrieved memory, source types."""

from __future__ import annotations

import uuid
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class SourceType(str, Enum):
    event = "event"
    character = "character"
    location = "location"
    item = "item"
    faction = "faction"
    plot_thread = "plot_thread"
    foreshadowing = "foreshadowing"
    plot_debt = "plot_debt"
    story_fact = "story_fact"
    chapter_summary = "chapter_summary"
    relationship = "relationship"


class RetrievalQuery(BaseModel):
    """Strongly-typed retrieval request. Retrieval parameters never live
    ad-hoc inside the ContextBuilder."""

    novel_id: uuid.UUID
    query_text: str | None = None
    chapter_number: int
    character_ids: list[uuid.UUID] = Field(default_factory=list)
    location_ids: list[uuid.UUID] = Field(default_factory=list)
    item_ids: list[uuid.UUID] = Field(default_factory=list)
    plot_thread_ids: list[uuid.UUID] = Field(default_factory=list)
    source_types: list[SourceType] | None = None  # None = all
    top_k: int = 12
    # hybrid scoring weights (defaults follow TASKS Phase 3)
    semantic_weight: float = 0.50
    recency_weight: float = 0.25
    importance_weight: float = 0.25
    # recency decay scale in chapters: score = 1 / (1 + distance / scale)
    recency_scale: float = 10.0
    max_chapter_distance: int | None = None  # hard chapter-window cap


class RetrievedMemory(BaseModel):
    source_type: SourceType
    source_id: str
    content: str
    chapter_number: int | None = None
    importance_score: float = 0.0
    semantic_score: float = 0.0
    recency_score: float = 0.0
    final_score: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)

    def sort_key(self) -> tuple:
        """Deterministic tie-breaking: score desc, then type, then id."""
        return (-self.final_score, self.source_type.value, self.source_id)
