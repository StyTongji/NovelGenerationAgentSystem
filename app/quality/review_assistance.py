"""Model-suggested form values, bound to a candidate; never an approval."""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.llm.models import GenerationMetadata
from app.quality.models import LiteraryScores


class OperationReviewSuggestion(BaseModel):
    operation_id: str
    verdict: Literal["correct", "missing", "wrong", "over-extracted"]
    reason: str = Field(min_length=1)


class ReviewAssistanceAssessment(BaseModel):
    operation_reviews: list[OperationReviewSuggestion]
    missing: list[str] = Field(default_factory=list)
    quality_scores: LiteraryScores
    summary: str


class ReviewAssistanceReport(ReviewAssistanceAssessment):
    attempt_id: str
    preview_package_hash: str
    draft_hash: str
    memory_hash: str
    generated_at: datetime
    source: Literal["model", "offline_demo"]
    generation_metadata: GenerationMetadata | None = None
    report_hash: str = ""
