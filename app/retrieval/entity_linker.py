"""Entity-first retrieval: exact entity name/alias matching in query text.

Known entities (characters, items, locations, factions, plot threads) are
matched by name or alias substring in the user direction / scene plan
BEFORE any semantic expansion. Deterministic; no LLM, no embeddings.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.character import CharacterRepository
from app.repositories.plot import PlotRepository
from app.repositories.world import FactionRepository, ItemRepository, LocationRepository


@dataclass
class LinkedEntities:
    character_ids: list[uuid.UUID] = field(default_factory=list)
    item_ids: list[uuid.UUID] = field(default_factory=list)
    location_ids: list[uuid.UUID] = field(default_factory=list)
    faction_ids: list[uuid.UUID] = field(default_factory=list)
    plot_thread_ids: list[uuid.UUID] = field(default_factory=list)
    matched_names: list[str] = field(default_factory=list)

    def merged_ids(self) -> list[uuid.UUID]:
        return (
            self.character_ids + self.item_ids + self.location_ids
            + self.faction_ids + self.plot_thread_ids
        )


def _matched_candidate(name: str, aliases: list[str], text: str) -> str | None:
    for candidate in [*aliases, name]:
        # Chinese entity names are short; require >= 2 chars to avoid noise
        if len(candidate) >= 2 and candidate in text:
            return candidate
    return None


class EntityLinker:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.characters = CharacterRepository(session)
        self.items = ItemRepository(session)
        self.locations = LocationRepository(session)
        self.factions = FactionRepository(session)
        self.threads = PlotRepository(session)

    async def link(self, novel_id: uuid.UUID, text: str | None) -> LinkedEntities:
        linked = LinkedEntities()
        if not text:
            return linked

        for character in await self.characters.list_for_novel(novel_id):
            matched = _matched_candidate(character.name, character.aliases or [], text)
            if matched:
                linked.character_ids.append(character.id)
                linked.matched_names.append(matched)

        for item in await self.items.list_for_novel(novel_id):
            matched = _matched_candidate(item.name, item.aliases or [], text)
            if matched:
                linked.item_ids.append(item.id)
                linked.matched_names.append(matched)

        for location in await self.locations.list_for_novel(novel_id):
            matched = _matched_candidate(location.name, [], text)
            if matched:
                linked.location_ids.append(location.id)
                linked.matched_names.append(matched)

        for faction in await self.factions.find_all_for_novel(novel_id):
            matched = _matched_candidate(faction.name, [], text)
            if matched:
                linked.faction_ids.append(faction.id)
                linked.matched_names.append(matched)

        for thread in await self.threads.list_threads(novel_id):
            if len(thread.title) >= 2 and thread.title in text:
                linked.plot_thread_ids.append(thread.id)
                linked.matched_names.append(thread.title)

        return linked
