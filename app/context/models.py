"""Context DTOs: strongly-typed, permission-scoped views for agents.

There is deliberately NO universal ``StoryContext``. Each agent role gets
its own DTO with its own information permissions:

- DirectorContext : planning view; may see thread truths and author secrets
- WriterContext   : information-safe view; POV knowledge firewall applies;
                    author summaries and thread truths are structurally absent
- ContinuityContext / MemoryExtractionContext : defined for Phase 6/7

Every item carries provenance (source_type / source_id / reason) so we can
later answer "why did this fact enter the prompt?"
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field

from app.memory.catalog import EvidenceCatalog
from app.schemas.agent_outputs import SceneBeat, WriterSafeScenePlan


class ContextFact(BaseModel):
    """A single piece of information entering a context, with provenance."""

    content: str
    source_type: str
    source_id: str
    reason: str | None = None


class DebugEntry(BaseModel):
    content: str
    source_type: str
    source_id: str
    reason: str


class ContextDebugReport(BaseModel):
    included: list[DebugEntry] = Field(default_factory=list)
    excluded: list[DebugEntry] = Field(default_factory=list)

    def include(self, source_type: str, source_id: str, content: str, reason: str) -> None:
        self.included.append(
            DebugEntry(source_type=source_type, source_id=source_id, content=content[:200], reason=reason)
        )

    def exclude(self, source_type: str, source_id: str, content: str, reason: str) -> None:
        self.excluded.append(
            DebugEntry(source_type=source_type, source_id=source_id, content=content[:200], reason=reason)
        )


# ------------------------------------------------------------------ views


class CharacterStateView(BaseModel):
    chapter_number: int
    alive_status: str
    realm: str | None = None
    subrealm: str | None = None
    location: str | None = None
    physical_condition: str | None = None
    mental_condition: str | None = None
    current_goals: list[str] = Field(default_factory=list)


class CharacterView(BaseModel):
    id: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    is_protagonist: bool = False
    description: str | None = None
    personality_traits: list[str] = Field(default_factory=list)
    state: CharacterStateView | None = None


class LocationView(BaseModel):
    id: str
    name: str
    description: str | None = None


class ItemView(BaseModel):
    id: str
    name: str
    type: str | None = None
    grade: str | None = None
    status: str
    owner_id: str | None = None
    abilities: list[str] = Field(default_factory=list)
    restrictions: list[str] = Field(default_factory=list)
    # NOTE: secret_fact_ids intentionally absent from every agent view


class RelationshipView(BaseModel):
    source_id: str
    source_name: str
    target_id: str
    target_name: str
    type: str
    trust: int
    affection: int
    hostility: int
    debt: str | None = None
    public_description: str | None = None
    # NOTE: hidden_description never enters any view; Director gets it via
    # DirectorContext.hidden_relationship_notes if planning needs it.


class WriterSafePlotThread(BaseModel):
    id: str
    title: str
    current_visible_state: str
    hints_allowed: list[str] = Field(default_factory=list)


class PlotThreadDirectorView(BaseModel):
    id: str
    title: str
    status: str
    truth: str | None = None
    public_summary: str | None = None
    priority: float
    last_advanced_chapter: int | None = None
    chapters_since_last_progress: int | None = None
    target_resolution_chapter: int | None = None


class WriterSafeForeshadowing(BaseModel):
    id: str
    visible_hint: str
    status: str


class ForeshadowingDirectorView(BaseModel):
    id: str
    plot_thread_id: str | None = None
    setup: str
    intended_payoff: str | None = None
    status: str
    planted_chapter: int | None = None
    payoff_chapter: int | None = None
    visibility: str


class DebtUrgency:
    OVERDUE = "overdue"
    DUE_SOON = "due_soon"
    NORMAL = "normal"
    NO_DEADLINE = "no_deadline"


class PlotDebtView(BaseModel):
    id: str
    debt_type: str | None = None
    description: str
    created_chapter: int
    due_chapter: int | None = None
    due_story_time: str | None = None
    urgency: str  # overdue | due_soon | normal | no_deadline


class ConstraintView(BaseModel):
    id: str
    type: str  # hard | soft
    scope: str
    statement: str
    start_chapter: int | None = None
    end_chapter: int | None = None


class EventView(BaseModel):
    id: str
    chapter_number: int
    event_type: str
    title: str | None = None
    summary: str
    participant_ids: list[str] = Field(default_factory=list)
    location_id: str | None = None
    importance: float
    happened_at_story_time: str | None = None


class ChapterSummaryView(BaseModel):
    """Author-level summary. NEVER enters WriterContext (see knowledge_filter)."""

    chapter_number: int
    short_summary: str


class ArcView(BaseModel):
    id: str
    title: str
    goal: str | None = None
    start_chapter: int
    planned_end_chapter: int | None = None
    major_beats: list[str] = Field(default_factory=list)
    ending_hook: str | None = None
    status: str


class RecentContext(BaseModel):
    previous_chapter_ending: str | None = None
    # author summaries intentionally excluded from writer-safe context
    notes: list[str] = Field(default_factory=list)


# ------------------------------------------------------------------ contexts


class DirectorContext(BaseModel):
    novel_id: str
    chapter_number: int
    current_story_time: str | None = None

    novel_summary: str | None = None  # no summary table yet (Phase 8+)
    current_arc: ArcView | None = None
    recent_chapter_summaries: list[ChapterSummaryView] = Field(default_factory=list)

    active_characters: list[CharacterView] = Field(default_factory=list)
    relevant_characters: list[CharacterView] = Field(default_factory=list)
    relevant_locations: list[LocationView] = Field(default_factory=list)
    relevant_items: list[ItemView] = Field(default_factory=list)

    active_plot_threads: list[PlotThreadDirectorView] = Field(default_factory=list)
    dormant_plot_threads: list[PlotThreadDirectorView] = Field(default_factory=list)
    active_foreshadowing: list[ForeshadowingDirectorView] = Field(default_factory=list)
    open_plot_debts: list[PlotDebtView] = Field(default_factory=list)

    relevant_events: list[EventView] = Field(default_factory=list)

    # director-only: author-level secrets needed for planning
    planning_secrets: list[ContextFact] = Field(default_factory=list)

    hard_constraints: list[ConstraintView] = Field(default_factory=list)
    soft_constraints: list[ConstraintView] = Field(default_factory=list)

    user_direction: str | None = None


class TemporaryEntityBrief(BaseModel):
    """Draft-local temporary entity passed to the writer for consistent
    portrayal (never a canonical character)."""

    temporary_ref: str
    entity_type: str
    name_hint: str | None = None
    role: str | None = None
    introduced_in_scene: str | None = None


class ChapterGuidance(BaseModel):
    """Writer-safe chapter-level guidance (from WriterSafePlan only)."""

    chapter_goal: str | None = None
    chapter_role: str | None = None
    pacing: str | None = None
    emotional_direction: str | None = None
    # truth-free structured restrictions (never raw prohibition texts)
    restrictions: list[str] = Field(default_factory=list)
    expected_state_changes: list[str] = Field(default_factory=list)


class WriterContext(BaseModel):
    novel_id: str
    chapter_number: int
    scene_index: int | None = None
    story_time: str | None = None

    pov_character: CharacterView
    pov_knowledge: list[ContextFact] = Field(default_factory=list)

    location: LocationView | None = None
    visible_characters: list[CharacterView] = Field(default_factory=list)
    relevant_relationships: list[RelationshipView] = Field(default_factory=list)

    relevant_items: list[ItemView] = Field(default_factory=list)
    relevant_power_rules: list[str] = Field(default_factory=list)

    relevant_events: list[EventView] = Field(default_factory=list)

    safe_plot_threads: list[WriterSafePlotThread] = Field(default_factory=list)
    safe_foreshadowing: list[WriterSafeForeshadowing] = Field(default_factory=list)

    hard_constraints: list[ConstraintView] = Field(default_factory=list)
    soft_constraints: list[ConstraintView] = Field(default_factory=list)

    recent_context: RecentContext = Field(default_factory=RecentContext)
    previous_scene_ending: str | None = None

    # writer-safe inputs only: NEVER raw user_direction (author-level)
    writer_instruction: str | None = None
    chapter_guidance: ChapterGuidance | None = None
    safe_scene_plan: "WriterSafeScenePlan | None" = None
    temporary_entities: list[TemporaryEntityBrief] = Field(default_factory=list)


# ------------------------------------------------- Phase 6/7 reserved DTOs


class KnowledgeRowView(BaseModel):
    character_id: str
    character_name: str
    statement: str
    status: str  # known | suspected | believed_false | forgotten


class ContinuityContext(BaseModel):
    """Continuity Checker sees canonical truth plus the chapter under review.

    Author-level by design (§5): includes secret facts, thread truths,
    knowledge rows and the author-level plan. Relevance-filtered: only
    chapter participants / referenced entities, not the whole database."""

    novel_id: str
    chapter_number: int
    story_time: str | None = None
    context_hash: str | None = None

    chapter_text: str
    chapter_plan: object | None = None  # author-level ChapterPlan
    scene_plans: list = Field(default_factory=list)  # author-level ScenePlan list
    draft: object | None = None  # ChapterDraft under review

    canonical_characters: list[CharacterView] = Field(default_factory=list)
    character_knowledge: list[KnowledgeRowView] = Field(default_factory=list)
    items: list[ItemView] = Field(default_factory=list)
    abilities: list[str] = Field(default_factory=list)  # "name|owner_name"
    power_realms: list[str] = Field(default_factory=list)  # "name(rank)"
    locations: list[LocationView] = Field(default_factory=list)
    relationships: list[RelationshipView] = Field(default_factory=list)

    relevant_events: list[EventView] = Field(default_factory=list)
    plot_threads: list[PlotThreadDirectorView] = Field(default_factory=list)
    foreshadowing: list = Field(default_factory=list)
    open_plot_debts: list[PlotDebtView] = Field(default_factory=list)

    hard_constraints: list[ConstraintView] = Field(default_factory=list)
    soft_constraints: list[ConstraintView] = Field(default_factory=list)
    author_facts: list[ContextFact] = Field(default_factory=list)  # incl. secrets
    secret_fact_statements: list[str] = Field(default_factory=list)


class ExtractionCharacterState(BaseModel):
    character_id: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    alive_status: str
    realm: str | None = None
    subrealm: str | None = None
    location: str | None = None
    physical_condition: str | None = None
    mental_condition: str | None = None

    def field_value(self, field: str) -> str | None:
        mapping = {
            "alive_status": self.alive_status,
            "realm": self.realm,
            "location": self.location,
            "physical_condition": self.physical_condition,
            "mental_condition": self.mental_condition,
        }
        return mapping.get(field)


class ExtractionItem(BaseModel):
    item_id: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    status: str
    owner_id: str | None = None
    location_id: str | None = None


class ExtractionFact(BaseModel):
    fact_id: str
    statement: str
    secrecy: str
    reader_visible: bool = False


class MemoryExtractionContext(BaseModel):
    """Memory Extractor sees final chapter text plus current canonical state
    so it proposes DELTAS instead of restating known facts.

    CANONICAL STATE IS REFERENCE, NOT EVIDENCE of what happened in this
    chapter; only draft evidence proves a change."""

    novel_id: str
    chapter_number: int
    chapter_text: str
    draft_hash: str = ""
    context_hash: str = ""

    character_states: list[ExtractionCharacterState] = Field(default_factory=list)
    existing_facts: list[ExtractionFact] = Field(default_factory=list)
    # v3 name makes the prompt contract explicit; kept alongside
    # existing_facts so older callers remain source-compatible.
    fact_catalog: list[ExtractionFact] = Field(default_factory=list)
    evidence_catalog: EvidenceCatalog | None = None
    character_knowledge: list[KnowledgeRowView] = Field(default_factory=list)
    items: list[ExtractionItem] = Field(default_factory=list)
    abilities: list[str] = Field(default_factory=list)
    locations: list[LocationView] = Field(default_factory=list)
    relationships: list[RelationshipView] = Field(default_factory=list)

    plot_threads: list[PlotThreadDirectorView] = Field(default_factory=list)
    foreshadowing: list = Field(default_factory=list)
    open_plot_debts: list[PlotDebtView] = Field(default_factory=list)
    resolved_plot_debt_ids: list[str] = Field(default_factory=list)

    power_realms: list[str] = Field(default_factory=list)
    temporary_entities: list[str] = Field(default_factory=list)
    secret_fact_statements: list[str] = Field(default_factory=list)

    chapter_plan: object | None = None
    scene_plans: list = Field(default_factory=list)
    draft: object | None = None

    def known_entity_ids(self) -> dict[str, set[str]]:
        return {
            "character": {c.character_id for c in self.character_states},
            "item": {i.item_id for i in self.items},
            "fact": {f.fact_id for f in self.existing_facts},
            "location": {loc.id for loc in self.locations},
            "plot_thread": {t.id for t in self.plot_threads},
            "foreshadowing": {f.id for f in self.foreshadowing if hasattr(f, "id")},
            "plot_debt": {d.id for d in self.open_plot_debts} | set(self.resolved_plot_debt_ids),
        }
