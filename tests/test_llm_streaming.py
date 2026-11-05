import asyncio
import json

import httpx
import pytest

from app.llm.budget import TrialBudget
from app.llm.client import AgentLLMClient
from app.llm.errors import ProviderRequestError, ProviderTimeoutError
from app.llm.openai_compatible import OpenAICompatibleProvider


def frame(value):
    return ("data: " + json.dumps(value) + "\n\n").encode()


class Stream(httpx.AsyncByteStream):
    def __init__(self, chunks, hang=False):
        self.chunks, self.hang, self.closed = chunks, hang, False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.hang:
            await asyncio.Event().wait()

    async def aclose(self):
        self.closed = True


async def test_stream_assembles_answer_and_usage_without_reasoning(monkeypatch):
    monkeypatch.setenv("TEST_KEY", "fake-key")
    stream = Stream([
        frame({"id": "request", "model": "Auto", "choices": [{"delta": {"reasoning_content": "private-reasoning"}}]}),
        frame({"choices": [{"delta": {"content": "o"}}]}),
        frame({"choices": [{"delta": {"content": "k"}, "finish_reason": "stop"}]}),
        frame({"choices": [], "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}}),
        b"data: [DONE]\n\n",
    ])
    def handler(request):
        assert json.loads(request.content)["stream_options"] == {"include_usage": True}
        return httpx.Response(200, stream=stream)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        provider = OpenAICompatibleProvider(base_url="https://test", api_key_env="TEST_KEY", http_client=http)
        provider.model_capabilities["Auto"] = {"stream_ok": True}
        result = await AgentLLMClient(provider, agent_name="test", model="Auto").generate_text(system="", user="test")
    assert result.text == "ok" and result.metadata.usage.total_tokens == 5
    assert "private-reasoning" not in result.model_dump_json()
    assert result.metadata.extra["http_attempts"][0]["stream"]
    assert stream.closed


@pytest.mark.parametrize("hang", [False, True])
async def test_stream_incomplete_or_hang_never_commits_answer(tmp_path, monkeypatch, hang):
    monkeypatch.setenv("TEST_KEY", "fake-key")
    stream = Stream([frame({"choices": [{"delta": {"content": "partial"}}]})], hang=hang)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=stream))) as http:
        provider = OpenAICompatibleProvider(base_url="https://test", api_key_env="TEST_KEY", http_client=http,
                                           budget=TrialBudget(tmp_path / "usage.json", 1))
        provider.model_capabilities["Auto"] = {"stream_ok": True}
        client = AgentLLMClient(provider, agent_name="test", model="Auto", max_tokens=100, deadline_seconds=.02)
        with pytest.raises(ProviderTimeoutError if hang else ProviderRequestError):
            await client.generate_text(system="", user="test")
    assert stream.closed
    rows = json.loads((tmp_path / "usage.json").read_text())["reservations"]
    assert len(rows) == 1 and all(not r["usage_known"] for r in rows.values())
