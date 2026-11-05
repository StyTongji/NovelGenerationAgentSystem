"""StoryFact: any proposition that can be known / hidden / revealed.

Knowledge isolation is built on this table:
``visible_facts_for(pov) = public_facts | facts_with_knowledge_row(pov)``.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, str_enum
from app.models.enums import SecrecyLevel, TruthStatus

if TYPE_CHECKING:
    from app.models.event import Event
    from app.models.novel import Novel


class StoryFact(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "story_facts"

    novel_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("novels.id", ondelete="CASCADE"), nullable=False
    )
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    truth_status: Mapped[TruthStatus] = mapped_column(
        str_enum(TruthStatus, 20), default=TruthStatus.true, nullable=False
    )
    secrecy: Mapped[SecrecyLevel] = mapped_column(
        str_enum(SecrecyLevel, 20), default=SecrecyLevel.public, nullable=False
    )
    category: Mapped[str | None] = mapped_column(String(50))
    introduced_chapter: Mapped[int | None] = mapped_column(Integer)
    revealed_to_reader_chapter: Mapped[int | None] = mapped_column(Integer)
    source_event_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("events.id", ondelete="SET NULL")
    )

    novel: Mapped["Novel"] = relationship(back_populates="facts")
    source_event: Mapped["Event | None"] = relationship()

    def __repr__(self) -> str:  # pragma: no cover
        return f"<StoryFact {self.statement[:24]!r} secrecy={self.secrecy}>"
