"""Deterministic FakeLLMProvider for tests.

No network, no API key, fully deterministic. Tests enqueue responses
(text or structured) and optional scripted failures; the entire chapter
pipeline (Phase 8) must be able to run against this provider only.

Enqueue entries:
- ``str``                       -> raw text response
- ``BaseModel`` / ``dict``      -> serialized to JSON for structured calls
- ``Exception``                 -> raised as-is (use ProviderTimeoutError,
                                   ProviderRequestError, ... to simulate)
"""

from __future__ import annotations

import json
from collections import deque
from typing import Literal

from pydantic import BaseModel

from app.llm.base import BaseLLMProvider
from app.llm.errors import ProviderRequestError
from app.llm.models import LLMMessage, Usage


class FakeLLMProvider(BaseLLMProvider):
    name = "fake"
    default_model = "fake-model"

    def __init__(
        self,
        *,
        default_text: str = "fake response",
        simulate_latency_ms: float = 0.0,
    ) -> None:
        self.simulate_latency_ms = simulate_latency_ms
        self._text_queue: deque[str | Exception] = deque()
        self._structured_queue: deque[str | Exception] = deque()
        self.calls: list[list[LLMMessage]] = []

    # ------------------------------------------------------------- scripting

    def enqueue_text(self, *responses: str | Exception) -> None:
        self._text_queue.extend(responses)

    def enqueue_structured(self, *responses: str | dict | BaseModel | Exception) -> None:
        for response in responses:
            if isinstance(response, BaseModel):
                self._structured_queue.append(
                    response.model_dump_json(by_alias=True, exclude_none=True)
                )
            elif isinstance(response, dict):
                self._structured_queue.append(json.dumps(response, ensure_ascii=False))
            else:
                self._structured_queue.append(response)

    def clear(self) -> None:
        self._text_queue.clear()
        self._structured_queue.clear()
        self.calls.clear()

    # ---------------------------------------------------------------- internals

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
        self.calls.append(messages)
        if self.simulate_latency_ms:
            import asyncio
            await asyncio.sleep(self.simulate_latency_ms / 1000)
        queue = self._structured_queue if mode == "structured" else self._text_queue
        if not queue:
            raise ProviderRequestError(
                f"FakeLLMProvider queue empty for a {mode} call "
                "(enqueue responses before calling)"
            )
        item = queue.popleft()
        if isinstance(item, Exception):
            raise item
        usage = Usage(
            prompt_tokens=sum(len(m.content) for m in messages) // 2 or 1,
            completion_tokens=max(1, len(item) // 2),
            total_tokens=(sum(len(m.content) for m in messages) + len(item)) // 2 or 2,
        )
        return item, "stop", usage
