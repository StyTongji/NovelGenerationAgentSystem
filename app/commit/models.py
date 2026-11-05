"""Canonical commit contracts: request, result, preview package, errors.

Only :class:`app.services.canonical_commit.CanonicalCommitService` may
mutate canonical state. Everything here is a DTO.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

from app.continuity.models import ContinuityReport
from app.draft.models import ChapterDraft
from app.memory.models import MemoryUpdateBatch
from app.memory.validator import MemoryValidationReport
from app.planning.models import PlanGenerationResult
from app.schemas.agent_outputs import ChapterPlan
from app.services.memory_extraction import MemoryExtractionResult


class CommitValidationCode(str, Enum):
    stale_state = "stale_state"
    draft_hash_mismatch = "draft_hash_mismatch"
    plan_hash_mismatch = "plan_hash_mismatch"
    continuity_invalid = "continuity_invalid"
    memory_invalid = "memory_invalid"
    dry_run_stale = "dry_run_stale"
    chapter_number_collision = "chapter_number_collision"
    duplicate_commit = "duplicate_commit"
    preview_tampered = "preview_tampered"
    precondition_failed = "precondition_failed"


class CommitError(Exception):
    def __init__(self, code: CommitValidationCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class StaleStoryStateError(CommitError):
    def __init__(self, expected: str, current: str) -> None:
        super().__init__(
            CommitValidationCode.stale_state,
            f"Story state moved: expected {expected[:12]}..., current {current[:12]}...; "
            "regenerate the preview before committing",
        )
        self.expected = expected
        self.current = current


class DuplicateCommitError(CommitError):
    def __init__(self, message: str) -> None:
        super().__init__(CommitValidationCode.duplicate_commit, message)


class ChapterNumberCollisionError(CommitError):
    def __init__(self, requested: int, current: int) -> None:
        super().__init__(
            CommitValidationCode.chapter_number_collision,
            f"chapter_number {requested} != current_chapter_number + 1 ({current + 1})",
        )


class PreviewPackageTamperedError(CommitError):
    def __init__(self, submitted: str, recomputed: str) -> None:
        super().__init__(
            CommitValidationCode.preview_tampered,
            f"preview_package_hash mismatch: submitted {submitted[:12]}... != "
            f"recomputed {recomputed[:12]}...; the preview package was modified",
        )
        self.submitted = submitted
        self.recomputed = recomputed


class ReviewApproval(BaseModel):
    """Local review receipt, NOT an authentication credential.

    Automated test receipts require an explicitly opted-in isolated caller;
    normal application commit paths accept human receipts only.
    """
    attempt_id: str
    reviewed_by: str = Field(min_length=1)
    approved: bool = False
    draft_hash: str
    memory_hash: str
    preview_package_hash: str
    operation_reviews: dict[str, Literal["correct", "missing", "wrong", "over-extracted"]] = Field(default_factory=dict)
    missing: list[str] = Field(default_factory=list)
    quality_scores: dict[str, int] = Field(default_factory=dict)
    semantic_review_complete: bool = False
    assisted_review_hash: str | None = None
    review_kind: Literal["human", "automated_test"] = "human"

    @staticmethod
    def hash_memory(memory: MemoryExtractionResult) -> str:
        return hashlib.sha256(json.dumps(
            memory.model_dump(mode="json"), sort_keys=True, ensure_ascii=False,
        ).encode()).hexdigest()

    def verify(self, request) -> None:
        memory = request.memory_result
        required_scores = {"readability", "character", "dialogue", "continuity", "progression"}
        operations = {op.operation_id for op in memory.batch.operations}
        if not (
            self.approved and self.reviewed_by.strip() and self.semantic_review_complete
            and self.attempt_id == request.attempt_id
            and self.draft_hash == hashlib.sha256(request.chapter_draft.full_text.encode()).hexdigest()
            and self.memory_hash == self.hash_memory(memory)
            and self.preview_package_hash == request.preview_package_hash
            and operations and set(self.operation_reviews) == operations
            and all(value == "correct" for value in self.operation_reviews.values())
            and not self.missing and set(self.quality_scores) == required_scores
            and all(3 <= score <= 5 for score in self.quality_scores.values())
            and memory.dry_run is not None and memory.dry_run.applicable
        ):
            raise CommitError(CommitValidationCode.precondition_failed,
                              "Human approval missing, incomplete, or bound to a different preview")


class ChapterCommitRequest(BaseModel):
    novel_id: str
    chapter_plan: ChapterPlan
    chapter_draft: ChapterDraft
    continuity_report: ContinuityReport
    memory_result: MemoryExtractionResult
    expected_story_state_hash: str
    # Phase 8.5 Fix B: integrity layer; server recomputes and enforces
    preview_package_hash: str | None = None
    attempt_id: str | None = None
    approval: ReviewApproval | None = None


class ChapterCommitResult(BaseModel):
    commit_id: uuid.UUID
    novel_id: str
    chapter_id: str
    chapter_number: int
    before_story_state_hash: str
    after_story_state_hash: str
    draft_hash: str
    operation_count: int
    applied_operation_fingerprints: list[str] = Field(default_factory=list)
    temporary_entity_mapping: dict[str, str] = Field(default_factory=dict)
    snapshot_id: str | None = None
    committed_at: datetime
    duplicate: bool = False

    @property
    def preview_package_hashable(self) -> str:
        return json.dumps(
            {
                "draft_hash": self.draft_hash,
                "after_story_state_hash": self.after_story_state_hash,
            },
            sort_keys=True,
        )


class ChapterPreviewPackage(BaseModel):
    """The full validated preview handed from generate-preview to finalize."""

    novel_id: str
    chapter_number: int
    user_direction: str | None = None

    plan_result: PlanGenerationResult
    draft: ChapterDraft
    continuity_report: ContinuityReport
    memory_result: MemoryExtractionResult

    story_state_hash: str  # state the whole preview is based on
    preview_package_hash: str
    generated_at: datetime
    status: str = "preview"

    @staticmethod
    def compute_hash(
        *,
        plan: ChapterPlan,
        draft: ChapterDraft,
        continuity_report: ContinuityReport,
        memory_result: MemoryExtractionResult,
        story_state_hash: str,
    ) -> str:
        if memory_result.batch.contract_version == 3:
            # Bind all reviewable content, not untrusted cached fingerprints.
            material = json.dumps({
                "plan": plan.model_dump(mode="json"),
                "draft": draft.model_dump(mode="json"),
                "continuity": continuity_report.model_dump(mode="json"),
                "memory": memory_result.model_dump(mode="json"),
                "story_state_hash": story_state_hash,
            }, ensure_ascii=False, sort_keys=True)
            return hashlib.sha256(material.encode("utf-8")).hexdigest()
        material = json.dumps(
            {
                "chapter_plan_hash": hashlib.sha256(
                    plan.model_dump_json().encode("utf-8")
                ).hexdigest(),
                "draft_hash": hashlib.sha256(
                    draft.full_text.encode("utf-8")
                ).hexdigest(),
                "continuity_valid": continuity_report.valid,
                "continuity_draft_hash": continuity_report.draft_hash,
                "memory_fingerprints": memory_result.validation_report.fingerprints,
                "story_state_hash": story_state_hash,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()


class CommitValidationReport(BaseModel):
    ok: bool
    code: CommitValidationCode | None = None
    message: str | None = None
    checks: dict[str, bool] = Field(default_factory=dict)
