"""Chapter + summary repositories (read-side for Context Builder)."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.models import Chapter, ChapterSummary
from app.repositories.base import BaseRepository


class ChapterRepository(BaseRepository[Chapter]):
    model = Chapter

    async def latest_for_number(
        self, novel_id: uuid.UUID, number: int, *, final_only: bool = True
    ) -> Chapter | None:
        """Highest-version chapter for a number (regeneration keeps history)."""
        stmt = (
            select(Chapter)
            .where(Chapter.novel_id == novel_id, Chapter.number == number)
            .order_by(Chapter.version.desc())
        )
        if final_only:
            stmt = stmt.where(Chapter.status.in_(["final", "canonical"]))
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def recent_final_bodies(
        self, novel_id: uuid.UUID, before_number: int, limit: int
    ) -> list[Chapter]:
        result = await self.session.execute(
            select(Chapter)
            .where(
                Chapter.novel_id == novel_id,
                Chapter.number < before_number,
                Chapter.status.in_(["final", "canonical"]),
            )
            .order_by(Chapter.number.desc())
            .limit(limit)
        )
        return list(result.scalars().all())


class ChapterSummaryRepository(BaseRepository[ChapterSummary]):
    model = ChapterSummary

    async def recent_summaries(
        self, novel_id: uuid.UUID, before_number: int, limit: int
    ) -> list[ChapterSummary]:
        """Most recent N summaries in ascending chapter order."""
        result = await self.session.execute(
            select(ChapterSummary)
            .where(
                ChapterSummary.novel_id == novel_id,
                ChapterSummary.chapter_number < before_number,
            )
            .order_by(ChapterSummary.chapter_number.desc())
            .limit(limit)
        )
        return list(reversed(result.scalars().all()))
