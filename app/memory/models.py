"""Memory extraction contracts (Phase 7/8.7, v3).

Every canonical mutation candidate carries a uniform envelope
(:class:`MemoryUpdateOperation`) with an :class:`EvidenceSpan` list pointing
into the ChapterDraft. `confidence` is UI metadata only — it never skips
validation.

Plan text / canonical context are REFERENCE for entity matching; only draft
evidence proves a change happened in this chapter.
"""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.enums import SecrecyLevel


class MemoryUpdateType(str, Enum):
    create_event = "create_event"
    character_state_update = "character_state_update"
    character_knowledge_update = "character_knowledge_update"
    relationship_update = "relationship_update"
    item_update = "item_update"
    story_fact_create = "story_fact_create"
    plot_thread_update = "plot_thread_update"
    foreshadowing_update = "foreshadowing_update"
    plot_debt_update = "plot_debt_update"
    propose_entity = "propose_entity"


class EvidenceSpan(BaseModel):
    # Server-resolved catalog id.  exact_quote remains for provenance and
    # backwards compatibility with v2/user-edited batches.
    evidence_id: str | None = None
    scene_id: str
    exact_quote: str
    start_char: int | None = None
    end_char: int | None = None
    reason: str | None = None


class EvidenceRef(BaseModel):
    evidence_id: str
    reason: str | None = None


class FactRef(BaseModel):
    """Exactly one canonical fact id or batch-local temporary fact ref."""

    fact_id: str | None = None
    temporary_fact_ref: str | None = None

    @model_validator(mode="after")
    def exclusive(self):
        if not self.valid or self.fact_id == "" or self.temporary_fact_ref == "":
            raise ValueError("exactly one nonempty fact_id or temporary_fact_ref is required")
        return self

    @property
    def valid(self) -> bool:
        return (self.fact_id is None) != (self.temporary_fact_ref is None)


class MemoryVisibility(BaseModel):
    """Structured visibility provenance (Phase 3 filter-before-serialization)."""

    reader_visible: bool = True
    known_by_character_ids: list[str] = Field(default_factory=list)
    suspected_by_character_ids: list[str] = Field(default_factory=list)
    secret_fact_ids: list[str] = Field(default_factory=list)
    author_only: bool = False


class EventCandidate(BaseModel):
    event_type: str
    title: str | None = None
    author_summary: str
    reader_safe_summary: str
    participant_ids: list[str] = Field(default_factory=list)
    participant_temporary_refs: list[str] = Field(default_factory=list)
    location_id: str | None = None
    happened_at_story_time: str | None = None
    importance: float = Field(default=0.5, ge=0, le=1)
    visibility: MemoryVisibility = Field(default_factory=MemoryVisibility)


CANONICAL_CHARACTER_STATE_FIELDS = {"alive_status", "physical_condition", "location", "realm", "mental_condition"}


class CharacterStateChange(BaseModel):
    character_id: str
    field: str  # alive_status | physical_condition | location | realm | mental_condition
    expected_old_value: str | None = None
    proposed_new_value: str
    new_value_description: str | None = None  # free text (injury etc.)
    requires_confirmation: bool = False


class KnowledgeChange(BaseModel):
    character_id: str
    fact_ref: FactRef | None = None
    fact_id: str | None = None
    temporary_fact_ref: str | None = None
    # statement must match an existing fact or a story_fact_create in the
    # same batch; it can never invent world truth by itself
    fact_statement: str | None = None
    status: Literal["known", "suspected", "believed_false", "forgotten"]
    belief_confidence: float | None = Field(default=None, ge=0, le=1)


class RelationshipChange(BaseModel):
    source_character_id: str
    target_character_id: str
    type: str
    note: str | None = None  # descriptive; NO numeric trust deltas from the LLM


class ItemChange(BaseModel):
    item_id: str | None = None
    temporary_item_ref: str | None = None
    change: Literal["owner_transfer", "location_change", "status_change", "consumed"]
    expected_old_owner_id: str | None = None
    new_owner_id: str | None = None
    new_location_id: str | None = None
    new_status: str | None = Field(default=None, description=(
        "For status_change only: intact, damaged, destroyed, lost, sealed or dormant. "
        "Opening, unsealing and other narrative events belong in events or facts."))

    @property
    def reference_key(self) -> str:
        return self.item_id or self.temporary_item_ref or ""


class StoryFactCandidate(BaseModel):
    statement: str
    temporary_fact_ref: str | None = None
    secrecy: SecrecyLevel = SecrecyLevel.public
    reader_visible: bool = True
    category: str | None = None
    visibility: MemoryVisibility = Field(default_factory=MemoryVisibility)


class PlotThreadChange(BaseModel):
    plot_thread_id: str
    action: Literal["introduced", "advanced", "echoed", "resolved"]
    note: str | None = None


class ForeshadowChange(BaseModel):
    foreshadowing_id: str | None = None
    action: Literal["introduced", "echoed", "advanced", "resolved"]
    setup: str | None = None  # for introduced
    note: str | None = None


class PlotDebtChange(BaseModel):
    plot_debt_id: str | None = None
    action: Literal["created", "progressed", "resolved"]
    description: str | None = None
    due_chapter: int | None = None


class ProposedCanonicalEntity(BaseModel):
    """A temporary entity worth canonicalizing. NO UUID, NO INSERT here —
    Phase 8 canonicalization creates the real rows."""

    temporary_ref: str
    entity_type: str
    proposed_name: str | None = None
    attributes: dict[str, str] = Field(default_factory=dict)


class MemoryUpdateOperation(BaseModel):
    operation_id: str
    update_type: MemoryUpdateType
    source_scene_id: str
    evidence: list[EvidenceSpan] = Field(default_factory=list)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0, le=1)  # UI/sort only
    planned: bool | None = None
    unplanned_change: bool = False

    event: EventCandidate | None = None
    character_state: CharacterStateChange | None = None
    knowledge: KnowledgeChange | None = None
    relationship: RelationshipChange | None = None
    item: ItemChange | None = None
    story_fact: StoryFactCandidate | None = None
    plot_thread: PlotThreadChange | None = None
    foreshadowing: ForeshadowChange | None = None
    plot_debt: PlotDebtChange | None = None
    proposed_entity: ProposedCanonicalEntity | None = None

    _PAYLOAD_FIELD = {
        MemoryUpdateType.create_event: "event",
        MemoryUpdateType.character_state_update: "character_state",
        MemoryUpdateType.character_knowledge_update: "knowledge",
        MemoryUpdateType.relationship_update: "relationship",
        MemoryUpdateType.item_update: "item",
        MemoryUpdateType.story_fact_create: "story_fact",
        MemoryUpdateType.plot_thread_update: "plot_thread",
        MemoryUpdateType.foreshadowing_update: "foreshadowing",
        MemoryUpdateType.plot_debt_update: "plot_debt",
        MemoryUpdateType.propose_entity: "proposed_entity",
    }

    @property
    def payload(self):
        return getattr(self, self._PAYLOAD_FIELD[self.update_type])

    def target_id(self) -> str:
        payload = self.payload
        for attr in (
            "character_id", "item_id", "temporary_item_ref", "plot_thread_id", "foreshadowing_id",
            "plot_debt_id", "source_character_id", "temporary_ref",
        ):
            value = getattr(payload, attr, None)
            if value:
                return value
        if self.update_type == MemoryUpdateType.story_fact_create:
            return self.story_fact.statement  # type: ignore[union-attr]
        return ""

    def fingerprint(self, novel_id: str, draft_hash: str) -> str:
        material = json.dumps(
            {
                "novel_id": novel_id,
                "draft_hash": draft_hash,
                "update_type": self.update_type.value,
                "target_id": self.target_id(),
                "source_scene_id": self.source_scene_id,
                "evidence": [e.exact_quote for e in self.evidence],
                "payload": json.loads(self.payload.model_dump_json(exclude_none=True)),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()


class MemoryUpdateBatch(BaseModel):
    contract_version: Literal[2, 3] = 2  # historical replay only; new output is v3
    operations: list[MemoryUpdateOperation] = Field(default_factory=list)
    candidate_author_summary: str | None = None
    candidate_reader_summary: str | None = None


class KnowledgeChangeV3(BaseModel):
    model_config = ConfigDict(extra="forbid")
    character_id: str
    fact_ref: FactRef
    status: Literal["known", "suspected", "believed_false", "forgotten"]
    belief_confidence: float | None = Field(default=None, ge=0, le=1)


class CharacterStateChangeV3(CharacterStateChange):
    field: Literal["alive_status", "physical_condition", "location", "realm", "mental_condition"]


class StoryFactCandidateV3(StoryFactCandidate):
    temporary_fact_ref: str = Field(pattern=r"^new_fact_[1-9][0-9]*$")


class ItemChangeV3(ItemChange):
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def exclusive(self):
        if (self.item_id is None) == (self.temporary_item_ref is None) or self.item_id == "" or self.temporary_item_ref == "":
            raise ValueError("exactly one nonempty item_id or temporary_item_ref is required")
        return self


class MemoryUpdateOperationV3(MemoryUpdateOperation):
    """LLM wire schema. Resolved spans belong to the server result only."""
    model_config = ConfigDict(extra="forbid")
    evidence_refs: list[EvidenceRef] = Field(min_length=1)
    evidence: list[EvidenceSpan] = Field(default_factory=list, max_length=0)
    knowledge: KnowledgeChangeV3 | None = None
    character_state: CharacterStateChangeV3 | None = None
    story_fact: StoryFactCandidateV3 | None = None
    item: ItemChangeV3 | None = None

    @model_validator(mode="after")
    def one_payload(self):
        fields = list(self._PAYLOAD_FIELD.values())
        if self.payload is None or sum(getattr(self, key) is not None for key in fields) != 1:
            raise ValueError("exactly one payload matching update_type is required")
        return self


class MemoryUpdateBatchV3(MemoryUpdateBatch):
    model_config = ConfigDict(extra="forbid")
    contract_version: Literal[3] = 3
    operations: list[MemoryUpdateOperationV3] = Field(default_factory=list)
