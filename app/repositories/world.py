"""World repositories: power system / realm / location / faction / item."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.models import (
    Faction,
    Item,
    Location,
    PowerSystem,
    Realm,
)
from app.repositories.base import BaseRepository


class PowerSystemRepository(BaseRepository[PowerSystem]):
    model = PowerSystem

    async def find_by_name(self, novel_id: uuid.UUID, name: str) -> PowerSystem | None:
        result = await self.session.execute(
            select(PowerSystem).where(
                PowerSystem.novel_id == novel_id, PowerSystem.name == name
            )
        )
        return result.scalar_one_or_none()

    async def realms_for(self, power_system_id: uuid.UUID) -> list[Realm]:
        result = await self.session.execute(
            select(Realm)
            .where(Realm.power_system_id == power_system_id)
            .order_by(Realm.rank.asc())
        )
        return list(result.scalars().all())

    async def find_realm_by_name(self, novel_id: uuid.UUID, name: str) -> Realm | None:
        """Realm lookup scoped by novel via its power system."""
        result = await self.session.execute(
            select(Realm)
            .join(PowerSystem, Realm.power_system_id == PowerSystem.id)
            .where(PowerSystem.novel_id == novel_id, Realm.name == name)
        )
        return result.scalar_one_or_none()


class LocationRepository(BaseRepository[Location]):
    model = Location

    async def list_for_novel(self, novel_id: uuid.UUID) -> list[Location]:
        result = await self.session.execute(
            select(Location).where(Location.novel_id == novel_id)
        )
        return list(result.scalars().all())

    async def find_by_name(self, novel_id: uuid.UUID, name: str) -> Location | None:
        result = await self.session.execute(
            select(Location).where(Location.novel_id == novel_id, Location.name == name)
        )
        return result.scalar_one_or_none()


class FactionRepository(BaseRepository[Faction]):
    model = Faction

    async def find_all_for_novel(self, novel_id: uuid.UUID) -> list[Faction]:
        result = await self.session.execute(
            select(Faction).where(Faction.novel_id == novel_id)
        )
        return list(result.scalars().all())

    async def find_by_name(self, novel_id: uuid.UUID, name: str) -> Faction | None:
        result = await self.session.execute(
            select(Faction).where(Faction.novel_id == novel_id, Faction.name == name)
        )
        return result.scalar_one_or_none()


class ItemRepository(BaseRepository[Item]):
    model = Item

    async def list_for_novel(self, novel_id: uuid.UUID) -> list[Item]:
        result = await self.session.execute(
            select(Item).where(Item.novel_id == novel_id)
        )
        return list(result.scalars().all())

    async def owned_by(self, character_ids: list[uuid.UUID]) -> list[Item]:
        if not character_ids:
            return []
        result = await self.session.execute(
            select(Item).where(Item.current_owner_character_id.in_(character_ids))
        )
        return list(result.scalars().all())

    async def find_by_name(self, novel_id: uuid.UUID, name: str) -> Item | None:
        result = await self.session.execute(
            select(Item).where(Item.novel_id == novel_id)
        )
        for item in result.scalars():
            if item.name == name or name in (item.aliases or []):
                return item
        return None
