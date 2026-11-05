"""Power system: PowerSystem, Realm, Ability (technique)."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import JSON, ForeignKey, Integer, String, Text, Uuid, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.character import Character
    from app.models.novel import Novel


class PowerSystem(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "power_systems"

    novel_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("novels.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    rules: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)

    novel: Mapped["Novel"] = relationship()
    realms: Mapped[list["Realm"]] = relationship(
        back_populates="power_system", cascade="all, delete-orphan", order_by="Realm.rank"
    )


class Realm(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "realms"
    __table_args__ = (UniqueConstraint("power_system_id", "name", name="uq_realm_ps_name"),)

    power_system_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("power_systems.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    sublevels: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    requirements: Mapped[str | None] = mapped_column(Text)
    capabilities: Mapped[str | None] = mapped_column(Text)

    power_system: Mapped["PowerSystem"] = relationship(back_populates="realms")


class Ability(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A technique / skill. Ownership is tracked via ``owner_character_id``."""

    __tablename__ = "abilities"

    novel_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("novels.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    type: Mapped[str | None] = mapped_column(String(50))
    grade: Mapped[str | None] = mapped_column(String(50))
    requirements: Mapped[str | None] = mapped_column(Text)
    effects: Mapped[str | None] = mapped_column(Text)
    limitations: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)

    owner_character_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("characters.id", ondelete="SET NULL")
    )
    learned_chapter: Mapped[int | None] = mapped_column(Integer)

    owner: Mapped["Character | None"] = relationship()
