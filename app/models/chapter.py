"""Chapter (immutable versions), ChapterSummary, StorySnapshot."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Integer, JSON, String, Text, Uuid, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, str_enum
from app.models.enums import ChapterStatus

if TYPE_CHECKING:
    from app.models.novel import Novel


class Chapter(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Chapter body. Never overwritten: regeneration creates a new version
    and marks the old one ``superseded``."""

    __tablename__ = "chapters"
    __table_args__ = (
        UniqueConstraint("novel_id", "number", "version", name="uq_chapter_novel_num_ver"),
    )

    novel_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("novels.id", ondelete="CASCADE"), nullable=False
    )
    number: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    title: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[ChapterStatus] = mapped_column(
        str_enum(ChapterStatus, 20), default=ChapterStatus.draft, nullable=False
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    user_direction: Mapped[str | None] = mapped_column(Text)
    chapter_plan_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    continuity_report_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # provenance for reproducibility
    prompt_version: Mapped[str | None] = mapped_column(String(50))
    model_config_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Phase 8: commit provenance
    draft_hash: Mapped[str | None] = mapped_column(String(64))
    story_state_before_hash: Mapped[str | None] = mapped_column(String(64))
    story_state_after_hash: Mapped[str | None] = mapped_column(String(64))

    novel: Mapped["Novel"] = relationship(back_populates="chapters")
    summary: Mapped["ChapterSummary | None"] = relationship(
        back_populates="chapter", cascade="all, delete-orphan", uselist=False
    )


class ChapterSummary(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "chapter_summaries"

    chapter_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("chapters.id", ondelete="CASCADE"), nullable=False
    )
    novel_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("novels.id", ondelete="CASCADE"), nullable=False
    )
    chapter_number: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    short_summary: Mapped[str] = mapped_column(Text, nullable=False)
    detailed_summary: Mapped[str | None] = mapped_column(Text)
    # Phase 8: visibility-separated summaries
    author_summary: Mapped[str | None] = mapped_column(Text)
    reader_summary: Mapped[str | None] = mapped_column(Text)
    # Phase 8.5 Fix C: provenance binding to the FINAL repaired draft
    source_draft_hash: Mapped[str | None] = mapped_column(String(64))
    key_event_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    character_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    plot_thread_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)

    chapter: Mapped["Chapter"] = relationship(back_populates="summary")


class StorySnapshot(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Serialized canonical state after chapter N. Derived but cheap to keep."""

    __tablename__ = "story_snapshots"

    novel_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("novels.id", ondelete="CASCADE"), nullable=False
    )
    after_chapter_number: Mapped[int] = mapped_column(Integer, nullable=False)
    state_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    serialized_state: Mapped[dict] = mapped_column(JSON, nullable=False)
    state_hash: Mapped[str | None] = mapped_column(String(64))

    novel: Mapped["Novel"] = relationship(back_populates="snapshots")
