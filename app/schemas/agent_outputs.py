"""Pydantic models for agent structured outputs.

These mirror the JSON Schemas in ``schemas/`` exactly (they are the
canonical contract from the agent pack):

- ``schemas/chapter_plan.schema.json``     -> ChapterPlan
- ``schemas/memory_update.schema.json``    -> MemoryUpdateBatch
- ``schemas/continuity_report.schema.json`` -> ContinuityReport

If a future domain need requires extra fields, update the JSON Schema file
and this model together so prompt-time contract and validation stay in sync.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.models.enums import Severity


# ------------------------------------------------------------------ director

SceneBeatNarrativeMode = str  # normal | flashback | illusion | corpse | remnant


class SceneBeat(BaseModel):
    """Coarse scene beat produced by the Director. Full ScenePlan (POV,
    transitions, expected length) belongs to the Phase 5 Scene Planner."""

    index: int = Field(ge=1)
    goal: str
    participants: list[str] = Field(default_factory=list)
    location: str
    conflict: str | None = None
    beats: list[str] = Field(default_factory=list)
    required_facts: list[str] = Field(default_factory=list)
    forbidden_reveals: list[str] = Field(default_factory=list)
    # required when a dead character appears (flashback/illusion/corpse/remnant)
    narrative_mode: str | None = Field(default=None)


class EntityRef(BaseModel):
    """ID-first entity reference; ``temporary_ref`` for proposed new entities."""

    character_id: str | None = None
    temporary_ref: str | None = None
    name: str | None = None


class ProposedEntity(BaseModel):
    """A new canonical entity the Director plans to introduce. Scene beats
    reference it via ``temporary_ref`` — the LLM must never invent UUIDs."""

    entity_type: str  # character | location | item | faction
    temporary_ref: str
    name_hint: str | None = None
    role: str | None = None
    canonicalize: bool = True


class PlotThreadInstruction(BaseModel):
    plot_thread_id: str | None = None
    title: str | None = None
    action: str  # introduce | advance | echo | misdirect | resolve
    instruction: str


class PlotDebtInstruction(BaseModel):
    plot_debt_id: str | None = None
    description: str | None = None
    instruction: str


class ForeshadowingInstruction(BaseModel):
    foreshadowing_id: str | None = None
    action: str  # introduce | advance | echo | misdirect | resolve
    instruction: str


class RevelationInstruction(BaseModel):
    """Explicit reveal policy for one fact. ``hidden`` facts never appear
    here; policies constrain what Phase 5 Writer may write."""

    fact_id: str | None = None
    statement: str | None = None
    policy: str  # hint_only | partial | full  (never "hidden": use prohibited)
    allowed_information: str | None = None
    forbidden_information: str | None = None


class ProhibitedRevelation(BaseModel):
    statement: str
    fact_id: str | None = None
    reason: str | None = None


class CharacterGoal(BaseModel):
    character_id: str | None = None
    temporary_ref: str | None = None
    goal: str


class StateExpectation(BaseModel):
    character_id: str | None = None
    expectation: str


class DirectorOnlyNotes(BaseModel):
    """Author-level planning metadata. MUST NEVER be projected into any
    Writer-visible view (see ChapterPlan.to_writer_safe)."""

    strategic_rationale: str | None = None
    secret_context: list[str] = Field(default_factory=list)
    follow_up_seeds: list[str] = Field(default_factory=list)


class ChapterPlan(BaseModel):
    """Structured plan of "what happens in this chapter" — never prose.

    The plan itself is part of the information firewall: ``director_notes``
    and planning secrets stay out of the writer-safe projection.
    """

    chapter_number: int | None = None
    chapter_goal: str
    chapter_role: str | None = None  # setup|exploration|conflict|revelation|transition|climax|aftermath
    emotional_direction: str | None = None
    pacing: str | None = None  # slow|balanced|fast|climactic

    participating_characters: list[EntityRef] = Field(default_factory=list)
    primary_locations: list[EntityRef] = Field(default_factory=list)
    proposed_entities: list[ProposedEntity] = Field(default_factory=list)

    required_events: list[str] = Field(default_factory=list)
    scenes: list[SceneBeat] = Field(min_length=1, max_length=8)

    plot_threads_to_progress: list[PlotThreadInstruction] = Field(default_factory=list)
    plot_debts_to_progress: list[PlotDebtInstruction] = Field(default_factory=list)
    foreshadowing_actions: list[ForeshadowingInstruction] = Field(default_factory=list)

    revelations: list[RevelationInstruction] = Field(default_factory=list)
    prohibited_revelations: list[ProhibitedRevelation] = Field(default_factory=list)

    character_goals: list[CharacterGoal] = Field(default_factory=list)
    character_state_expectations: list[StateExpectation] = Field(default_factory=list)

    must_not_reveal: list[str] = Field(default_factory=list)
    hard_constraints: list[str] = Field(default_factory=list)
    soft_preferences: list[str] = Field(default_factory=list)
    expected_state_changes: list[str] = Field(default_factory=list)

    ending_hook: str
    conflicts: list[str] = Field(default_factory=list)

    director_notes: DirectorOnlyNotes | None = None

    def to_writer_safe(self) -> "WriterSafePlan":
        return writer_safe_view(self)


class WriterSafeRevelation(BaseModel):
    """Writer-safe reveal instruction: the forbidden truth is replaced by a
    generic restriction; the secret itself never crosses the firewall."""

    policy: str
    allowed_information: str | None = None
    restriction: str | None = None


class WriterSafeRestriction(BaseModel):
    """Structured, truth-free prohibition for the Writer.

    Carries an opaque topic reference and a generic instruction; the secret
    itself NEVER crosses into the writer-safe layer."""

    topic_ref: str | None = None
    instruction: str


class WriterSafePlan(BaseModel):
    """The ONLY plan projection Phase 5 Writer may ever receive.

    Excluded by construction: director_notes, thread truths, planning
    secrets, and RAW prohibition texts (must_not_reveal /
    prohibited_revelations become truth-free structured restrictions).
    """

    chapter_number: int | None = None
    chapter_goal: str
    chapter_role: str | None = None
    emotional_direction: str | None = None
    pacing: str | None = None
    participating_characters: list[str] = Field(default_factory=list)
    primary_locations: list[str] = Field(default_factory=list)
    proposed_entities: list[ProposedEntity] = Field(default_factory=list)
    required_events: list[str] = Field(default_factory=list)
    scenes: list[SceneBeat] = Field(default_factory=list)
    plot_threads_to_progress: list[PlotThreadInstruction] = Field(default_factory=list)
    plot_debts_to_progress: list[PlotDebtInstruction] = Field(default_factory=list)
    foreshadowing_actions: list[ForeshadowingInstruction] = Field(default_factory=list)
    revelations: list[WriterSafeRevelation] = Field(default_factory=list)
    restrictions: list[WriterSafeRestriction] = Field(default_factory=list)
    character_goals: list[CharacterGoal] = Field(default_factory=list)
    hard_constraints: list[str] = Field(default_factory=list)
    soft_preferences: list[str] = Field(default_factory=list)
    expected_state_changes: list[str] = Field(default_factory=list)
    ending_hook: str

    def participant_names(self) -> list[str]:
        return list(self.participating_characters)

    def location_name(self) -> str | None:
        return self.primary_locations[0] if self.primary_locations else None


_RESTRICTION_BY_POLICY = {
    "hint_only": "只能轻微暗示，不得给出任何确定结论",
    "partial": "只能呈现允许的部分信息，不得扩大到完整真相",
    "full": "本章允许正式揭露，但不得超出允许范围",
}


_RESTRICTION_TOPICS = {
    "hint_only": "该谜团只能提供模糊暗示，不得写成确定结论。",
    "partial": "该谜团只能呈现被允许的部分信息，不得扩大到完整真相。",
    "full": "该信息本章允许揭露，但不得超出允许范围。",
}
_RESTRICTION_GENERIC = "存在受限信息：不得将其写成确定结论，只能保持悬念。"


def _build_restrictions(plan: ChapterPlan) -> list[WriterSafeRestriction]:
    """Convert author-level prohibitions into truth-free writer instructions."""
    restrictions: list[WriterSafeRestriction] = []
    for revelation in plan.revelations:
        restrictions.append(
            WriterSafeRestriction(
                topic_ref=revelation.fact_id,
                instruction=_RESTRICTION_TOPICS.get(
                    revelation.policy, _RESTRICTION_GENERIC
                ),
            )
        )
    for prohibited in plan.prohibited_revelations:
        restrictions.append(
            WriterSafeRestriction(
                topic_ref=prohibited.fact_id,
                instruction=_RESTRICTION_GENERIC,
            )
        )
    for _ in plan.must_not_reveal:
        # raw statement intentionally discarded; only the count matters
        restrictions.append(WriterSafeRestriction(instruction=_RESTRICTION_GENERIC))
    return restrictions


def writer_safe_view(plan: ChapterPlan) -> WriterSafePlan:
    return WriterSafePlan(
        chapter_number=plan.chapter_number,
        chapter_goal=plan.chapter_goal,
        chapter_role=plan.chapter_role,
        emotional_direction=plan.emotional_direction,
        pacing=plan.pacing,
        participating_characters=[
            ref.name or ref.temporary_ref or ref.character_id or "?"
            for ref in plan.participating_characters
        ],
        primary_locations=[ref.name or ref.temporary_ref or "?" for ref in plan.primary_locations],
        proposed_entities=plan.proposed_entities,
        required_events=plan.required_events,
        scenes=[
            scene.model_copy(update={"forbidden_reveals": []})
            for scene in plan.scenes
        ],
        plot_threads_to_progress=plan.plot_threads_to_progress,
        plot_debts_to_progress=plan.plot_debts_to_progress,
        foreshadowing_actions=plan.foreshadowing_actions,
        revelations=[
            WriterSafeRevelation(
                policy=r.policy,
                allowed_information=r.allowed_information,
                restriction=_RESTRICTION_BY_POLICY.get(r.policy, "遵守既定 reveal 政策"),
            )
            for r in plan.revelations
        ],
        restrictions=_build_restrictions(plan),
        character_goals=plan.character_goals,
        hard_constraints=plan.hard_constraints,
        soft_preferences=plan.soft_preferences,
        expected_state_changes=plan.expected_state_changes,
        ending_hook=plan.ending_hook,
    )




# ------------------------------------------------------------- scene planner


class ScenePlan(BaseModel):
    """Executable per-scene plan expanded from chapter beats by the Scene
    Planner. The planner only ever sees the WriterSafePlan, so a ScenePlan
    is author-safe by construction — planner_notes aside."""

    scene_id: str
    scene_index: int = Field(ge=1)
    source_beat_ids: list[str] = Field(default_factory=list)
    pov: EntityRef
    location: EntityRef | None = None
    participants: list[EntityRef] = Field(default_factory=list)
    scene_goal: str
    conflict: str | None = None
    opening_state: str | None = None
    required_actions: list[str] = Field(default_factory=list)
    required_outcomes: list[str] = Field(default_factory=list)
    allowed_information: list[str] = Field(default_factory=list)
    allowed_hints: list[str] = Field(default_factory=list)
    emotional_target: str | None = None
    pacing: str | None = None
    transition_in: str | None = None
    transition_out: str | None = None
    target_chars: int = Field(default=800, ge=100, le=5000)
    temporary_entities: list[str] = Field(default_factory=list)
    planner_notes: list[str] = Field(default_factory=list)
    # narrative context used by the continuity checker (dead-character rule)
    narrative_mode: str | None = None  # normal | flashback | illusion | corpse | remnant
    story_time: str | None = None

    def to_writer_safe(self) -> "WriterSafeScenePlan":
        return WriterSafeScenePlan(
            scene_id=self.scene_id,
            scene_index=self.scene_index,
            pov=self.pov,
            location=self.location,
            participants=self.participants,
            scene_goal=self.scene_goal,
            conflict=self.conflict,
            opening_state=self.opening_state,
            required_actions=self.required_actions,
            required_outcomes=self.required_outcomes,
            allowed_information=self.allowed_information,
            allowed_hints=self.allowed_hints,
            emotional_target=self.emotional_target,
            pacing=self.pacing,
            transition_in=self.transition_in,
            transition_out=self.transition_out,
            target_chars=self.target_chars,
            temporary_entities=self.temporary_entities,
        )


class WriterSafeScenePlan(BaseModel):
    """The ONLY scene-level plan the Writer may receive. Type boundary:
    future planner-only metadata cannot accidentally leak through it."""

    scene_id: str
    scene_index: int
    pov: EntityRef
    location: EntityRef | None = None
    participants: list[EntityRef] = Field(default_factory=list)
    scene_goal: str
    conflict: str | None = None
    opening_state: str | None = None
    required_actions: list[str] = Field(default_factory=list)
    required_outcomes: list[str] = Field(default_factory=list)
    allowed_information: list[str] = Field(default_factory=list)
    allowed_hints: list[str] = Field(default_factory=list)
    emotional_target: str | None = None
    pacing: str | None = None
    transition_in: str | None = None
    transition_out: str | None = None
    target_chars: int
    temporary_entities: list[str] = Field(default_factory=list)

    def participant_names(self) -> list[str]:
        return [
            ref.name or ref.temporary_ref or ref.character_id or "?"
            for ref in self.participants
        ]

    def pov_name(self) -> str:
        return self.pov.name or self.pov.temporary_ref or self.pov.character_id or "?"

    def location_name(self) -> str | None:
        if self.location is None:
            return None
        return self.location.name or self.location.temporary_ref


class ScenePlanBatch(BaseModel):
    chapter_number: int | None = None
    scenes: list[ScenePlan] = Field(min_length=1, max_length=12)
    planner_notes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------- memory extractor
# The memory contracts evolved to the envelope model (Phase 7); kept here as
# re-exports so existing imports keep working.

from app.memory.models import (  # noqa: E402,F401
    EvidenceRef,
    EvidenceSpan,
    FactRef,
    MemoryUpdateBatch,
    MemoryUpdateOperation,
    MemoryUpdateType,
    MemoryVisibility,
)

# -------------------------------------------------------- continuity checker


class ContinuityIssue(BaseModel):
    type: str
    severity: Severity
    affected_scene: int | None = None
    evidence_text: str
    canonical_fact: str | None = None
    explanation: str
    suggested_fix: str


class ContinuityReport(BaseModel):
    passed: bool
    issues: list[ContinuityIssue] = Field(default_factory=list)

    @property
    def has_critical(self) -> bool:
        return any(issue.severity == Severity.critical for issue in self.issues)

    def by_severity(self) -> dict[str, list[ContinuityIssue]]:
        grouped: dict[str, list[ContinuityIssue]] = {}
        for issue in self.issues:
            grouped.setdefault(issue.severity.value, []).append(issue)
        return grouped
