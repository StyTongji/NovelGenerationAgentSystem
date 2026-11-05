"""Provider abstraction and the structured-generation validation loop.

Agents depend only on :class:`LLMProvider`; concrete transports subclass
:class:`BaseLLMProvider` and implement the single low-level ``_complete``.

The structured loop enforces: raw output -> JSON extraction -> Pydantic
validation -> repair retry (bounded) -> validated model. A successful
``json.loads`` alone is never treated as valid.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Literal

from pydantic import ValidationError

from app.llm.errors import (
    JSONExtractionError,
    RetryExhaustedError,
    StructuredValidationError,
)
from app.llm.json_utils import extract_json_object
from app.llm.models import (
    GenerationMetadata,
    LLMMessage,
    StructuredRequest,
    StructuredResult,
    TextRequest,
    TextResult,
    Usage,
)


class BaseLLMProvider(ABC):
    """Abstract provider. Subclasses implement one raw completion call."""

    name: str = "base"
    default_model: str = ""

    @abstractmethod
    async def _complete(
        self,
        messages: list[LLMMessage],
        *,
        model: str,
        temperature: float,
        max_tokens: int | None,
        timeout_seconds: float | None,
        agent: str | None = None,
        mode: Literal["text", "structured"] = "text",
    ) -> tuple[str, str | None, Usage]:
        """Run one chat completion.

        ``mode`` is an explicit call-kind signal (never inferred from prompt
        text). Returns ``(text, finish_reason, usage)``.
        Raises ProviderRequestError / ProviderTimeoutError on failure.
        """

    async def generate_text(self, request: TextRequest) -> TextResult:
        start = time.perf_counter()
        text, finish_reason, usage = await self._complete(
            request.all_messages(),
            model=request.model or self.default_model,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
            timeout_seconds=request.timeout_seconds,
            agent=request.metadata.get("agent"),
            mode="text",
        )
        latency_ms = (time.perf_counter() - start) * 1000
        metadata = self._metadata(request, latency_ms=latency_ms, finish_reason=finish_reason, usage=usage)
        return TextResult(text=text, raw=text, metadata=metadata)

    async def generate_structured(self, request: StructuredRequest) -> StructuredResult:
        """Structured generation with bounded validation/repair loop."""
        max_attempts = max(1, request.max_repair_attempts + 1)
        messages = list(request.all_messages())
        if request.json_instruction():
            instruction = request.json_instruction()
            if messages and messages[0].role == "system":
                messages[0] = LLMMessage(
                    role="system", content=f"{messages[0].content}\n\n{instruction}"
                )
            else:
                messages.insert(0, LLMMessage(role="system", content=instruction))

        initial_messages = list(messages)
        raw_outputs: list[str] = []
        errors: list[str] = []
        last_finish_reason: str | None = None
        total_usage = Usage()
        start = time.perf_counter()

        for attempt in range(1, max_attempts + 1):
            text, finish_reason, usage = await self._complete(
                messages,
                model=request.model or self.default_model,
                temperature=request.temperature,
                max_tokens=request.max_tokens,
                timeout_seconds=request.timeout_seconds,
                agent=request.metadata.get("agent"),
                mode="structured",
            )
            total_usage = Usage(
                prompt_tokens=total_usage.prompt_tokens + usage.prompt_tokens,
                completion_tokens=total_usage.completion_tokens + usage.completion_tokens,
                total_tokens=total_usage.total_tokens + usage.total_tokens,
            )
            last_finish_reason = finish_reason
            raw_outputs.append(text)

            if finish_reason == "length" and not text.strip():
                # Repeating the identical output cap with a JSON-repair prompt
                # cannot repair an empty completion (often reasoning-only).
                raise RetryExhaustedError(
                    "模型耗尽单次输出上限但未返回可解析内容（finish_reason=length）；"
                    "可能是思考 token 占满输出额度。请提高单次输出上限，或使用已验证支持非思考模式的模型；"
                    "本次不继续相同额度的空输出重试。",
                    attempts=attempt, raw_outputs=raw_outputs,
                    errors=[*errors, "Empty completion at output token limit"],
                )

            try:
                data = extract_json_object(text)
            except JSONExtractionError as exc:
                errors.append(f"attempt {attempt}: {exc}")
                messages = list(initial_messages)
                self._append_repair_messages(messages, text, errors[-1])
                continue

            try:
                validated = request.response_model.model_validate(data)
            except ValidationError as exc:
                error_summary = _format_validation_error(exc)
                errors.append(f"attempt {attempt}: schema validation failed: {error_summary}")
                messages = list(initial_messages)
                self._append_repair_messages(messages, text, errors[-1])
                continue

            latency_ms = (time.perf_counter() - start) * 1000
            metadata = self._metadata(
                request,
                latency_ms=latency_ms,
                finish_reason=last_finish_reason,
                usage=total_usage,
            )
            metadata.attempts = attempt
            metadata.extra["structured_validation_errors"] = list(errors)
            return StructuredResult(
                data=validated, raw=text, raw_outputs=raw_outputs, metadata=metadata
            )

        raise RetryExhaustedError(
            f"Structured generation failed after {max_attempts} attempt(s) "
            f"for model={request.model or self.default_model!r}",
            attempts=max_attempts,
            raw_outputs=raw_outputs,
            errors=errors,
        )

    @staticmethod
    def _append_repair_messages(messages: list[LLMMessage], raw: str, error: str) -> None:
        messages.append(LLMMessage(role="assistant", content=raw))
        messages.append(
            LLMMessage(
                role="user",
                content=(
                    f"Your previous response was invalid: {error}\n"
                    "Fix the problem and respond again with a single valid JSON object. "
                    "No markdown fences, no commentary."
                ),
            )
        )

    def _metadata(
        self,
        request: TextRequest,
        *,
        latency_ms: float,
        finish_reason: str | None = None,
        usage: Usage | None = None,
    ) -> GenerationMetadata:
        return GenerationMetadata(
            provider=self.name,
            model=request.model or self.default_model,
            agent=request.metadata.get("agent"),
            prompt_version=request.metadata.get("prompt_version"),
            temperature=request.temperature,
            max_tokens=request.max_tokens,
            latency_ms=latency_ms,
            finish_reason=finish_reason,
            usage=usage or Usage(),
        )


def _format_validation_error(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors()[:10]:
        loc = ".".join(str(piece) for piece in error.get("loc", []))
        parts.append(f"{loc or '<root>'}: {error.get('msg')}")
    return "; ".join(parts)
