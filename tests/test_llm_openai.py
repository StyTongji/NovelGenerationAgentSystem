"""OpenAI-compatible provider tests using httpx MockTransport (no network).

Covers: happy path, timeout, HTTP error mapping, retry logic, and the
secrecy guarantee (API key never appears in exceptions or logs).
"""

from __future__ import annotations

import logging

import httpx
import pytest

from app.llm.errors import (
    ProviderConfigurationError,
    ProviderRequestError,
    ProviderTimeoutError,
)
from app.llm.models import LLMMessage, TextRequest
from app.llm.openai_compatible import OpenAICompatibleProvider

SECRET_KEY = "sk-test-secret-1234567890"
API_KEY_ENV = "NOVEL_TEST_LLM_KEY"


@pytest.fixture
def api_key(monkeypatch):
    monkeypatch.setenv(API_KEY_ENV, SECRET_KEY)
    return SECRET_KEY


def make_provider(handler, **overrides) -> OpenAICompatibleProvider:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    params = {
        "base_url": "https://api.example.com/v1",
        "api_key_env": API_KEY_ENV,
        "model": "test-model",
        "http_client": client,
    }
    params.update(overrides)
    return OpenAICompatibleProvider(**params)


def _request() -> TextRequest:
    return TextRequest(messages=[LLMMessage(role="user", content="你好")])


async def test_missing_api_key_raises_configuration_error(monkeypatch):
    monkeypatch.delenv(API_KEY_ENV, raising=False)
    provider = make_provider(lambda request: httpx.Response(200, json={}))
    with pytest.raises(ProviderConfigurationError, match=API_KEY_ENV):
        await provider.generate_text(_request())


async def test_empty_base_url_rejected():
    with pytest.raises(ProviderConfigurationError):
        OpenAICompatibleProvider(base_url="")


async def test_successful_completion(api_key):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["Authorization"] == f"Bearer {SECRET_KEY}"
        body = request.read()
        assert b"test-model" in body
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": "正文内容"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        )

    provider = make_provider(handler)
    result = await provider.generate_text(_request())
    assert result.text == "正文内容"
    assert result.metadata.finish_reason == "stop"
    assert result.metadata.usage.total_tokens == 15
    assert result.metadata.model == "test-model"
    await provider.aclose()


async def test_logs_each_api_call(api_key, caplog):
    provider = make_provider(
        lambda request: httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]},
        )
    )
    with caplog.at_level(logging.INFO, logger="app.llm.openai_compatible"):
        await provider.generate_text(_request())

    messages = [record.getMessage() for record in caplog.records]
    assert any("llm call started" in message and "mode=text" in message for message in messages)
    assert any("llm call completed" in message and "total_tokens=" in message for message in messages)
    await provider.aclose()


async def test_timeout_mapped_to_provider_timeout_error(api_key):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out", request=request)

    provider = make_provider(handler, max_retries=0)
    with pytest.raises(ProviderTimeoutError):
        await provider.generate_text(_request())
    await provider.aclose()


async def test_http_error_raises_request_error_without_retry_on_400(api_key):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="bad request")

    provider = make_provider(handler, max_retries=3)
    with pytest.raises(ProviderRequestError, match="HTTP 400"):
        await provider.generate_text(_request())
    await provider.aclose()


async def test_retryable_5xx_then_success(api_key):
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(503, text="overloaded")
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]},
        )

    provider = make_provider(handler, max_retries=2, retry_backoff_seconds=0.01)
    result = await provider.generate_text(_request())
    assert result.text == "ok"
    assert calls["count"] == 2
    await provider.aclose()


async def test_retries_exhausted_raises_last_error(api_key):
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(500, text="server error")

    provider = make_provider(handler, max_retries=2, retry_backoff_seconds=0.01)
    with pytest.raises(ProviderRequestError, match="HTTP 500"):
        await provider.generate_text(_request())
    assert calls["count"] == 3  # initial + 2 retries
    await provider.aclose()


async def test_api_key_never_in_exception_or_logs(api_key, caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    provider = make_provider(handler, max_retries=0)
    with caplog.at_level(logging.DEBUG, logger="app.llm.openai_compatible"):
        with pytest.raises(ProviderRequestError) as excinfo:
            await provider.generate_text(_request())

    assert SECRET_KEY not in str(excinfo.value)
    assert SECRET_KEY not in excinfo.value.args
    for record in caplog.records:
        assert SECRET_KEY not in record.getMessage()
    await provider.aclose()


async def test_scrub_secrets_removes_all_occurrences():
    from app.llm.errors import scrub_secrets

    text = f"Authorization: Bearer {SECRET_KEY} failed at {SECRET_KEY} endpoint"
    assert scrub_secrets(text, SECRET_KEY) == (
        "Authorization: Bearer ***REDACTED*** failed at ***REDACTED*** endpoint"
    )
