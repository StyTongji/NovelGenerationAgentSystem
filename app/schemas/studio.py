"""Author-facing contracts for persistent, sequential chapter runs."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, Field, SecretStr, field_validator

from app.memory.models import MemoryUpdateBatch


class BudgetLimits(BaseModel):
    max_calls: int = Field(default=30, ge=1, le=100)
    chapter_tokens: int = Field(default=150_000, ge=1000, le=1_000_000)
    run_tokens: int = Field(default=2_000_000, ge=1000, le=5_000_000)


class RunInput(BudgetLimits):
    novel_id: UUID
    mode: Literal["demo", "live"] = "demo"
    provider: str = ""
    custom_base_url: str | None = None
    model: str = ""
    role_models: dict[str, str] = Field(default_factory=dict)
    max_output_tokens: int | None = Field(default=None, ge=512, le=32768)
    runtime_capabilities: dict[str, dict[str, bool]] | None = None
    chapter_count: int = Field(default=3, ge=1, le=10)
    target_chars: int = Field(default=5000, ge=1000, le=10000)
    direction: str = Field(default="", max_length=12000)
    chapter_directions: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("custom_base_url")
    @classmethod
    def normalize_base_url(cls, value):
        if value is None:
            return None
        parts = urlsplit(value.strip())
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError("API 地址必须是有效的 http:// 或 https:// 地址")
        if parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("API 地址请只填写 Base URL，密钥填入独立的 API Key 输入框")
        path = parts.path.rstrip("/")
        if path.endswith("/chat/completions"):
            path = path[:-len("/chat/completions")]
        return urlunsplit((parts.scheme, parts.netloc, path, "", ""))

    @field_validator("role_models")
    @classmethod
    def known_roles(cls, values):
        allowed = {"director", "scene_planner", "writer", "writer_repair", "continuity_checker",
                   "memory_extractor", "literary_reviewer"}
        if values.keys() - allowed:
            raise ValueError("Unknown agent role")
        return values


class StartRunInput(RunInput):
    # Accepted only on incoming requests, never persisted or returned.
    api_key: SecretStr | None = Field(default=None, exclude=True)
    replace_existing: bool = Field(default=False, exclude=True)


class RuntimeCredentialInput(BaseModel):
    api_key: SecretStr = Field(exclude=True)


class ReviewAssistanceInput(BaseModel):
    model: str = Field(default="", max_length=200)
    max_output_tokens: int | None = Field(default=None, ge=512, le=32768)


class ChapterProgress(BaseModel):
    number: int
    status: str = "queued"
    attempt_id: str = ""
    title: str = ""
    characters: int = 0
    action: Literal["generate", "revise", "recheck", "memory", "memory_retry", "memory_assisted_repair"] = "generate"
    source_attempt: str | None = None
    inputs: dict = Field(default_factory=dict)
    error: str | None = None
    assistance_file: str | None = None


class StudioRun(BaseModel):
    id: str
    novel_id: str
    title: str
    mode: Literal["demo", "live"]
    status: Literal["running", "awaiting_review", "blocked", "cancelled", "completed", "interrupted", "archived"]
    current_chapter: int
    total_chapters: int
    start_chapter: int
    chapters: list[ChapterProgress]
    config: RunInput
    active_stage: str = ""
    requires_api_key: bool = False
    error: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ApprovalInput(BaseModel):
    attempt_id: str
    reviewed_by: str = Field(min_length=1, max_length=100)
    preview_package_hash: str
    operation_reviews: dict[str, Literal["correct", "missing", "wrong", "over-extracted"]]
    missing: list[str] = Field(default_factory=list)
    quality_scores: dict[str, int]
    semantic_review_complete: bool = False
    approved: bool = False
    assisted_review_hash: str | None = None


class RevisionInput(BaseModel):
    replacements: dict[str, str]


class ItemDeclarationInput(BaseModel):
    temporary_ref: str = Field(min_length=1)
    name: str = Field(min_length=1)
    source_scene_id: str
    evidence_ids: list[str] = Field(min_length=1)


class MemoryEditInput(BaseModel):
    batch: MemoryUpdateBatch
    item_declarations: list[ItemDeclarationInput] = Field(default_factory=list)


class RecheckInput(BaseModel):
    max_output_tokens: int = Field(default=8192, ge=512, le=32768)
    runtime_capabilities: dict[str, dict[str, bool]] | None = None
    role_models: dict[str, str] | None = None
