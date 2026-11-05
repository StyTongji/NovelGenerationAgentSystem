"""Power system domain logic: realm ordering and progression validation.

This is deterministic code, not LLM logic: "林凡目前什么境界" style questions
must be answered from the structured database, never from text memory.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PowerSystem, Realm
from app.repositories.world import PowerSystemRepository


class RealmOrderError(ValueError):
    pass


@dataclass(frozen=True)
class RealmPosition:
    rank: int
    name: str
    subrealm: str | None = None


class PowerSystemDomain:
    """Realm ranking / progression rules for one novel's power system."""

    def __init__(self, session: AsyncSession, novel_id: uuid.UUID) -> None:
        self.session = session
        self.novel_id = novel_id

    async def realms(self) -> list[Realm]:
        result = await self.session.execute(
            select(Realm)
            .join(PowerSystem, Realm.power_system_id == PowerSystem.id)
            .where(PowerSystem.novel_id == self.novel_id)
            .order_by(Realm.rank.asc())
        )
        return list(result.scalars().all())

    async def position_of(self, realm_name: str, subrealm: str | None = None) -> RealmPosition:
        repo = PowerSystemRepository(self.session)
        realm = await repo.find_realm_by_name(self.novel_id, realm_name)
        if realm is None:
            raise RealmOrderError(f"Unknown realm {realm_name!r} for novel {self.novel_id}")
        return RealmPosition(rank=realm.rank, name=realm.name, subrealm=subrealm)

    async def validate_progression(
        self,
        current: RealmPosition,
        target: RealmPosition,
        *,
        max_jump: int = 1,
    ) -> None:
        """A realm jump bigger than ``max_jump`` ranks without an in-story
        justification event is forbidden at the domain layer."""
        if target.rank < current.rank:
            raise RealmOrderError(
                f"Realm regression {current.name} -> {target.name} requires explicit events"
            )
        if target.rank - current.rank > max_jump:
            raise RealmOrderError(
                f"Illegal realm jump {current.name}(rank {current.rank}) -> "
                f"{target.name}(rank {target.rank}): exceeds {max_jump} rank(s)"
            )
