"""Character / state / knowledge / relationship repositories."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import joinedload

from app.models import (
    Character,
    CharacterKnowledge,
    CharacterState,
    Relationship,
)
from app.repositories.base import BaseRepository


class CharacterRepository(BaseRepository[Character]):
    model = Character

    async def list_for_novel(self, novel_id: uuid.UUID) -> list[Character]:
        result = await self.session.execute(
            select(Character).where(Character.novel_id == novel_id)
        )
        return list(result.scalars().all())

    async def find_by_name(self, novel_id: uuid.UUID, name: str) -> Character | None:
        """Match by name or any alias (case-sensitive exact match)."""
        result = await self.session.execute(
            select(Character).where(Character.novel_id == novel_id)
        )
        for character in result.scalars():
            if character.name == name or name in (character.aliases or []):
                return character
        return None

    async def current_state(self, character_id: uuid.UUID) -> CharacterState | None:
        result = await self.session.execute(
            select(CharacterState)
            .where(CharacterState.character_id == character_id)
            .options(joinedload(CharacterState.realm), joinedload(CharacterState.location))
            .order_by(CharacterState.chapter_number.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def state_at(self, character_id: uuid.UUID, chapter_number: int) -> CharacterState | None:
        result = await self.session.execute(
            select(CharacterState)
            .where(
                CharacterState.character_id == character_id,
                CharacterState.chapter_number <= chapter_number,
            )
            .options(joinedload(CharacterState.realm), joinedload(CharacterState.location))
            .order_by(CharacterState.chapter_number.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def list_states(self, character_id: uuid.UUID) -> list[CharacterState]:
        result = await self.session.execute(
            select(CharacterState)
            .where(CharacterState.character_id == character_id)
            .options(joinedload(CharacterState.realm), joinedload(CharacterState.location))
            .order_by(CharacterState.chapter_number.asc())
        )
        return list(result.unique().scalars().all())

    async def add_state(self, state: CharacterState) -> CharacterState:
        return await self.add(state)

    async def get_knowledge(self, character_id: uuid.UUID, fact_id: uuid.UUID) -> CharacterKnowledge | None:
        result = await self.session.execute(
            select(CharacterKnowledge).where(
                CharacterKnowledge.character_id == character_id,
                CharacterKnowledge.fact_id == fact_id,
            )
        )
        return result.scalar_one_or_none()

    async def knowledge_of(self, character_id: uuid.UUID) -> list[CharacterKnowledge]:
        result = await self.session.execute(
            select(CharacterKnowledge).where(CharacterKnowledge.character_id == character_id)
        )
        return list(result.scalars().all())

    async def knowers_of_fact(self, fact_id: uuid.UUID) -> list[CharacterKnowledge]:
        result = await self.session.execute(
            select(CharacterKnowledge).where(CharacterKnowledge.fact_id == fact_id)
        )
        return list(result.scalars().all())

    async def add_knowledge(self, knowledge: CharacterKnowledge) -> CharacterKnowledge:
        return await self.add(knowledge)

    async def find_relationship(
        self, novel_id: uuid.UUID, source_id: uuid.UUID, target_id: uuid.UUID, rel_type: str
    ) -> Relationship | None:
        result = await self.session.execute(
            select(Relationship).where(
                Relationship.novel_id == novel_id,
                Relationship.source_character_id == source_id,
                Relationship.target_character_id == target_id,
                Relationship.type == rel_type,
            )
        )
        return result.scalar_one_or_none()

    async def add_relationship(self, relationship: Relationship) -> Relationship:
        return await self.add(relationship)
