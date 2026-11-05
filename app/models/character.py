"""Character, versioned CharacterState, CharacterKnowledge, Relationship.

Design notes
------------
* ``Character`` is static identity (name/aliases/personality).
* ``CharacterState`` rows are versioned by ``chapter_number``: the current
  state is the row with the highest chapter_number. States are never
  mutated in place after being superseded by a later chapter.
* ``CharacterKnowledge`` links a character to a ``StoryFact``; the fact's
  text lives in exactly one place (knowledge isolation).
* ``Relationship`` is directed: source -> target.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    Boolean,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    Uuid,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, str_enum
from app.models.enums import AliveStatus, KnowledgeStatus

if TYPE_CHECKING:
    from app.models.event import Event
    from app.models.fact import StoryFact
    from app.models.location import Location
    from app.models.novel import Novel
    from app.models.power import Realm


class Character(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "characters"

    novel_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("novels.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    aliases: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    gender: Mapped[str | None] = mapped_column(String(20))
    description: Mapped[str | None] = mapped_column(Text)
    personality_traits: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    background: Mapped[str | None] = mapped_column(Text)
    immutable_notes: Mapped[str | None] = mapped_column(Text)
    is_protagonist: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    novel: Mapped["Novel"] = relationship(back_populates="characters")
    states: Mapped[list["CharacterState"]] = relationship(
        back_populates="character",
        cascade="all, delete-orphan",
        order_by="CharacterState.chapter_number",
    )
    knowledge: Mapped[list["CharacterKnowledge"]] = relationship(
        back_populates="character", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Character {self.name!r}>"


class CharacterState(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "character_states"
    __table_args__ = (
        UniqueConstraint("character_id", "chapter_number", name="uq_state_char_chapter"),
    )

    character_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("characters.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    chapter_number: Mapped[int] = mapped_column(Integer, nullable=False)
    alive_status: Mapped[AliveStatus] = mapped_column(
        str_enum(AliveStatus, 20), default=AliveStatus.alive, nullable=False
    )
    current_location_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("locations.id", ondelete="SET NULL")
    )
    realm_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("realms.id", ondelete="SET NULL")
    )
    subrealm: Mapped[str | None] = mapped_column(String(50))
    physical_condition: Mapped[str | None] = mapped_column(Text)
    mental_condition: Mapped[str | None] = mapped_column(Text)
    current_goals: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    reputation: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    wealth_summary: Mapped[str | None] = mapped_column(Text)

    character: Mapped["Character"] = relationship(back_populates="states")
    location: Mapped["Location | None"] = relationship()
    realm: Mapped["Realm | None"] = relationship()


class CharacterKnowledge(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "character_knowledge"
    __table_args__ = (
        UniqueConstraint("character_id", "fact_id", name="uq_knowledge_char_fact"),
    )

    character_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("characters.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    fact_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("story_facts.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[KnowledgeStatus] = mapped_column(
        str_enum(KnowledgeStatus, 20), default=KnowledgeStatus.known, nullable=False
    )
    confidence: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    learned_event_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("events.id", ondelete="SET NULL")
    )
    learned_chapter: Mapped[int | None] = mapped_column(Integer)

    character: Mapped["Character"] = relationship(back_populates="knowledge")
    fact: Mapped["StoryFact"] = relationship()


class Relationship(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "relationships"
    __table_args__ = (
        UniqueConstraint(
            "source_character_id", "target_character_id", "type", name="uq_rel_pair_type"
        ),
    )

    novel_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("novels.id", ondelete="CASCADE"), nullable=False
    )
    source_character_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("characters.id", ondelete="CASCADE"), nullable=False
    )
    target_character_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("characters.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[str] = mapped_column(String(50), nullable=False)
    trust: Mapped[int] = mapped_column(default=0, nullable=False)
    affection: Mapped[int] = mapped_column(default=0, nullable=False)
    hostility: Mapped[int] = mapped_column(default=0, nullable=False)
    debt: Mapped[str | None] = mapped_column(Text)
    public_description: Mapped[str | None] = mapped_column(Text)
    hidden_description: Mapped[str | None] = mapped_column(Text)
    last_changed_event_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("events.id", ondelete="SET NULL")
    )

    source: Mapped["Character"] = relationship(foreign_keys=[source_character_id])
    target: Mapped["Character"] = relationship(foreign_keys=[target_character_id])
    last_changed_event: Mapped["Event | None"] = relationship()
