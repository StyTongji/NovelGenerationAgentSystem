"""Planning layer models: validation report, generation result, provenance."""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.llm.models import GenerationMetadata
from app.models.enums import Severity
from app.schemas.agent_outputs import ChapterPlan, ScenePlanBatch, WriterSafePlan


class PlanIssue(BaseModel):
    code: str
    severity: Severity
    message: str
    entity_type: str | None = None
    entity_id: str | None = None
    evidence: list[str] = Field(default_factory=list)
    suggested_fix: str | None = None


class PlanValidationReport(BaseModel):
    valid: bool
    issues: list[PlanIssue] = Field(default_factory=list)

    def errors(self) -> list[PlanIssue]:
        return [i for i in self.issues if i.severity in (Severity.error, Severity.critical)]

    def warnings(self) -> list[PlanIssue]:
        return [i for i in self.issues if i.severity == Severity.warning]

    def criticals(self) -> list[PlanIssue]:
        return [i for i in self.issues if i.severity == Severity.critical]

    @property
    def has_blocking(self) -> bool:
        return bool(self.errors())


class PlanGenerationResult(BaseModel):
    """Preview result of plan generation. Never commits anything.

    ``plan`` holds a ChapterPlan (Director) or ScenePlanBatch (ScenePlanner).
    """

    plan: ChapterPlan | ScenePlanBatch
    writer_safe_plan: WriterSafePlan
    validation_report: PlanValidationReport
    metadata: GenerationMetadata | None = None
    repair_count: int = 0
    # provenance: which story state version produced this plan
    context_hash: str = ""
    based_on_chapter_number: int = 0
    prompt_version: str = "director_v2"
