"""Items / artifacts / treasures with ownership and status tracking."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import JSON, ForeignKey, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, str_enum
from app.models.enums import ItemStatus

if TYPE_CHECKING:
    from app.models.character import Character
    from app.models.location import Location
    from app.models.novel import Novel


class Item(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "items"

    novel_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("novels.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    aliases: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    type: Mapped[str | None] = mapped_column(String(50))
    grade: Mapped[str | None] = mapped_column(String(50))
    description: Mapped[str | None] = mapped_column(Text)

    current_owner_character_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("characters.id", ondelete="SET NULL")
    )
    current_location_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("locations.id", ondelete="SET NULL")
    )
    status: Mapped[ItemStatus] = mapped_column(
        str_enum(ItemStatus, 20), default=ItemStatus.intact, nullable=False
    )

    abilities: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    restrictions: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    origin: Mapped[str | None] = mapped_column(Text)
    secret_fact_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)

    first_appearance_chapter: Mapped[int | None] = mapped_column(Integer)
    last_appearance_chapter: Mapped[int | None] = mapped_column(Integer)

    owner: Mapped["Character | None"] = relationship()
    location: Mapped["Location | None"] = relationship()
