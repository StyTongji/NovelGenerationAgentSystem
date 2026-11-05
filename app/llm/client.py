"""Agent-facing LLM client.

Binds a provider, model, temperature, agent name and prompt version into
one object so Phase 4+ agents stay free of provider details. All calls flow
through the provider abstraction — agents never touch vendor SDKs, and this
module never queries story state (that is Context Builder's job, Phase 3).
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from functools import wraps

from pydantic import BaseModel

from app.llm.base import BaseLLMProvider
from app.llm.models import (
    LLMMessage,
    StructuredRequest,
    StructuredResult,
    TextRequest,
    TextResult,
)
from app.llm.errors import ProviderTimeoutError


class AgentLLMClient:
    def __init__(
        self,
        provider: BaseLLMProvider,
        *,
        agent_name: str,
        model: str = "",
        temperature: float = 0.5,
        max_tokens: int | None = None,
        prompt_version: str | None = None,
        timeout_seconds: float | None = None,
        deadline_seconds: float | None = 240.0,
    ) -> None:
        self.provider = provider
        self.agent_name = agent_name
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.prompt_version = prompt_version
        self.timeout_seconds = timeout_seconds
        self.deadline_seconds = deadline_seconds
        self._deadline = ContextVar(f"llm_deadline_{id(self)}", default=None)

    @asynccontextmanager
    async def operation(self):
        """One deadline across HTTP, JSON repair AND domain repair loops."""
        if self._deadline.get() is not None or self.deadline_seconds is None:
            yield
            return
        deadline = asyncio.get_running_loop().time() + self.deadline_seconds
        token = self._deadline.set(deadline)
        try:
            async with asyncio.timeout_at(deadline):
                yield
        except TimeoutError as exc:
            raise ProviderTimeoutError(
                f"LLM agent deadline exceeded after {self.deadline_seconds}s "
                f"(agent={self.agent_name}, model={self.model})"
            ) from exc
        finally:
            self._deadline.reset(token)

    async def run_request(self, request: TextRequest):
        """The only production Agent -> Provider entry point."""
        request = request.model_copy(update={
            "metadata": self._metadata(request.metadata),
            "max_tokens": request.max_tokens if request.max_tokens is not None else self.max_tokens,
            "timeout_seconds": request.timeout_seconds or self.timeout_seconds,
        })
        async with self.operation():
            if isinstance(request, StructuredRequest):
                return await self.provider.generate_structured(request)
            return await self.provider.generate_text(request)

    def _metadata(self, extra: dict[str, str] | None) -> dict[str, str]:
        metadata = dict(extra or {})
        metadata.setdefault("agent", self.agent_name)
        if self.prompt_version:
            metadata.setdefault("prompt_version", self.prompt_version)
        return metadata

    async def generate_text(
        self,
        *,
        system: str | None,
        user: str,
        metadata: dict[str, str] | None = None,
    ) -> TextResult:
        request = TextRequest(
            model=self.model,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            timeout_seconds=self.timeout_seconds,
            metadata=self._metadata(metadata),
            system=system,
            messages=[LLMMessage(role="user", content=user)],
        )
        return await self.run_request(request)

    async def generate_structured(
        self,
        *,
        system: str | None,
        user: str,
        response_model: type[BaseModel],
        max_repair_attempts: int = 2,
        metadata: dict[str, str] | None = None,
    ) -> StructuredResult:
        request = StructuredRequest(
            model=self.model,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            timeout_seconds=self.timeout_seconds,
            metadata=self._metadata(metadata),
            system=system,
            messages=[LLMMessage(role="user", content=user)],
            response_model=response_model,
            max_repair_attempts=max_repair_attempts,
        )
        return await self.run_request(request)


def agent_operation(method):
    """Keep domain-repair attempts within the client's initial deadline."""
    @wraps(method)
    async def wrapped(self, *args, **kwargs):
        async with self.llm.operation():
            return await method(self, *args, **kwargs)
    return wrapped
