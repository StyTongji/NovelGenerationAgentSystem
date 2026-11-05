"""Continuity models: issue contract, report, repair projection DTOs.

The report schema (``schemas/continuity_report.schema.json``) is generated
from these models. ``ContinuityIssue`` deliberately has two permission
layers:

- ``author_explanation``  — checker-level, may reference secret truth
- writer-safe projection  — ``app/continuity/repair_projection.py`` builds a
  ``WriterSafeRepairInstruction`` that never carries the explanation
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

from app.llm.models import GenerationMetadata
from app.models.enums import Severity
from app.schemas.agent_outputs import EntityRef


class ContinuityCategory(str, Enum):
    dead_character = "dead_character"
    item_ownership = "item_ownership"
    ability_ownership = "ability_ownership"
    realm_state = "realm_state"
    illegal_breakthrough = "illegal_breakthrough"
    hard_constraint = "hard_constraint"
    knowledge_leak = "knowledge_leak"
    reveal_policy = "reveal_policy"
    plot_thread_state = "plot_thread_state"
    proposed_entity_identity = "proposed_entity_identity"
    timeline = "timeline"
    location_travel = "location_travel"
    scene_continuity = "scene_continuity"
    plan_adherence = "plan_adherence"
    personality_drift = "personality_drift"
    other = "other"


BLOCKING_SEVERITIES = (Severity.error, Severity.critical)


class ContinuityIssue(BaseModel):
    issue_id: str
    category: ContinuityCategory
    severity: Severity
    scene_id: str | None = None
    entity_refs: list[EntityRef] = Field(default_factory=list)
    evidence_excerpt: str | None = None
    # checker-level explanation; may reference canonical secrets. NEVER send
    # this to a writer-facing agent.
    author_explanation: str
    suggested_action: str | None = None
    source: Literal["deterministic", "llm"]
    blocking: bool = False

    def merge_key(self) -> tuple:
        """Grouping key: category + scene + primary entity + normalized
        evidence region. Distinct problems in one scene (e.g. two different
        leaked secrets) keep distinct keys; the merger adds a conservative
        evidence-overlap fallback for ref-less LLM findings."""
        primary = ""
        for ref in self.entity_refs:
            primary = ref.character_id or ref.temporary_ref or ref.name or ""
            if primary:
                break
        evidence_head = re.sub(r"[\s，。、！？：；,.!?;:]+", "", (self.evidence_excerpt or ""))[:40]
        return (self.category.value, self.scene_id, primary, evidence_head)


class ContinuityReport(BaseModel):
    valid: bool  # no blocking issue
    issues: list[ContinuityIssue] = Field(default_factory=list)
    highest_severity: Severity | None = None
    blocking_issue_count: int = 0
    deterministic_issue_count: int = 0
    llm_issue_count: int = 0
    checked_scene_ids: list[str] = Field(default_factory=list)
    context_hash: str | None = None
    draft_hash: str = ""

    def blocking_issues(self) -> list[ContinuityIssue]:
        return [i for i in self.issues if i.blocking]


class ContinuityRepairInstruction(BaseModel):
    """Writer-safe repair instruction (§25). Structurally excludes
    author_explanation / secret truth / director notes."""

    issue_id: str
    scene_id: str
    category: str
    instruction: str
    preserve: list[str] = Field(default_factory=list)
    allowed_information: list[str] = Field(default_factory=list)


class ContinuityRepairRecord(BaseModel):
    round_index: int
    scene_ids_repaired: list[str]
    issues_before: int
    issues_after: int
    metadata: list[GenerationMetadata] = Field(default_factory=list)


class ContinuityRepairResult(BaseModel):
    draft: object  # ChapterDraft (avoid circular import; validated by service)
    report: ContinuityReport
    repair_history: list[ContinuityRepairRecord] = Field(default_factory=list)
    exhausted: bool = False
    generation_metadata: list[GenerationMetadata] = Field(default_factory=list)
