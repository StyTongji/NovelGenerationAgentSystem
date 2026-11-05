"""Novel aggregate root."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, str_enum
from app.models.enums import NovelStatus

if TYPE_CHECKING:
    from app.models.chapter import Chapter
    from app.models.event import Event
    from app.models.fact import StoryFact
    from app.models.plot import Arc, Constraint, PlotDebt, PlotThread
    from app.models.snapshot import StorySnapshot


class Novel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "novels"

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    genre: Mapped[str | None] = mapped_column(String(50))
    premise: Mapped[str | None] = mapped_column(Text)
    style_profile: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[NovelStatus] = mapped_column(
        str_enum(NovelStatus, 20), default=NovelStatus.drafting, nullable=False
    )
    current_chapter_number: Mapped[int] = mapped_column(default=0, nullable=False)

    current_arc_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("arcs.id", use_alter=True, ondelete="SET NULL")
    )

    # --- relationships ---
    arcs: Mapped[list["Arc"]] = relationship(
        back_populates="novel", foreign_keys="Arc.novel_id"
    )
    characters: Mapped[list["Character"]] = relationship(back_populates="novel")
    chapters: Mapped[list["Chapter"]] = relationship(back_populates="novel")
    events: Mapped[list["Event"]] = relationship(back_populates="novel")
    facts: Mapped[list["StoryFact"]] = relationship(back_populates="novel")
    plot_threads: Mapped[list["PlotThread"]] = relationship(back_populates="novel")
    plot_debts: Mapped[list["PlotDebt"]] = relationship(back_populates="novel")
    constraints: Mapped[list["Constraint"]] = relationship(back_populates="novel")
    snapshots: Mapped[list["StorySnapshot"]] = relationship(back_populates="novel")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Novel {self.title!r}>"


# imported late to avoid circular import at module load
from app.models.character import Character  # noqa: E402,F401
