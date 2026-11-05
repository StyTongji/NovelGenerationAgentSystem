"""Strongly-typed LLM request / result DTOs.

Business code passes these models around; never raw dicts. Provenance data
(agent, prompt version, latency, usage) rides in ``GenerationMetadata`` so
Phase 4/5 can persist it on chapters without schema churn.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Role = Literal["system", "user", "assistant"]


class LLMMessage(BaseModel):
    role: Role
    content: str


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class GenerationMetadata(BaseModel):
    """Provenance for one generation call."""

    provider: str
    model: str
    agent: str | None = None
    prompt_version: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    latency_ms: float | None = None
    finish_reason: str | None = None
    usage: Usage = Field(default_factory=Usage)
    attempts: int = 1
    success: bool = True
    error: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class LLMRequestBase(BaseModel):
    """Common generation parameters. ``model`` empty = provider default."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    model: str = ""
    temperature: float = 0.7
    max_tokens: int | None = None
    timeout_seconds: float | None = None
    # provenance hints copied onto results (agent name, prompt version, ...)
    metadata: dict[str, str] = Field(default_factory=dict)


class TextRequest(LLMRequestBase):
    system: str | None = None
    messages: list[LLMMessage] = Field(default_factory=list)

    def all_messages(self) -> list[LLMMessage]:
        msgs: list[LLMMessage] = []
        if self.system:
            msgs.append(LLMMessage(role="system", content=self.system))
        msgs.extend(self.messages)
        return msgs


class StructuredRequest(TextRequest):
    """Request a JSON response validated against a Pydantic model."""

    response_model: type[BaseModel]
    # initial attempt + this many repair attempts; never unlimited
    max_repair_attempts: int = 2
    include_json_schema_hint: bool = True
    compact_schema_hint: bool = False

    def json_instruction(self) -> str:
        instruction = (
            "Respond with a single JSON object and nothing else. "
            "No markdown fences, no commentary."
        )
        if self.include_json_schema_hint:
            import json

            schema_object = self.response_model.model_json_schema()
            if self.compact_schema_hint:
                def compact(value):
                    if isinstance(value, dict):
                        return {k: ({name: compact(schema) for name, schema in v.items()}
                                    if k in {"properties", "$defs", "patternProperties"} else compact(v))
                                for k, v in value.items()
                                if k not in {"title", "description", "examples", "default"}}
                    if isinstance(value, list):
                        return [compact(v) for v in value]
                    return value
                schema_object = compact(schema_object)
            # Whitespace is not schema semantics. Retain descriptions and every
            # constraint unless the caller explicitly opts into metadata pruning.
            schema = json.dumps(schema_object, ensure_ascii=False, separators=(",", ":"))
            instruction += f"\nThe JSON must conform to this schema:\n{schema}"
        return instruction


class TextResult(BaseModel):
    text: str
    raw: str
    metadata: GenerationMetadata


class StructuredResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    data: BaseModel  # validated instance of request.response_model
    raw: str  # raw output of the successful attempt
    raw_outputs: list[str]  # every attempt, preserved for debugging
    metadata: GenerationMetadata

    def model_dump_validated(self) -> dict:
        return self.data.model_dump()
