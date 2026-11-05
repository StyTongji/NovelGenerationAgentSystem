"""Typed, draft-bound literary review; never a substitute for continuity."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, computed_field

from app.llm.models import GenerationMetadata

QualityDimension = Literal["readability", "character", "dialogue", "continuity", "progression"]


class LiteraryScores(BaseModel):
    readability: int = Field(ge=1, le=5, description="语言可读性、重复与冗余")
    character: int = Field(ge=1, le=5, description="动机可信度、人物辨识度")
    dialogue: int = Field(ge=1, le=5, description="对话自然度、声音区分")
    continuity: int = Field(ge=1, le=5, description="场景空间与叙事衔接；不替代 Canonical 校验")
    progression: int = Field(ge=1, le=5, description="事件推进、节奏与结尾牵引")


class LiteraryFinding(BaseModel):
    dimension: QualityDimension
    scene_id: str
    exact_quote: str = Field(min_length=1)
    explanation: str = Field(min_length=1)
    suggestion: str = Field(min_length=1)


class LiteraryAssessment(BaseModel):
    scores: LiteraryScores
    summary: str
    strengths: list[str] = Field(default_factory=list)
    findings: list[LiteraryFinding] = Field(default_factory=list)


class LiteraryQualityReport(LiteraryAssessment):
    draft_hash: str
    prompt_version: str = "literary_reviewer_v1"
    generation_metadata: GenerationMetadata | None = None
    advisory_only: bool = True

    @computed_field
    @property
    def needs_revision(self) -> bool:
        """A weak dimension cannot be hidden by a high average score."""
        return min(self.scores.model_dump().values()) < 3
