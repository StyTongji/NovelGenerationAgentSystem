"""Canonical story state service.

Owns every write to canonical entities in Phase 1:
bootstrap, versioned character states, facts + knowledge isolation,
append-only event log, plot structures, and snapshot serialization.

Agents (Phase 2+) will only ever propose deltas; this layer validates
and applies them inside a transaction.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Ability,
    Arc,
    Character,
    CharacterKnowledge,
    CharacterState,
    Constraint,
    Event,
    EventEffect,
    Faction,
    Foreshadowing,
    Item,
    Location,
    Novel,
    PlotDebt,
    PlotThread,
    PowerSystem,
    Realm,
    StoryFact,
    StorySnapshot,
)
from app.models.enums import (
    AliveStatus,
    KnowledgeStatus,
    SecrecyLevel,
    TruthStatus,
)
from app.repositories.character import CharacterRepository
from app.repositories.novel import FactRepository, NovelRepository, SnapshotRepository
from app.repositories.plot import (
    ArcRepository,
    ConstraintRepository,
    DebtRepository,
    EventRepository,
    ForeshadowRepository,
    PlotRepository,
)
from app.repositories.world import (
    FactionRepository,
    ItemRepository,
    LocationRepository,
    PowerSystemRepository,
)
from app.schemas.story import (
    BootstrapWorldRequest,
    CharacterIn,
    ItemIn,
    LocationIn,
    StoryFactIn,
)
from app.services.power import PowerSystemDomain, RealmOrderError, RealmPosition


class StoryStateError(Exception):
    """Domain-level validation failure; caller must not commit."""


@dataclass
class BootstrapResult:
    novel_id: uuid.UUID
    character_ids: dict[str, uuid.UUID]
    realm_ids: dict[str, uuid.UUID]
    location_ids: dict[str, uuid.UUID]
    fact_ids: dict[str, uuid.UUID]
    plot_thread_ids: dict[str, uuid.UUID]


class StoryStateService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.novels = NovelRepository(session)
        self.characters = CharacterRepository(session)
        self.facts = FactRepository(session)
        self.snapshots = SnapshotRepository(session)
        self.power = PowerSystemRepository(session)
        self.locations = LocationRepository(session)
        self.factions = FactionRepository(session)
        self.items = ItemRepository(session)
        self.events = EventRepository(session)
        self.threads = PlotRepository(session)
        self.arcs = ArcRepository(session)
        self.debts = DebtRepository(session)
        self.foreshadows = ForeshadowRepository(session)
        self.constraints = ConstraintRepository(session)

    # ------------------------------------------------------------------ novel

    async def create_novel(
        self,
        title: str,
        genre: str | None = None,
        premise: str | None = None,
        style_profile: str | None = None,
    ) -> Novel:
        novel = Novel(title=title, genre=genre, premise=premise, style_profile=style_profile)
        return await self.novels.add(novel)

    # -------------------------------------------------------------- bootstrap

    async def bootstrap_world(self, novel_id: uuid.UUID, data: BootstrapWorldRequest) -> BootstrapResult:
        novel = await self.novels.get(novel_id)
        if novel is None:
            raise StoryStateError(f"Novel {novel_id} not found")

        location_ids = await self._create_locations(novel_id, data.locations)
        character_ids = await self._create_characters(novel_id, data.characters, location_ids)
        # Faction leaders reference the initial characters, so create those first.
        await self._create_factions(novel_id, data.factions)
        realm_ids = await self._create_power_system(novel_id, data.power_system)
        await self._set_initial_states(novel_id, data.characters, character_ids, realm_ids, location_ids)
        await self._create_items(novel_id, data.items, character_ids, location_ids)
        fact_ids = await self._create_facts(novel_id, data.initial_facts, character_ids)
        thread_ids = await self._create_plot_threads(novel_id, data.plot_threads)
        await self._create_debts(novel_id, data.plot_debts)
        if data.arc is not None:
            arc = Arc(
                novel_id=novel_id,
                title=data.arc.title,
                start_chapter=data.arc.start_chapter,
                planned_end_chapter=data.arc.planned_end_chapter,
                goal=data.arc.goal,
                major_beats=data.arc.major_beats,
                status="active",
            )
            await self.arcs.add(arc)
        await self._create_constraints(novel_id, data.constraints)

        return BootstrapResult(
            novel_id=novel_id,
            character_ids=character_ids,
            realm_ids=realm_ids,
            location_ids=location_ids,
            fact_ids=fact_ids,
            plot_thread_ids=thread_ids,
        )

    async def _create_power_system(self, novel_id, power_system_in) -> dict[str, uuid.UUID]:
        if await self.power.find_by_name(novel_id, power_system_in.name) is not None:
            raise StoryStateError(f"Power system {power_system_in.name!r} already exists")
        ps = PowerSystem(
            novel_id=novel_id,
            name=power_system_in.name,
            description=power_system_in.description,
            rules=power_system_in.rules,
        )
        await self.power.add(ps)
        realms: list[tuple[Realm, object]] = []
        for realm_in in power_system_in.realms:
            realm = Realm(
                power_system_id=ps.id,
                name=realm_in.name,
                rank=realm_in.rank,
                sublevels=realm_in.sublevels,
                requirements=realm_in.requirements,
                capabilities=realm_in.capabilities,
            )
            self.session.add(realm)
            realms.append((realm, realm_in))
        await self.session.flush()
        return {realm_in.name: realm.id for realm, realm_in in realms}

    async def _create_locations(self, novel_id, locations_in: list[LocationIn]) -> dict[str, uuid.UUID]:
        pending: list[tuple[Location, LocationIn]] = []
        for loc_in in locations_in:
            loc = Location(novel_id=novel_id, name=loc_in.name, description=loc_in.description)
            self.session.add(loc)
            pending.append((loc, loc_in))
        await self.session.flush()
        ids = {loc_in.name: loc.id for loc, loc_in in pending}
        for loc, loc_in in pending:
            if loc_in.parent_name:
                parent_id = ids.get(loc_in.parent_name)
                if parent_id is None:
                    raise StoryStateError(f"Unknown parent location {loc_in.parent_name!r}")
                loc.parent_location_id = parent_id
        return ids

    async def _create_factions(self, novel_id, factions_in) -> list[Faction]:
        created = []
        for f_in in factions_in:
            faction = Faction(
                novel_id=novel_id, name=f_in.name, type=f_in.type,
                description=f_in.description, goals=f_in.goals,
            )
            self.session.add(faction)
            created.append((faction, f_in))
        await self.session.flush()
        for faction, f_in in created:
            if f_in.leader_name:
                leader = await self.characters.find_by_name(novel_id, f_in.leader_name)
                if leader is not None:
                    faction.leader_character_id = leader.id
        return [faction for faction, _ in created]

    async def _create_characters(self, novel_id, characters_in: list[CharacterIn], location_ids) -> dict[str, uuid.UUID]:
        pending: list[tuple[Character, CharacterIn]] = []
        for c_in in characters_in:
            if await self.characters.find_by_name(novel_id, c_in.name) is not None:
                raise StoryStateError(f"Character {c_in.name!r} already exists")
            character = Character(
                novel_id=novel_id,
                name=c_in.name,
                aliases=c_in.aliases,
                gender=c_in.gender,
                description=c_in.description,
                personality_traits=c_in.personality_traits,
                background=c_in.background,
                is_protagonist=c_in.is_protagonist,
            )
            self.session.add(character)
            pending.append((character, c_in))
        await self.session.flush()
        return {c_in.name: character.id for character, c_in in pending}

    async def _set_initial_states(
        self, novel_id, characters_in: list[CharacterIn], character_ids, realm_ids, location_ids
    ) -> None:
        for c_in in characters_in:
            realm_id = realm_ids.get(c_in.realm_name) if c_in.realm_name else None
            if c_in.realm_name and realm_id is None:
                raise StoryStateError(f"Unknown realm {c_in.realm_name!r} for {c_in.name!r}")
            location_id = location_ids.get(c_in.location_name) if c_in.location_name else None
            if c_in.location_name and location_id is None:
                raise StoryStateError(f"Unknown location {c_in.location_name!r} for {c_in.name!r}")
            state = CharacterState(
                character_id=character_ids[c_in.name],
                chapter_number=0,
                alive_status=AliveStatus.alive,
                realm_id=realm_id,
                subrealm=c_in.subrealm,
                current_location_id=location_id,
                physical_condition=c_in.physical_condition,
                current_goals=c_in.current_goals,
            )
            await self.characters.add_state(state)

    async def _create_items(self, novel_id, items_in: list[ItemIn], character_ids, location_ids) -> None:
        for i_in in items_in:
            owner_id = character_ids.get(i_in.owner_name) if i_in.owner_name else None
            if i_in.owner_name and owner_id is None:
                raise StoryStateError(f"Item {i_in.name!r} owner {i_in.owner_name!r} not found")
            location_id = location_ids.get(i_in.location_name) if i_in.location_name else None
            item = Item(
                novel_id=novel_id,
                name=i_in.name,
                aliases=i_in.aliases,
                type=i_in.type,
                grade=i_in.grade,
                description=i_in.description,
                current_owner_character_id=owner_id,
                current_location_id=location_id,
                abilities=i_in.abilities,
                restrictions=i_in.restrictions,
            )
            self.session.add(item)
        await self.session.flush()

    async def _create_facts(self, novel_id, facts_in: list[StoryFactIn], character_ids) -> dict[str, uuid.UUID]:
        ids: dict[str, uuid.UUID] = {}
        for f_in in facts_in:
            fact = StoryFact(
                novel_id=novel_id,
                statement=f_in.statement,
                truth_status=f_in.truth_status,
                secrecy=f_in.secrecy,
                category=f_in.category,
            )
            self.session.add(fact)
            await self.session.flush()
            ids[f_in.statement] = fact.id
            for name in f_in.known_by:
                character_id = character_ids.get(name)
                if character_id is None:
                    raise StoryStateError(f"Fact known_by character {name!r} not found")
                self.session.add(
                    CharacterKnowledge(
                        character_id=character_id,
                        fact_id=fact.id,
                        status=KnowledgeStatus.known,
                        confidence=1.0,
                    )
                )
            for name in f_in.suspected_by:
                character_id = character_ids.get(name)
                if character_id is None:
                    raise StoryStateError(f"Fact suspected_by character {name!r} not found")
                self.session.add(
                    CharacterKnowledge(
                        character_id=character_id,
                        fact_id=fact.id,
                        status=KnowledgeStatus.suspected,
                        confidence=0.4,
                    )
                )
        await self.session.flush()
        return ids

    async def _create_plot_threads(self, novel_id, threads_in) -> dict[str, uuid.UUID]:
        pending: list[tuple[PlotThread, object]] = []
        for t_in in threads_in:
            thread = PlotThread(
                novel_id=novel_id,
                title=t_in.title,
                thread_type=t_in.thread_type,
                status=t_in.status,
                truth=t_in.truth,
                public_summary=t_in.public_summary,
                priority=t_in.priority,
            )
            self.session.add(thread)
            pending.append((thread, t_in))
        await self.session.flush()
        return {t_in.title: thread.id for thread, t_in in pending}

    async def _create_debts(self, novel_id, debts_in) -> None:
        for d_in in debts_in:
            debt = PlotDebt(
                novel_id=novel_id,
                debt_type=d_in.debt_type,
                description=d_in.description,
                created_chapter=d_in.created_chapter,
                due_chapter=d_in.due_chapter,
                due_story_time=d_in.due_story_time,
            )
            self.session.add(debt)
        await self.session.flush()

    async def _create_constraints(self, novel_id, constraints_in) -> None:
        for c_in in constraints_in:
            constraint = Constraint(
                novel_id=novel_id,
                type=c_in.type,
                scope=c_in.scope,
                statement=c_in.statement,
                start_chapter=c_in.start_chapter,
                end_chapter=c_in.end_chapter,
            )
            self.session.add(constraint)
        await self.session.flush()

    # ------------------------------------------------------- character state

    async def get_current_state(self, character_id: uuid.UUID) -> CharacterState | None:
        return await self.characters.current_state(character_id)

    async def update_character_state(
        self,
        character_id: uuid.UUID,
        chapter_number: int,
        *,
        alive_status: AliveStatus | None = None,
        realm_name: str | None = None,
        subrealm: str | None = None,
        location_name: str | None = None,
        physical_condition: str | None = None,
        mental_condition: str | None = None,
        current_goals: list[str] | None = None,
        wealth_summary: str | None = None,
        allow_realm_jump: int = 1,
    ) -> CharacterState:
        """Append a new versioned state row. Never mutates history rows.

        Realm changes are validated against the power system ordering.
        """
        current = await self.characters.current_state(character_id)
        if current is None:
            raise StoryStateError(f"Character {character_id} has no initial state")
        if chapter_number <= current.chapter_number:
            raise StoryStateError(
                f"chapter_number must be > {current.chapter_number} (state rows are "
                "immutable; pass the chapter in which the change happens)"
            )

        realm_id = current.realm_id
        if realm_name is not None:
            realm = await self.power.find_realm_by_name(self._novel_of(character_id), realm_name)
            if realm is None:
                raise RealmOrderError(f"Unknown realm {realm_name!r}")
            if current.realm is not None:
                domain = PowerSystemDomain(self.session, self._novel_of(character_id))
                current_pos = RealmPosition(
                    rank=current.realm.rank, name=current.realm.name, subrealm=current.subrealm
                )
                target_pos = RealmPosition(rank=realm.rank, name=realm.name, subrealm=subrealm)
                await domain.validate_progression(current_pos, target_pos, max_jump=allow_realm_jump)
            realm_id = realm.id

        location_id = current.current_location_id
        if location_name is not None:
            novel_id = self._novel_of(character_id)
            location = await self.locations.find_by_name(novel_id, location_name)
            if location is None:
                raise StoryStateError(f"Unknown location {location_name!r}")
            location_id = location.id

        new_state = CharacterState(
            character_id=character_id,
            chapter_number=chapter_number,
            alive_status=alive_status or current.alive_status,
            current_location_id=location_id,
            realm_id=realm_id,
            subrealm=subrealm or current.subrealm,
            physical_condition=physical_condition or current.physical_condition,
            mental_condition=mental_condition or current.mental_condition,
            current_goals=current_goals if current_goals is not None else current.current_goals,
            reputation=current.reputation,
            wealth_summary=wealth_summary or current.wealth_summary,
        )
        return await self.characters.add_state(new_state)

    def _novel_of(self, character_id: uuid.UUID) -> uuid.UUID:
        # resolved lazily by caller context; Phase 1 keeps a sync lookup
        character = self.session.sync_session.get(Character, character_id)
        if character is None:
            raise StoryStateError(f"Character {character_id} not found")
        return character.novel_id

    # ------------------------------------------------------------- knowledge

    async def add_fact(
        self,
        novel_id: uuid.UUID,
        statement: str,
        secrecy: SecrecyLevel,
        *,
        truth_status: TruthStatus = TruthStatus.true,
        introduced_chapter: int | None = None,
    ) -> StoryFact:
        existing = await self.facts.find_by_statement(novel_id, statement)
        if existing is not None:
            return existing
        fact = StoryFact(
            novel_id=novel_id,
            statement=statement,
            secrecy=secrecy,
            truth_status=truth_status,
            introduced_chapter=introduced_chapter,
        )
        return await self.facts.add(fact)

    async def grant_knowledge(
        self,
        character_id: uuid.UUID,
        fact_id: uuid.UUID,
        *,
        status: KnowledgeStatus = KnowledgeStatus.known,
        confidence: float = 1.0,
        learned_chapter: int | None = None,
        learned_event_id: uuid.UUID | None = None,
    ) -> CharacterKnowledge:
        existing = await self.characters.get_knowledge(character_id, fact_id)
        if existing is not None:
            existing.status = status
            existing.confidence = confidence
            return existing
        knowledge = CharacterKnowledge(
            character_id=character_id,
            fact_id=fact_id,
            status=status,
            confidence=confidence,
            learned_chapter=learned_chapter,
            learned_event_id=learned_event_id,
        )
        return await self.characters.add_knowledge(knowledge)

    async def visible_facts_for(self, pov_character_id: uuid.UUID) -> list[StoryFact]:
        """Facts the POV character may legitimately see in their context.

        public/limited facts + facts with a knowledge row for this character.
        secret/top_secret facts only appear when explicitly known.
        """
        novel_id = self._novel_of(pov_character_id)
        all_facts = await self.facts.list_for_novel(novel_id)
        knowledge_rows = await self.characters.knowledge_of(pov_character_id)
        known_fact_ids = {k.fact_id for k in knowledge_rows if k.status == KnowledgeStatus.known}
        visible = []
        for fact in all_facts:
            if fact.secrecy in (SecrecyLevel.public, SecrecyLevel.limited):
                visible.append(fact)
            elif fact.id in known_fact_ids:
                visible.append(fact)
        return visible

    # ----------------------------------------------------------------- event

    async def record_event(
        self,
        novel_id: uuid.UUID,
        *,
        chapter_number: int,
        event_type: str,
        summary: str,
        participant_ids: list[uuid.UUID] | None = None,
        location_id: uuid.UUID | None = None,
        importance: float = 0.5,
        scene_index: int | None = None,
        title: str | None = None,
        happened_at_story_time: str | None = None,
        evidence_span: str | None = None,
        effects: list[dict] | None = None,
        extra_metadata: dict | None = None,
    ) -> Event:
        """Append an immutable event to the log. Effects are structured rows
        describing how this event changed canonical targets."""
        event = Event(
            novel_id=novel_id,
            chapter_number=chapter_number,
            scene_index=scene_index,
            event_type=event_type,
            title=title,
            summary=summary,
            participant_ids=[str(p) for p in (participant_ids or [])],
            location_id=location_id,
            importance=importance,
            happened_at_story_time=happened_at_story_time,
            evidence_span=evidence_span,
            extra_metadata=extra_metadata or {},
        )
        # attach effects while the event is still pending (avoids a lazy load)
        for effect in effects or []:
            event.effects.append(
                EventEffect(
                    target_type=effect["target_type"],
                    target_id=str(effect["target_id"]),
                    field=effect["field"],
                    operation=effect.get("operation", "set"),
                    old_value=effect.get("old_value"),
                    new_value=effect.get("new_value"),
                )
            )
        await self.events.add(event)
        return event

    # -------------------------------------------------------------- snapshot

    async def build_state_dict(self, novel_id: uuid.UUID) -> dict:
        novel = await self.novels.get(novel_id)
        if novel is None:
            raise StoryStateError(f"Novel {novel_id} not found")

        characters_out = []
        for character in await self.characters.list_for_novel(novel_id):
            state = await self.characters.current_state(character.id)
            realm = state.realm if state else None
            location = state.location if state else None
            characters_out.append(
                {
                    "id": str(character.id),
                    "name": character.name,
                    "aliases": character.aliases,
                    "is_protagonist": character.is_protagonist,
                    "state": {
                        "chapter_number": state.chapter_number if state else 0,
                        "alive_status": state.alive_status.value if state else None,
                        "realm": realm.name if realm else None,
                        "subrealm": state.subrealm if state else None,
                        "location": location.name if location else None,
                        "physical_condition": state.physical_condition if state else None,
                        "current_goals": state.current_goals if state else [],
                    },
                }
            )

        items_out = [
            {
                "id": str(item.id),
                "name": item.name,
                "type": item.type,
                "grade": item.grade,
                "status": item.status.value if hasattr(item.status, "value") else str(item.status),
                "owner": str(item.current_owner_character_id) if item.current_owner_character_id else None,
            }
            for item in await self.items.list_for_novel(novel_id)
        ]
        threads_out = [
            {
                "id": str(t.id),
                "title": t.title,
                "status": t.status.value,
                "priority": t.priority,
                "last_advanced_chapter": t.last_advanced_chapter,
            }
            for t in await self.threads.list_threads(novel_id)
        ]
        debts_out = [
            {
                "id": str(d.id),
                "description": d.description,
                "status": d.status.value,
                "due_chapter": d.due_chapter,
            }
            for d in await self.debts.list_open(novel_id)
        ]
        return {
            "novel": {
                "id": str(novel.id),
                "title": novel.title,
                "current_chapter_number": novel.current_chapter_number,
            },
            "characters": characters_out,
            "items": items_out,
            "plot_threads": threads_out,
            "open_plot_debts": debts_out,
        }

    async def create_snapshot(self, novel_id: uuid.UUID, after_chapter_number: int) -> StorySnapshot:
        state = await self.build_state_dict(novel_id)
        serialized = json.dumps(state, ensure_ascii=False, sort_keys=True)
        snapshot = StorySnapshot(
            novel_id=novel_id,
            after_chapter_number=after_chapter_number,
            serialized_state=state,
            state_hash=hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
        )
        return await self.snapshots.add(snapshot)

    # ------------------------------------------------------------- utilities

    async def set_current_arc(self, novel_id: uuid.UUID, arc_id: uuid.UUID) -> None:
        novel = await self.novels.get(novel_id)
        if novel is None:
            raise StoryStateError(f"Novel {novel_id} not found")
        novel.current_arc_id = arc_id
        await self.session.flush()
