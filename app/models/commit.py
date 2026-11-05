"""ChapterCommit: idempotency + audit record for canonical commits."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ChapterCommit(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "chapter_commits"
    __table_args__ = (
        # idempotency: one commit per novel per draft
        __import__("sqlalchemy").UniqueConstraint(
            "novel_id", "draft_hash", name="uq_commit_novel_draft"
        ),
    )

    novel_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("novels.id", ondelete="CASCADE"), nullable=False
    )
    chapter_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("chapters.id", ondelete="CASCADE"), nullable=False
    )
    chapter_number: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    draft_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    before_state_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    after_state_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_fingerprints_json: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    # audit trail: per-operation provenance (type/target/scene/evidence)
    operations_json: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    temporary_entity_mapping_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    generation_metadata_json: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    # Phase 8.5 Fix D: final continuity report at commit time (audit chain)
    continuity_report_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("story_snapshots.id", ondelete="SET NULL")
    )
