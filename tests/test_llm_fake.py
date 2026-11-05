"""FakeLLMProvider tests: text, structured, failures, determinism."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from app.llm.errors import (
    ProviderRequestError,
    ProviderTimeoutError,
    RetryExhaustedError,
)
from app.llm.fake import FakeLLMProvider
from app.llm.models import LLMMessage, StructuredRequest, TextRequest


class SimpleOutput(BaseModel):
    name: str
    level: int


def _structured_request(**overrides) -> StructuredRequest:
    params = {
        "system": "sys",
        "messages": [LLMMessage(role="user", content="hello")],
        "response_model": SimpleOutput,
        "max_repair_attempts": 2,
    }
    params.update(overrides)
    return StructuredRequest(**params)


async def test_fake_text_generation_deterministic():
    provider = FakeLLMProvider()
    provider.enqueue_text("第一段正文", "第二段正文")

    first = await provider.generate_text(
        TextRequest(messages=[LLMMessage(role="user", content="写一段")])
    )
    second = await provider.generate_text(
        TextRequest(messages=[LLMMessage(role="user", content="再写一段")])
    )
    assert first.text == "第一段正文"
    assert second.text == "第二段正文"
    assert first.metadata.provider == "fake"
    assert first.metadata.success is True
    assert first.metadata.usage.total_tokens > 0
    assert len(provider.calls) == 2


async def test_fake_structured_generation_from_dict():
    provider = FakeLLMProvider()
    provider.enqueue_structured({"name": "林凡", "level": 3})

    result = await provider.generate_structured(_structured_request())
    assert isinstance(result.data, SimpleOutput)
    assert result.data.name == "林凡"
    assert result.metadata.attempts == 1
    assert result.raw_outputs == [result.raw]


async def test_fake_empty_queue_raises_clear_error():
    provider = FakeLLMProvider()
    with pytest.raises(ProviderRequestError, match="queue empty"):
        await provider.generate_text(
            TextRequest(messages=[LLMMessage(role="user", content="x")])
        )


async def test_fake_simulated_timeout():
    provider = FakeLLMProvider()
    provider.enqueue_text(ProviderTimeoutError("timed out"))
    with pytest.raises(ProviderTimeoutError):
        await provider.generate_text(
            TextRequest(messages=[LLMMessage(role="user", content="x")])
        )


async def test_fake_simulated_http_error():
    provider = FakeLLMProvider()
    provider.enqueue_structured(
        ProviderRequestError("HTTP 500: internal", status_code=500)
    )
    with pytest.raises(ProviderRequestError, match="HTTP 500"):
        await provider.generate_structured(_structured_request())


async def test_fake_isolated_text_and_structured_queues():
    provider = FakeLLMProvider()
    provider.enqueue_text("plain text")
    provider.enqueue_structured({"name": "n", "level": 1})
    text = await provider.generate_text(
        TextRequest(messages=[LLMMessage(role="user", content="x")])
    )
    structured = await provider.generate_structured(_structured_request())
    assert text.text == "plain text"
    assert structured.data.level == 1
