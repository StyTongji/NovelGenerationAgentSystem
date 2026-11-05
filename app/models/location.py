"""World geography and organizations: Location, Faction."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import JSON, ForeignKey, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, str_enum
from app.models.enums import FactionStatus

if TYPE_CHECKING:
    from app.models.character import Character
    from app.models.novel import Novel


class Location(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "locations"

    novel_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("novels.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    parent_location_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("locations.id", ondelete="SET NULL")
    )
    description: Mapped[str | None] = mapped_column(Text)
    faction_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("factions.id", ondelete="SET NULL")
    )
    travel_metadata: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    secret_fact_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)

    novel: Mapped["Novel"] = relationship()
    parent: Mapped["Location | None"] = relationship(remote_side="Location.id")
    faction: Mapped["Faction | None"] = relationship(back_populates="locations")


class Faction(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "factions"

    novel_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("novels.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    type: Mapped[str | None] = mapped_column(String(50))
    leader_character_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("characters.id", ondelete="SET NULL")
    )
    description: Mapped[str | None] = mapped_column(Text)
    goals: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    status: Mapped[FactionStatus] = mapped_column(
        str_enum(FactionStatus, 20), default=FactionStatus.active, nullable=False
    )

    leader: Mapped["Character | None"] = relationship()
    locations: Mapped[list["Location"]] = relationship(back_populates="faction")
