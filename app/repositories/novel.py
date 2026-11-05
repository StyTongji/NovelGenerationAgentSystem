"""Novel, StoryFact and StorySnapshot repositories."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.models import Novel, StoryFact, StorySnapshot
from app.repositories.base import BaseRepository


class NovelRepository(BaseRepository[Novel]):
    model = Novel

    async def get(self, entity_id: uuid.UUID) -> Novel | None:
        return await self.session.get(Novel, entity_id)


class FactRepository(BaseRepository[StoryFact]):
    model = StoryFact

    async def list_for_novel(self, novel_id: uuid.UUID) -> list[StoryFact]:
        result = await self.session.execute(
            select(StoryFact).where(StoryFact.novel_id == novel_id)
        )
        return list(result.scalars().all())

    async def find_by_statement(self, novel_id: uuid.UUID, statement: str) -> StoryFact | None:
        result = await self.session.execute(
            select(StoryFact).where(
                StoryFact.novel_id == novel_id, StoryFact.statement == statement
            )
        )
        return result.scalar_one_or_none()


class SnapshotRepository(BaseRepository[StorySnapshot]):
    model = StorySnapshot

    async def latest_for_novel(self, novel_id: uuid.UUID) -> StorySnapshot | None:
        result = await self.session.execute(
            select(StorySnapshot)
            .where(StorySnapshot.novel_id == novel_id)
            .order_by(StorySnapshot.after_chapter_number.desc(), StorySnapshot.state_version.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()
