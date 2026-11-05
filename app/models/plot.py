"""Long-range narrative structure: Arc, PlotThread, Foreshadowing,
PlotDebt, Constraint."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, str_enum
from app.models.enums import (
    ArcStatus,
    ConstraintScope,
    ConstraintSource,
    ConstraintType,
    DebtStatus,
    ForeshadowStatus,
    ForeshadowVisibility,
    ThreadStatus,
)

if TYPE_CHECKING:
    from app.models.novel import Novel


class Arc(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "arcs"

    novel_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("novels.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    start_chapter: Mapped[int] = mapped_column(Integer, nullable=False)
    planned_end_chapter: Mapped[int | None] = mapped_column(Integer)
    goal: Mapped[str | None] = mapped_column(Text)
    antagonist_character_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("characters.id", ondelete="SET NULL")
    )
    major_beats: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    character_growth_plan: Mapped[str | None] = mapped_column(Text)
    mysteries: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    ending_hook: Mapped[str | None] = mapped_column(Text)
    status: Mapped[ArcStatus] = mapped_column(
        str_enum(ArcStatus, 20), default=ArcStatus.planned, nullable=False
    )

    novel: Mapped["Novel"] = relationship(back_populates="arcs", foreign_keys=[novel_id])


class PlotThread(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "plot_threads"

    novel_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("novels.id", ondelete="CASCADE"), nullable=False
    )
    arc_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("arcs.id", ondelete="SET NULL")
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    thread_type: Mapped[str | None] = mapped_column(String(50))
    status: Mapped[ThreadStatus] = mapped_column(
        str_enum(ThreadStatus, 20), default=ThreadStatus.planned, nullable=False, index=True
    )
    truth: Mapped[str | None] = mapped_column(Text)
    public_summary: Mapped[str | None] = mapped_column(Text)
    introduced_chapter: Mapped[int | None] = mapped_column(Integer)
    target_resolution_chapter: Mapped[int | None] = mapped_column(Integer)
    priority: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    related_entity_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    last_advanced_chapter: Mapped[int | None] = mapped_column(Integer)

    novel: Mapped["Novel"] = relationship(back_populates="plot_threads")
    foreshadowings: Mapped[list["Foreshadowing"]] = relationship(
        back_populates="plot_thread"
    )


class Foreshadowing(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "foreshadowings"

    novel_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("novels.id", ondelete="CASCADE"), nullable=False
    )
    plot_thread_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("plot_threads.id", ondelete="SET NULL")
    )
    setup: Mapped[str] = mapped_column(Text, nullable=False)
    intended_payoff: Mapped[str | None] = mapped_column(Text)
    status: Mapped[ForeshadowStatus] = mapped_column(
        str_enum(ForeshadowStatus, 20), default=ForeshadowStatus.planned, nullable=False, index=True
    )
    planted_chapter: Mapped[int | None] = mapped_column(Integer)
    payoff_chapter: Mapped[int | None] = mapped_column(Integer)
    visibility: Mapped[ForeshadowVisibility] = mapped_column(
        str_enum(ForeshadowVisibility, 20), default=ForeshadowVisibility.hidden, nullable=False
    )
    # how many chapters since last advancement; used to wake dormant setups
    chapters_since_last_touch: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    plot_thread: Mapped["PlotThread | None"] = relationship(back_populates="foreshadowings")


class PlotDebt(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A promise made by the narrative that is not yet fulfilled."""

    __tablename__ = "plot_debts"

    novel_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("novels.id", ondelete="CASCADE"), nullable=False
    )
    debt_type: Mapped[str | None] = mapped_column(String(50))
    description: Mapped[str] = mapped_column(Text, nullable=False)
    created_chapter: Mapped[int] = mapped_column(Integer, nullable=False)
    due_story_time: Mapped[str | None] = mapped_column(String(100))
    due_chapter: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[DebtStatus] = mapped_column(
        str_enum(DebtStatus, 20), default=DebtStatus.open, nullable=False, index=True
    )
    related_entities: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    related_plot_thread_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("plot_threads.id", ondelete="SET NULL")
    )
    resolved_chapter: Mapped[int | None] = mapped_column(Integer)

    novel: Mapped["Novel"] = relationship(back_populates="plot_debts")


class Constraint(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "constraints"

    novel_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("novels.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[ConstraintType] = mapped_column(str_enum(ConstraintType, 10), nullable=False)
    scope: Mapped[ConstraintScope] = mapped_column(
        str_enum(ConstraintScope, 20), default=ConstraintScope.novel, nullable=False
    )
    scope_ref_id: Mapped[str | None] = mapped_column(String(64))
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_by: Mapped[ConstraintSource] = mapped_column(
        str_enum(ConstraintSource, 10), default=ConstraintSource.user, nullable=False
    )
    start_chapter: Mapped[int | None] = mapped_column(Integer)
    end_chapter: Mapped[int | None] = mapped_column(Integer)

    novel: Mapped["Novel"] = relationship(back_populates="constraints")
