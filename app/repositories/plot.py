"""Event log + plot structure repositories."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import String, select
from sqlalchemy.orm import selectinload

from app.models import (
    Arc,
    Constraint,
    Event,
    EventEffect,
    Foreshadowing,
    PlotDebt,
    PlotThread,
)
from app.repositories.base import BaseRepository


class EventRepository(BaseRepository[Event]):
    model = Event

    async def search(
        self,
        novel_id: uuid.UUID,
        *,
        participant_id: uuid.UUID | None = None,
        location_id: uuid.UUID | None = None,
        event_type: str | None = None,
        chapter_max: int | None = None,
        chapter_min: int | None = None,
        min_importance: float | None = None,
        limit: int = 200,
    ) -> list[Event]:
        """SQL-level narrowing. Participant matching uses a text cast of the
        JSON column (portable across SQLite and PostgreSQL); everything else
        is a plain indexed predicate."""
        stmt = select(Event).where(Event.novel_id == novel_id)
        if participant_id is not None:
            # JSON list serialized as text; match the quoted UUID
            stmt = stmt.where(
                Event.participant_ids.cast(String).like(f'%"{participant_id}"%')
            )
        if location_id is not None:
            stmt = stmt.where(Event.location_id == location_id)
        if event_type is not None:
            stmt = stmt.where(Event.event_type == event_type)
        if chapter_max is not None:
            stmt = stmt.where(Event.chapter_number <= chapter_max)
        if chapter_min is not None:
            stmt = stmt.where(Event.chapter_number >= chapter_min)
        if min_importance is not None:
            stmt = stmt.where(Event.importance >= min_importance)
        stmt = stmt.order_by(Event.chapter_number.desc(), Event.id.asc()).limit(limit)
        result = await self.session.execute(stmt.options(selectinload(Event.effects)))
        return list(result.scalars().unique().all())

    async def list_for_novel(
        self, novel_id: uuid.UUID, limit: int | None = None
    ) -> list[Event]:
        stmt = (
            select(Event)
            .where(Event.novel_id == novel_id)
            .options(selectinload(Event.effects))
            .order_by(Event.chapter_number.asc(), Event.created_at.asc())
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def recent_events(self, novel_id: uuid.UUID, last_n_chapters: int) -> list[Event]:
        result = await self.session.execute(
            select(Event)
            .where(Event.novel_id == novel_id)
            .order_by(Event.chapter_number.desc())
            .limit(last_n_chapters * 10)
        )
        return list(result.scalars().all())

    async def add_effect(self, effect: EventEffect) -> EventEffect:
        self.session.add(effect)
        await self.session.flush()
        return effect


class PlotRepository(BaseRepository[PlotThread]):
    model = PlotThread

    async def list_threads(self, novel_id: uuid.UUID) -> list[PlotThread]:
        result = await self.session.execute(
            select(PlotThread).where(PlotThread.novel_id == novel_id)
        )
        return list(result.scalars().all())

    async def find_thread_by_title(self, novel_id: uuid.UUID, title: str) -> PlotThread | None:
        result = await self.session.execute(
            select(PlotThread).where(
                PlotThread.novel_id == novel_id, PlotThread.title == title
            )
        )
        return result.scalar_one_or_none()


class ArcRepository(BaseRepository[Arc]):
    model = Arc

    async def list_for_novel(self, novel_id: uuid.UUID) -> list[Arc]:
        result = await self.session.execute(
            select(Arc).where(Arc.novel_id == novel_id).order_by(Arc.start_chapter.asc())
        )
        return list(result.scalars().all())


class DebtRepository(BaseRepository[PlotDebt]):
    model = PlotDebt

    async def list_open(self, novel_id: uuid.UUID) -> list[PlotDebt]:
        result = await self.session.execute(
            select(PlotDebt).where(PlotDebt.novel_id == novel_id, PlotDebt.status == "open")
        )
        return list(result.scalars().all())


class ForeshadowRepository(BaseRepository[Foreshadowing]):
    model = Foreshadowing

    async def list_unresolved(self, novel_id: uuid.UUID) -> list[Foreshadowing]:
        result = await self.session.execute(
            select(Foreshadowing).where(
                Foreshadowing.novel_id == novel_id,
                Foreshadowing.status.in_(["planned", "planted", "reinforced"]),
            )
        )
        return list(result.scalars().all())


class ConstraintRepository(BaseRepository[Constraint]):
    model = Constraint

    async def list_active(self, novel_id: uuid.UUID, chapter_number: int | None = None) -> list[Constraint]:
        stmt = select(Constraint).where(
            Constraint.novel_id == novel_id, Constraint.active.is_(True)
        )
        if chapter_number is not None:
            from sqlalchemy import or_

            stmt = stmt.where(
                or_(
                    Constraint.start_chapter.is_(None),
                    Constraint.start_chapter <= chapter_number,
                ),
                or_(
                    Constraint.end_chapter.is_(None),
                    Constraint.end_chapter >= chapter_number,
                ),
            )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())
