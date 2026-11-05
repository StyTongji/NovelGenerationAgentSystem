"""ContextBuilder: compiles canonical state into agent-safe contexts.

Boundary: repositories -> retrieval -> ContextBuilder -> agent DTOs.
Agents never touch repositories; this module never calls an LLM.

Writer assembly routes EVERY text through the KnowledgeFilter at this final
boundary (facts, event summaries, thread public summaries, foreshadowing
hints). Author-level summaries never enter WriterContext at all.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.context.budget import DEFAULT_BUDGET, ContextBudget, tail, trim_events
from app.context.knowledge_filter import KnowledgeFilter
from app.context.models import (
    ArcView,
    ChapterGuidance,
    ExtractionCharacterState,
    ExtractionFact,
    ExtractionItem,
    KnowledgeRowView,
    ChapterSummaryView,
    CharacterStateView,
    CharacterView,
    ConstraintView,
    ContextDebugReport,
    ContextFact,
    ContinuityContext,
    DirectorContext,
    EventView,
    ForeshadowingDirectorView,
    ItemView,
    LocationView,
    MemoryExtractionContext,
    PlotDebtView,
    PlotThreadDirectorView,
    RecentContext,
    RelationshipView,
    WriterContext,
    WriterSafeForeshadowing,
    WriterSafePlotThread,
)
from app.models import (
    Arc,
    Character,
    Chapter,
    Event,
    Item,
    Location,
    PlotDebt,
    PlotThread,
    PowerSystem,
    StoryFact,
)
from app.models.enums import ThreadStatus
from app.retrieval.entity_linker import EntityLinker
from app.retrieval.event_retriever import StructuredEventRetriever
from app.retrieval.hybrid import HybridRetriever
from app.retrieval.models import RetrievalQuery, RetrievedMemory, SourceType
from app.repositories.character import CharacterRepository
from app.repositories.chapter import ChapterRepository, ChapterSummaryRepository
from app.repositories.plot import (
    ConstraintRepository,
    DebtRepository,
    ForeshadowRepository,
    PlotRepository,
)
from app.repositories.world import ItemRepository, LocationRepository, PowerSystemRepository
from app.schemas.agent_outputs import WriterSafeScenePlan

DUE_SOON_WINDOW = 3


class ContextBuilder:
    def __init__(self, session: AsyncSession, budget: ContextBudget | None = None) -> None:
        self.session = session
        self.budget = budget or DEFAULT_BUDGET
        self.characters = CharacterRepository(session)
        self.items = ItemRepository(session)
        self.locations = LocationRepository(session)
        self.threads = PlotRepository(session)
        self.debts = DebtRepository(session)
        self.foreshadows = ForeshadowRepository(session)
        self.constraints = ConstraintRepository(session)
        self.arcs = PlotRepository(session)
        self.chapters = ChapterRepository(session)
        self.summaries = ChapterSummaryRepository(session)
        self.power_repo = PowerSystemRepository(session)
        self.linker = EntityLinker(session)
        # debug of the most recent build
        self.debug = ContextDebugReport()

    # ---------------------------------------------------------------- director

    async def build_director_context(
        self,
        novel_id: uuid.UUID,
        chapter_number: int,
        user_direction: str | None = None,
    ) -> DirectorContext:
        self.debug = ContextDebugReport()
        debug = self.debug
        novel = await self._novel(novel_id)

        linked = await self.linker.link(novel_id, user_direction)

        all_characters = await self.characters.list_for_novel(novel_id)
        active_characters = [
            await self._character_view(c) for c in all_characters
        ]
        active_characters = [v for v in active_characters if v.state and v.state.alive_status == "alive"]
        relevant_ids = set(str(i) for i in linked.character_ids)
        relevant_characters = [v for v in active_characters if v.id in relevant_ids]
        if not relevant_characters:
            protagonist = [v for v in active_characters if v.is_protagonist]
            relevant_characters = protagonist or active_characters[:3]

        relevant_events = await self._retrieve_events(
            novel_id, chapter_number, user_direction, linked, debug
        )

        threads = await self.threads.list_threads(novel_id)
        director_threads = [self._thread_director_view(t, chapter_number) for t in threads]
        active = [t for t in director_threads if t.status in ("active", "progressing")]
        dormant = [t for t in director_threads if t.status == "dormant"]
        active.sort(key=lambda t: (-t.priority, t.id))
        dormant.sort(key=lambda t: (-(t.chapters_since_last_progress or 0), t.id))

        debts = [
            self._debt_view(d, chapter_number)
            for d in await self.debts.list_open(novel_id)
        ]
        debts.sort(key=self._debt_sort_key)

        unresolved = [
            self._foreshadow_director_view(f)
            for f in await self.foreshadows.list_unresolved(novel_id)
        ]

        hard, soft = await self._constraints_for_chapter(novel_id, chapter_number)

        summaries = await self.summaries.recent_summaries(
            novel_id, chapter_number, self.budget.max_recent_summaries
        )

        arc_view = None
        if novel.current_arc_id:
            arc = await self.session.get(Arc, novel.current_arc_id)
            if arc is not None:
                arc_view = ArcView(
                    id=str(arc.id),
                    title=arc.title,
                    goal=arc.goal,
                    start_chapter=arc.start_chapter,
                    planned_end_chapter=arc.planned_end_chapter,
                    major_beats=arc.major_beats,
                    ending_hook=arc.ending_hook,
                    status=arc.status.value if hasattr(arc.status, "value") else str(arc.status),
                )

        knowledge_filter = KnowledgeFilter(self.session, novel_id)
        await knowledge_filter.load(pov_character_id=self._any_character_id(all_characters))
        planning_secrets = await knowledge_filter.planning_secrets(debug, cap=self.budget.max_planning_secrets)

        story_time = self._latest_story_time(relevant_events)

        return DirectorContext(
            novel_id=str(novel_id),
            chapter_number=chapter_number,
            current_story_time=story_time,
            current_arc=arc_view,
            recent_chapter_summaries=[
                ChapterSummaryView(chapter_number=s.chapter_number, short_summary=s.short_summary)
                for s in summaries
            ],
            active_characters=active_characters,
            relevant_characters=relevant_characters,
            relevant_locations=await self._location_views(novel_id, linked.location_ids),
            relevant_items=await self._item_views(novel_id, linked.item_ids),
            active_plot_threads=active[: self.budget.max_plot_threads],
            dormant_plot_threads=dormant[: self.budget.max_plot_threads],
            active_foreshadowing=unresolved,
            open_plot_debts=debts,
            relevant_events=relevant_events,
            planning_secrets=planning_secrets,
            hard_constraints=hard,
            soft_constraints=soft,
            user_direction=user_direction,
        )

    # ------------------------------------------------------------------ writer

    async def build_writer_context(
        self,
        novel_id: uuid.UUID,
        chapter_number: int,
        pov_character_id: uuid.UUID,
        *,
        safe_scene_plan: WriterSafeScenePlan | None = None,
        scene_index: int | None = None,
        previous_scene_ending: str | None = None,
        writer_instruction: str | None = None,
        chapter_guidance: ChapterGuidance | None = None,
        temporary_entities: list | None = None,
        retrieval_hint: str | None = None,
    ) -> WriterContext:
        self.debug = ContextDebugReport()
        debug = self.debug
        await self._novel(novel_id)

        pov = await self.characters.get(pov_character_id)
        if pov is None:
            raise ValueError(f"POV character {pov_character_id} not found")
        pov_view = await self._character_view(pov)

        knowledge_filter = KnowledgeFilter(self.session, novel_id)
        await knowledge_filter.load(pov_character_id)
        pov_knowledge = await knowledge_filter.pov_knowledge(debug)

        # resolve visible characters from the scene plan (names) or POV alone
        visible: list[CharacterView] = [pov_view]
        if safe_scene_plan is not None:
            for name in safe_scene_plan.participant_names():
                character = await self.characters.find_by_name(novel_id, name)
                if character is not None and character.id != pov_character_id:
                    visible.append(await self._character_view(character))
                elif character is None:
                    debug.exclude(
                        "character", "-", name, "scene participant does not exist in canon"
                    )

        # location: scene plan first, then POV current location
        location_view = None
        location_name = safe_scene_plan.location_name() if safe_scene_plan else None
        location = None
        if location_name:
            location = await self.locations.find_by_name(novel_id, location_name)
            if location is None:
                debug.exclude("location", "-", location_name, "scene location unknown in canon")
        if location is None and pov_view.state:
            for loc in await self.locations.list_for_novel(novel_id):
                if loc.name == pov_view.state.location:
                    location = loc
                    break
        if location is not None:
            location_view = LocationView(
                id=str(location.id), name=location.name, description=location.description
            )

        # relationships among visible characters (both directions, public only)
        visible_ids = {v.id for v in visible}
        pov_id = str(pov_character_id)
        relationships = await self._relationships_among(novel_id, visible_ids | {pov_id})
        relationships = [
            r for r in relationships
            # relationships inside the scene, or any relationship of the POV
            if (r.source_id in visible_ids and r.target_id in visible_ids)
            or r.source_id == pov_id or r.target_id == pov_id
        ]

        items = await self.items.owned_by([uuid.UUID(v.id) for v in visible])

        power_rules = await self._power_rules(visible)

        writer_linked = await self.linker.link(
            novel_id, retrieval_hint or (safe_scene_plan.scene_goal if safe_scene_plan else None)
        )
        relevant_events = await self._retrieve_events(
            novel_id,
            chapter_number,
            retrieval_hint or (safe_scene_plan.scene_goal if safe_scene_plan else None),
            writer_linked,
            debug,
            character_ids=[pov_character_id],
            knowledge_filter=knowledge_filter,
        )
        relevant_events = self._firewall_event_text(relevant_events, knowledge_filter, debug)


        safe_threads = await self._safe_plot_threads(novel_id, chapter_number, knowledge_filter, debug, pov_character_id)
        safe_foreshadowing = await self._safe_foreshadowing(novel_id, knowledge_filter, debug)

        hard, soft = await self._constraints_for_chapter(novel_id, chapter_number)

        scrubbed_previous_ending = await self._previous_chapter_ending(
            novel_id, chapter_number, knowledge_filter
        )

        story_time = self._latest_story_time(relevant_events)

        return WriterContext(
            novel_id=str(novel_id),
            chapter_number=chapter_number,
            scene_index=scene_index,
            story_time=story_time,
            pov_character=pov_view,
            pov_knowledge=pov_knowledge[: self.budget.max_knowledge_facts],
            location=location_view,
            visible_characters=visible,
            relevant_relationships=relationships,
            relevant_items=[
                ItemView(
                    id=str(i.id), name=i.name, type=i.type, grade=i.grade,
                    status=i.status.value if hasattr(i.status, "value") else str(i.status),
                    owner_id=str(i.current_owner_character_id) if i.current_owner_character_id else None,
                    abilities=i.abilities, restrictions=i.restrictions,
                )
                for i in items[: self.budget.max_items]
            ],
            relevant_power_rules=power_rules,
            relevant_events=relevant_events,
            safe_plot_threads=safe_threads[: self.budget.max_plot_threads],
            safe_foreshadowing=safe_foreshadowing,
            hard_constraints=hard,
            soft_constraints=soft,
            recent_context=RecentContext(
                previous_chapter_ending=scrubbed_previous_ending,
                notes=["Author-level chapter summaries are intentionally not provided to the Writer."],
            ),
            previous_scene_ending=previous_scene_ending,
            writer_instruction=writer_instruction,
            chapter_guidance=chapter_guidance,
            safe_scene_plan=safe_scene_plan,
            temporary_entities=temporary_entities or [],
        )

    # ------------------------------------------------------------ stubs 6/7

    async def build_continuity_context(
        self,
        novel_id: uuid.UUID,
        chapter_number: int,
        chapter_text: str,
        *,
        chapter_plan=None,
        scene_plans=None,
        draft=None,
    ) -> ContinuityContext:
        """Author-level continuity view (Phase 6). Relevance-filtered:
        chapter participants, referenced items, active threads, constraints,
        knowledge rows of participants and secret statements."""
        self.debug = ContextDebugReport()
        await self._novel(novel_id)
        from sqlalchemy import select as sa_select

        from app.models import Ability, CharacterKnowledge

        all_characters = await self.characters.list_for_novel(novel_id)
        views = [await self._character_view(c) for c in all_characters]
        views_by_name = {v.name: v for v in views}

        # relevance: plan participants (canonical) or everyone for chapter 0
        participant_names: set[str] = set()
        if chapter_plan is not None:
            participant_names = {
                ref.name for ref in chapter_plan.participating_characters if ref.name
            }
            for scene in chapter_plan.scenes:
                participant_names.update(scene.participants)
        # full cast for ownership/identity checks; knowledge rows below stay
        # relevance-filtered to chapter participants
        relevant_views = views

        # knowledge rows of relevant characters
        knowledge_rows: list[KnowledgeRowView] = []
        knowledge_targets = (
            [v for v in views if v.name in participant_names] or views
        )
        for view in knowledge_targets:
            for row in await self.characters.knowledge_of(uuid.UUID(view.id)):
                fact = await self.session.get(StoryFact, row.fact_id)
                if fact is None:
                    continue
                knowledge_rows.append(
                    KnowledgeRowView(
                        character_id=view.id,
                        character_name=view.name,
                        statement=fact.statement,
                        status=row.status.value if hasattr(row.status, "value") else str(row.status),
                    )
                )

        abilities: list[str] = []
        for ability in (
            await self.session.execute(sa_select(Ability).where(Ability.novel_id == novel_id))
        ).scalars():
            owner = (
                await self.session.get(Character, ability.owner_character_id)
                if ability.owner_character_id
                else None
            )
            abilities.append(f"{ability.name}|{owner.name if owner else '?'}")

        realms = [
            f"{r.name}({r.rank})"
            for r in await self.power_repo.realms_for(
                (
                    await self.session.execute(
                        sa_select(PowerSystem).where(PowerSystem.novel_id == novel_id)
                    )
                ).scalars().first().id
            )
        ] if (await self.session.execute(sa_select(PowerSystem).where(PowerSystem.novel_id == novel_id))).scalars().first() else []

        threads = await self.threads.list_threads(novel_id)
        hard, soft = await self._constraints_for_chapter(novel_id, chapter_number)
        knowledge_filter = KnowledgeFilter(self.session, novel_id)
        await knowledge_filter.load(self._any_character_id(all_characters))

        facts = await self.facts_repo_list(novel_id)
        secret_statements = [
            f.statement
            for f in facts
            if (f.secrecy.value if hasattr(f.secrecy, "value") else str(f.secrecy))
            in ("secret", "top_secret")
        ]

        import hashlib

        context = ContinuityContext(
            novel_id=str(novel_id),
            chapter_number=chapter_number,
            context_hash=hashlib.sha256(f"{novel_id}:{chapter_number}".encode()).hexdigest(),
            chapter_text=chapter_text,
            chapter_plan=chapter_plan,
            scene_plans=scene_plans or [],
            draft=draft,
            canonical_characters=relevant_views,
            character_knowledge=knowledge_rows,
            items=[
                ItemView(
                    id=str(i.id), name=i.name, type=i.type, grade=i.grade,
                    status=i.status.value if hasattr(i.status, "value") else str(i.status),
                    owner_id=str(i.current_owner_character_id) if i.current_owner_character_id else None,
                    abilities=i.abilities,
                )
                for i in await self.items.list_for_novel(novel_id)
            ],
            abilities=abilities,
            power_realms=realms,
            locations=[
                LocationView(id=str(loc.id), name=loc.name, description=loc.description)
                for loc in await self.locations.list_for_novel(novel_id)
            ],
            plot_threads=[self._thread_director_view(t, chapter_number) for t in threads],
            foreshadowing=[
                self._foreshadow_director_view(f)
                for f in await self.foreshadows.list_unresolved(novel_id)
            ],
            open_plot_debts=[
                self._debt_view(d, chapter_number) for d in await self.debts.list_open(novel_id)
            ],
            hard_constraints=hard,
            soft_constraints=soft,
            author_facts=[
                ContextFact(content=f.statement, source_type="story_fact", source_id=str(f.id))
                for f in facts
            ],
            secret_fact_statements=secret_statements,
        )
        return context

    async def facts_repo_list(self, novel_id):
        from app.repositories.novel import FactRepository

        return await FactRepository(self.session).list_for_novel(novel_id)

    async def build_extraction_context(
        self,
        novel_id: uuid.UUID,
        chapter_number: int,
        chapter_text: str,
        *,
        chapter_plan=None,
        scene_plans=None,
        draft=None,
        draft_hash: str = "",
    ) -> MemoryExtractionContext:
        """Author-level extraction view (Phase 7): current canonical state
        for entity matching + state comparison; draft is the only evidence
        source for changes."""
        await self._novel(novel_id)
        from sqlalchemy import select as sa_select

        from app.models import (
            Ability,
            CharacterKnowledge,
            Item as ItemModel,
            PlotDebt as PlotDebtModel,
            PowerSystem,
            StoryFact,
        )
        from app.repositories.novel import FactRepository
        from app.models.enums import SecrecyLevel
        from app.memory.catalog import EvidenceCatalogBuilder

        all_characters = await self.characters.list_for_novel(novel_id)
        character_states: list[ExtractionCharacterState] = []
        views_by_name = {}
        for character in all_characters:
            view = await self._character_view(character)
            views_by_name[view.name] = view
            state = view.state
            character_states.append(
                ExtractionCharacterState(
                    character_id=view.id,
                    name=view.name,
                    aliases=view.aliases,
                    alive_status=state.alive_status if state else "alive",
                    realm=state.realm if state else None,
                    subrealm=state.subrealm if state else None,
                    location=state.location if state else None,
                    physical_condition=state.physical_condition if state else None,
                    mental_condition=state.mental_condition if state else None,
                )
            )

        facts = await FactRepository(self.session).list_for_novel(novel_id)
        existing_facts = [
            ExtractionFact(
                fact_id=str(f.id),
                statement=f.statement,
                secrecy=f.secrecy.value if hasattr(f.secrecy, "value") else str(f.secrecy),
                reader_visible=(f.revealed_to_reader_chapter is not None)
                or (f.secrecy.value if hasattr(f.secrecy, "value") else str(f.secrecy)) == SecrecyLevel.public.value,
            )
            for f in facts
        ]

        knowledge_rows: list[KnowledgeRowView] = []
        for character in all_characters:
            view = views_by_name[character.name]
            for row in await self.characters.knowledge_of(character.id):
                fact = await self.session.get(StoryFact, row.fact_id)
                if fact is None:
                    continue
                knowledge_rows.append(
                    KnowledgeRowView(
                        character_id=view.id,
                        character_name=view.name,
                        statement=fact.statement,
                        status=row.status.value if hasattr(row.status, "value") else str(row.status),
                    )
                )

        items: list[ExtractionItem] = []
        for item in await self.items.list_for_novel(novel_id):
            items.append(
                ExtractionItem(
                    item_id=str(item.id),
                    name=item.name,
                    aliases=item.aliases,
                    status=item.status.value if hasattr(item.status, "value") else str(item.status),
                    owner_id=str(item.current_owner_character_id) if item.current_owner_character_id else None,
                    location_id=str(item.current_location_id) if item.current_location_id else None,
                )
            )

        abilities: list[str] = []
        for ability in (
            await self.session.execute(sa_select(Ability).where(Ability.novel_id == novel_id))
        ).scalars():
            owner = (
                await self.session.get(Character, ability.owner_character_id)
                if ability.owner_character_id
                else None
            )
            abilities.append(f"{ability.name}|{owner.name if owner else '?'}")

        power_system = (
            await self.session.execute(sa_select(PowerSystem).where(PowerSystem.novel_id == novel_id))
        ).scalars().first()
        realms = [
            r.name for r in await self.power_repo.realms_for(power_system.id)
        ] if power_system else []

        threads = await self.threads.list_threads(novel_id)
        open_debts = await self.debts.list_open(novel_id)
        resolved_ids = [
            str(d.id)
            for d in (
                await self.session.execute(
                    sa_select(PlotDebtModel).where(
                        PlotDebtModel.novel_id == novel_id,
                        PlotDebtModel.status == "fulfilled",
                    )
                )
            ).scalars()
        ]

        temporary_entities: list[str] = []
        if draft is not None:
            temporary_entities = [e.temporary_ref for e in draft.temporary_entities]

        import hashlib

        context = MemoryExtractionContext(
            novel_id=str(novel_id),
            chapter_number=chapter_number,
            chapter_text=chapter_text,
            draft_hash=draft_hash,
            context_hash=hashlib.sha256(f"{novel_id}:{chapter_number}".encode()).hexdigest(),
            character_states=character_states,
            existing_facts=existing_facts,
            fact_catalog=existing_facts,
            evidence_catalog=(
                EvidenceCatalogBuilder.build(draft, draft_hash)
                if draft is not None else None
            ),
            character_knowledge=knowledge_rows,
            items=items,
            abilities=abilities,
            locations=[
                LocationView(id=str(loc.id), name=loc.name, description=loc.description)
                for loc in await self.locations.list_for_novel(novel_id)
            ],
            plot_threads=[self._thread_director_view(t, chapter_number) for t in threads],
            foreshadowing=[
                self._foreshadow_director_view(f)
                for f in await self.foreshadows.list_unresolved(novel_id)
            ],
            open_plot_debts=[self._debt_view(d, chapter_number) for d in open_debts],
            resolved_plot_debt_ids=resolved_ids,
            power_realms=realms,
            temporary_entities=temporary_entities,
            secret_fact_statements=[
                f.statement for f in facts
                if (f.secrecy.value if hasattr(f.secrecy, "value") else str(f.secrecy))
                in ("secret", "top_secret")
            ],
            chapter_plan=chapter_plan,
            scene_plans=scene_plans or [],
            draft=draft,
        )
        return context

    # ------------------------------------------------------------- internals

    async def _all_facts(self):
        from app.repositories.novel import FactRepository

        return await FactRepository(self.session).list_for_novel(await self._current_novel_id())

    async def _novel(self, novel_id: uuid.UUID):
        from app.models import Novel

        novel = await self.session.get(Novel, novel_id)
        if novel is None:
            raise ValueError(f"Novel {novel_id} not found")
        return novel

    async def _current_novel_id(self) -> uuid.UUID:
        return self._current_novel_id_value

    async def _character_view(self, character: Character) -> CharacterView:
        state = await self.characters.current_state(character.id)
        state_view = None
        if state is not None:
            state_view = CharacterStateView(
                chapter_number=state.chapter_number,
                alive_status=state.alive_status.value if hasattr(state.alive_status, "value") else str(state.alive_status),
                realm=state.realm.name if state.realm else None,
                subrealm=state.subrealm,
                location=state.location.name if state.location else None,
                physical_condition=state.physical_condition,
                mental_condition=state.mental_condition,
                current_goals=state.current_goals,
            )
        return CharacterView(
            id=str(character.id),
            name=character.name,
            aliases=character.aliases,
            is_protagonist=character.is_protagonist,
            description=character.description,
            personality_traits=character.personality_traits,
            state=state_view,
        )

    async def _location_views(self, novel_id, location_ids) -> list[LocationView]:
        out = []
        for location_id in location_ids:
            location = await self.session.get(Location, location_id)
            if location is not None:
                out.append(
                    LocationView(id=str(location.id), name=location.name, description=location.description)
                )
        return out

    async def _item_views(self, novel_id, item_ids) -> list[ItemView]:
        out = []
        for item_id in item_ids:
            item = await self.session.get(Item, item_id)
            if item is not None:
                out.append(
                    ItemView(
                        id=str(item.id), name=item.name, type=item.type, grade=item.grade,
                        status=item.status.value if hasattr(item.status, "value") else str(item.status),
                        owner_id=str(item.current_owner_character_id) if item.current_owner_character_id else None,
                        abilities=item.abilities, restrictions=item.restrictions,
                    )
                )
        return out

    async def _relationships_among(self, novel_id, character_ids: set[str]) -> list[RelationshipView]:
        from app.models import Relationship
        from sqlalchemy import select, or_

        if not character_ids:
            return []
        ids = [uuid.UUID(cid) for cid in character_ids]
        result = await self.session.execute(
            select(Relationship).where(
                Relationship.novel_id == novel_id,
                or_(
                    Relationship.source_character_id.in_(ids),
                    Relationship.target_character_id.in_(ids),
                ),
            )
        )
        name_by_id = {v.id: v.name for v in
                      [await self._character_view(c) for c in await self.characters.list_for_novel(novel_id)]}
        out = []
        for rel in result.scalars():
            out.append(
                RelationshipView(
                    source_id=str(rel.source_character_id),
                    source_name=name_by_id.get(str(rel.source_character_id), "?"),
                    target_id=str(rel.target_character_id),
                    target_name=name_by_id.get(str(rel.target_character_id), "?"),
                    type=rel.type,
                    trust=rel.trust,
                    affection=rel.affection,
                    hostility=rel.hostility,
                    debt=rel.debt,
                    public_description=rel.public_description,
                )
            )
        return out

    async def _power_rules(self, visible: list[CharacterView]) -> list[str]:
        rules: list[str] = []
        for view in visible:
            if view.state and view.state.realm:
                sub = f"（{view.state.subrealm}）" if view.state.subrealm else ""
                rules.append(f"{view.name}：{view.state.realm}{sub}")
        return rules

    def _thread_director_view(self, thread: PlotThread, current_chapter: int) -> PlotThreadDirectorView:
        anchor = thread.last_advanced_chapter or thread.introduced_chapter
        since = (current_chapter - anchor) if anchor is not None else None
        return PlotThreadDirectorView(
            id=str(thread.id),
            title=thread.title,
            status=thread.status.value if hasattr(thread.status, "value") else str(thread.status),
            truth=thread.truth,
            public_summary=thread.public_summary,
            priority=thread.priority,
            last_advanced_chapter=thread.last_advanced_chapter,
            chapters_since_last_progress=since,
            target_resolution_chapter=thread.target_resolution_chapter,
        )

    def _debt_view(self, debt: PlotDebt, current_chapter: int) -> PlotDebtView:
        if debt.due_chapter is None:
            urgency = "no_deadline"
        elif debt.due_chapter < current_chapter:
            urgency = "overdue"
        elif debt.due_chapter <= current_chapter + DUE_SOON_WINDOW:
            urgency = "due_soon"
        else:
            urgency = "normal"
        return PlotDebtView(
            id=str(debt.id),
            debt_type=debt.debt_type,
            description=debt.description,
            created_chapter=debt.created_chapter,
            due_chapter=debt.due_chapter,
            due_story_time=debt.due_story_time,
            urgency=urgency,
        )

    @staticmethod
    def _debt_sort_key(debt: PlotDebtView) -> tuple:
        order = {"overdue": 0, "due_soon": 1, "no_deadline": 2, "normal": 3}
        return (order.get(debt.urgency, 9), debt.due_chapter or 10**9, debt.id)

    def _foreshadow_director_view(self, f) -> ForeshadowingDirectorView:
        return ForeshadowingDirectorView(
            id=str(f.id),
            plot_thread_id=str(f.plot_thread_id) if f.plot_thread_id else None,
            setup=f.setup,
            intended_payoff=f.intended_payoff,
            status=f.status.value if hasattr(f.status, "value") else str(f.status),
            planted_chapter=f.planted_chapter,
            payoff_chapter=f.payoff_chapter,
            visibility=f.visibility.value if hasattr(f.visibility, "value") else str(f.visibility),
        )

    async def _constraints_for_chapter(self, novel_id, chapter_number) -> tuple[list[ConstraintView], list[ConstraintView]]:
        hard: list[ConstraintView] = []
        soft: list[ConstraintView] = []
        for c in await self.constraints.list_active(novel_id, chapter_number):
            view = ConstraintView(
                id=str(c.id),
                type=c.type.value if hasattr(c.type, "value") else str(c.type),
                scope=c.scope.value if hasattr(c.scope, "value") else str(c.scope),
                statement=c.statement,
                start_chapter=c.start_chapter,
                end_chapter=c.end_chapter,
            )
            (hard if view.type == "hard" else soft).append(view)
        hard.sort(key=lambda v: v.id)
        soft.sort(key=lambda v: v.id)
        return hard, soft

    async def _retrieve_events(
        self,
        novel_id: uuid.UUID,
        chapter_number: int,
        query_text: str | None,
        linked,
        debug: ContextDebugReport,
        character_ids: list[uuid.UUID] | None = None,
        knowledge_filter: KnowledgeFilter | None = None,
    ) -> list[EventView]:
        retriever = HybridRetriever(StructuredEventRetriever(self.session))
        query = RetrievalQuery(
            novel_id=novel_id,
            query_text=query_text,
            chapter_number=chapter_number,
            character_ids=character_ids if character_ids is not None else linked.character_ids,
            location_ids=linked.location_ids,
            item_ids=linked.item_ids,
            plot_thread_ids=linked.plot_thread_ids,
            top_k=self.budget.max_events,
        )
        memories = await retriever.retrieve(query)
        if knowledge_filter is not None:
            memories = self._firewall_memories(memories, knowledge_filter, debug)
        events = [self._event_view(m) for m in memories]
        scores = {m.source_id: m.final_score for m in memories}
        trimmed = trim_events(events, scores, self.budget)
        for event in trimmed:
            debug.include("event", event.id, event.summary, "hybrid retrieval match")
        return trimmed

    @staticmethod
    def _firewall_memories(memories, kf: KnowledgeFilter, debug: ContextDebugReport):
        """Drop memories explicitly tagged with unknown secret facts before
        they ever become EventViews."""
        kept = []
        for memory in memories:
            tagged = memory.metadata.get("secret_fact_ids") or []
            unknown = kf.unknown_secret_ids(tagged)
            if unknown:
                debug.exclude(
                    "event", memory.source_id, memory.content,
                    f"event tagged with {len(unknown)} unknown secret(s)",
                )
                continue
            kept.append(memory)
        return kept

    @staticmethod
    def _event_view(memory: RetrievedMemory) -> EventView:
        meta = memory.metadata
        return EventView(
            id=memory.source_id,
            chapter_number=memory.chapter_number or 0,
            event_type=meta.get("event_type", "unknown"),
            title=None,
            summary=memory.content,
            participant_ids=meta.get("participant_ids", []),
            location_id=meta.get("location_id"),
            importance=memory.importance_score,
            happened_at_story_time=meta.get("happened_at_story_time"),
        )

    def _firewall_event_text(self, events: list[EventView], kf: KnowledgeFilter, debug: ContextDebugReport) -> list[EventView]:
        """Final-assembly firewall for event text: mask unknown secret
        statements inside summaries (last line of defense)."""
        kept: list[EventView] = []
        for event in events:
            # metadata tagging via RetrievedMemory is lost in EventView; scrub
            # by statement matching (tagging handled at retrieval in Phase 7)
            scrubbed, masked = kf.scrub_text(event.summary)
            if masked:
                if not scrubbed.strip("……（） "):
                    debug.exclude("event", event.id, event.summary, "entire event leaks unknown secret")
                    continue
                event.summary = scrubbed
                debug.include("event", event.id, event.summary, f"masked {len(masked)} unknown secret(s)")
            kept.append(event)
        return kept

    async def _safe_plot_threads(
        self, novel_id, chapter_number, kf: KnowledgeFilter, debug: ContextDebugReport, pov_character_id
    ) -> list[WriterSafePlotThread]:
        out: list[WriterSafePlotThread] = []
        for thread in await self.threads.list_threads(novel_id):
            if thread.status in (ThreadStatus.resolved.value, ThreadStatus.abandoned.value):
                debug.exclude("plot_thread", str(thread.id), thread.title, "resolved/abandoned threads are not writer context")
                continue
            if not thread.public_summary:
                continue
            scrubbed, masked = kf.scrub_text(thread.public_summary)
            if masked and not scrubbed.strip("……（） "):
                debug.exclude("plot_thread", str(thread.id), thread.title, "public summary leaks unknown secret")
                continue
            # truth is structurally absent from WriterSafePlotThread
            if masked:
                debug.include("plot_thread", str(thread.id), thread.title, f"public summary scrubbed ({len(masked)} masked)")
            else:
                debug.include("plot_thread", str(thread.id), thread.title, "public summary safe")
            out.append(
                WriterSafePlotThread(
                    id=str(thread.id),
                    title=thread.title,
                    current_visible_state=scrubbed,
                    hints_allowed=[],
                )
            )
        return out

    async def _safe_foreshadowing(self, novel_id, kf: KnowledgeFilter, debug: ContextDebugReport) -> list[WriterSafeForeshadowing]:
        out: list[WriterSafeForeshadowing] = []
        for f in await self.foreshadows.list_unresolved(novel_id):
            visibility = f.visibility.value if hasattr(f.visibility, "value") else str(f.visibility)
            if visibility != "public":
                debug.exclude("foreshadowing", str(f.id), f.setup, "hidden foreshadowing is not writer context")
                continue
            scrubbed, masked = kf.scrub_text(f.setup)
            if masked and not scrubbed.strip("……（） "):
                debug.exclude("foreshadowing", str(f.id), f.setup, "foreshadowing setup leaks unknown secret")
                continue
            out.append(
                WriterSafeForeshadowing(
                    id=str(f.id),
                    visible_hint=scrubbed,
                    status=f.status.value if hasattr(f.status, "value") else str(f.status),
                )
            )
        return out

    async def _previous_chapter_ending(
        self, novel_id, chapter_number: int, knowledge_filter: KnowledgeFilter | None = None
    ) -> str | None:
        """Tail of the previous chapter, scrubbed through the POV firewall:
        the previous chapter may have been another POV where the reader
        learned something this POV does not know."""
        previous = await self.chapters.latest_for_number(novel_id, chapter_number - 1)
        if previous is None or not previous.body:
            return None
        ending = tail(previous.body, self.budget.previous_chapter_tail_chars)
        if knowledge_filter is not None:
            scrubbed, masked = knowledge_filter.scrub_text(ending)
            if masked and not scrubbed.strip("……（） "):
                return None  # conservative: ending is entirely secret-laden
            return scrubbed
        return ending

    @staticmethod
    def _latest_story_time(events: list[EventView]) -> str | None:
        times = [e.happened_at_story_time for e in events if e.happened_at_story_time]
        return times[0] if times else None

    @staticmethod
    def _any_character_id(characters: list[Character]) -> uuid.UUID:
        if not characters:
            raise ValueError("Novel has no characters; cannot load knowledge filter")
        return characters[0].id
