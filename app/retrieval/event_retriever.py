"""Structured event retrieval over the event log (episodic memory)."""

from __future__ import annotations

import uuid

from app.models import Event
from app.retrieval.models import RetrievalQuery, RetrievedMemory, SourceType
from app.repositories.plot import EventRepository


class StructuredEventRetriever:
    """Deterministic event retrieval. SQL narrows the candidate set; scoring
    and top-K happen afterwards on candidates only."""

    name = "structured_event"

    def __init__(self, session) -> None:
        self.events = EventRepository(session)

    async def retrieve(self, query: RetrievalQuery) -> list[RetrievedMemory]:
        memories: list[RetrievedMemory] = []
        candidate_ids: set[str] = set()

        async def collect(bound_events: list[Event]) -> None:
            for event in bound_events:
                if str(event.id) in candidate_ids:
                    continue
                candidate_ids.add(str(event.id))
                memories.append(self._to_memory(event))

        # entity-driven candidates: one SQL query per linked participant
        for character_id in query.character_ids:
            found = await self.events.search(
                query.novel_id,
                participant_id=character_id,
                chapter_max=query.chapter_number,
                limit=query.top_k * 4,
            )
            await collect(found)

        for location_id in query.location_ids:
            found = await self.events.search(
                query.novel_id,
                location_id=location_id,
                chapter_max=query.chapter_number,
                limit=query.top_k * 4,
            )
            await collect(found)

        if query.max_chapter_distance is not None:
            floor = query.chapter_number - query.max_chapter_distance
            memories = [
                m for m in memories
                if m.chapter_number is None or m.chapter_number >= floor
            ]
        return memories

    @staticmethod
    def _to_memory(event: Event) -> RetrievedMemory:
        return RetrievedMemory(
            source_type=SourceType.event,
            source_id=str(event.id),
            content=event.summary,
            chapter_number=event.chapter_number,
            importance_score=event.importance,
            metadata={
                "event_type": event.event_type,
                "participant_ids": event.participant_ids,
                "location_id": str(event.location_id) if event.location_id else None,
                "happened_at_story_time": event.happened_at_story_time,
                # secret tagging by future MemoryExtractor; the firewall uses it
                "secret_fact_ids": (event.extra_metadata or {}).get("secret_fact_ids", []),
            },
        )
