"""Phase 8.7 provider total-deadline and cancellation checks."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.llm.client import AgentLLMClient
from app.llm.errors import ProviderTimeoutError
from app.llm.fake import FakeLLMProvider
from app.llm.models import LLMMessage, Usage
from app.llm.base import BaseLLMProvider


class HangingProvider(BaseLLMProvider):
    name = "hanging"

    async def _complete(
        self, messages: list[LLMMessage], *, model: str, temperature: float,
        max_tokens: int | None, timeout_seconds: float | None,
        agent: str | None = None, mode: str = "text",
    ) -> tuple[str, str | None, Usage]:
        await asyncio.Event().wait()
        return "", "stop", Usage()


@pytest.mark.asyncio
async def test_agent_deadline_cancels_permanent_provider_hang():
    client = AgentLLMClient(
        HangingProvider(), agent_name="memory_extractor", model="test",
        deadline_seconds=0.01,
    )
    with pytest.raises(ProviderTimeoutError):
        await client.generate_text(system="", user="hang")


@pytest.mark.asyncio
async def test_external_task_cancel_propagates_without_retry():
    provider = HangingProvider()
    client = AgentLLMClient(
        provider, agent_name="writer", model="test", deadline_seconds=5,
    )
    task = asyncio.create_task(client.generate_text(system="", user="cancel"))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_retry_sleep_is_inside_total_deadline():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="busy")

    transport = httpx.MockTransport(handler)
    provider = __import__("app.llm.openai_compatible", fromlist=["OpenAICompatibleProvider"]).OpenAICompatibleProvider(
        base_url="https://example.test/v1", api_key_env="TEST_DEADLINE_KEY",
        model="test", max_retries=5, retry_backoff_seconds=0.05,
        http_client=httpx.AsyncClient(transport=transport),
    )
    import os
    os.environ["TEST_DEADLINE_KEY"] = "deadline-key"
    try:
        client = AgentLLMClient(
            provider, agent_name="director", model="test", deadline_seconds=0.02,
        )
        with pytest.raises(ProviderTimeoutError):
            await client.generate_text(system="", user="retry")
    finally:
        os.environ.pop("TEST_DEADLINE_KEY", None)
        await provider.aclose()
