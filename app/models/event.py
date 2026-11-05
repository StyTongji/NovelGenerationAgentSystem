"""Event log (event sourcing) + structured EventEffect rows."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Float, ForeignKey, Integer, JSON, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, str_enum
from app.models.enums import EventTargetType

if TYPE_CHECKING:
    from app.models.chapter import Chapter
    from app.models.location import Location
    from app.models.novel import Novel


class Event(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Immutable historical record: "what happened". Append-only."""

    __tablename__ = "events"

    novel_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("novels.id", ondelete="CASCADE"), nullable=False
    )
    chapter_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("chapters.id", ondelete="SET NULL")
    )
    chapter_number: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    scene_index: Mapped[int | None] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    title: Mapped[str | None] = mapped_column(String(200))
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    participant_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    location_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("locations.id", ondelete="SET NULL")
    )
    importance: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    happened_at_story_time: Mapped[str | None] = mapped_column(String(100))
    evidence_span: Mapped[str | None] = mapped_column(Text)
    extra_metadata: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    novel: Mapped["Novel"] = relationship(back_populates="events")
    chapter: Mapped["Chapter | None"] = relationship()
    location: Mapped["Location | None"] = relationship()
    effects: Mapped[list["EventEffect"]] = relationship(
        back_populates="event", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Event ch{self.chapter_number} {self.event_type}>"


class EventEffect(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """How one event changed a target entity field."""

    __tablename__ = "event_effects"

    event_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    target_type: Mapped[EventTargetType] = mapped_column(str_enum(EventTargetType, 30), nullable=False)
    target_id: Mapped[str] = mapped_column(String(64), nullable=False)
    field: Mapped[str] = mapped_column(String(100), nullable=False)
    operation: Mapped[str] = mapped_column(String(20), default="set", nullable=False)
    old_value: Mapped[dict | list | str | int | float | bool | None] = mapped_column(
        JSON, nullable=True
    )
    new_value: Mapped[dict | list | str | int | float | bool | None] = mapped_column(
        JSON, nullable=True
    )

    event: Mapped["Event"] = relationship(back_populates="effects")
