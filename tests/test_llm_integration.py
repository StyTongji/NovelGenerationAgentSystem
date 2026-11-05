"""Integration tests against a REAL OpenAI-compatible endpoint.

These never run in the default pytest pass (see pyproject addopts
``-m "not integration"``). Run explicitly with:

    NOVEL_LLM_API_KEY=... NOVEL_LLM_BASE_URL=... pytest -m integration
"""

from __future__ import annotations

import os

import pytest
from pydantic import BaseModel

from app.llm.models import LLMMessage, StructuredRequest, TextRequest
from app.llm.openai_compatible import OpenAICompatibleProvider

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("NOVEL_LLM_API_KEY"),
        reason="NOVEL_LLM_API_KEY not set; integration tests are opt-in",
    ),
]


class TinyPlan(BaseModel):
    chapter_goal: str
    ending_hook: str


async def test_real_endpoint_text_generation():
    provider = OpenAICompatibleProvider(
        base_url=os.environ.get("NOVEL_LLM_BASE_URL", "https://api.example.com/v1"),
        model=os.environ.get("NOVEL_TEST_MODEL", "gpt-4o-mini"),
        timeout_seconds=30,
        max_retries=1,
    )
    result = await provider.generate_text(
        TextRequest(
            system="你是测试助手，只回复两个字：收到",
            messages=[LLMMessage(role="user", content="测试")],
            max_tokens=20,
        )
    )
    assert len(result.text) > 0
    assert result.metadata.usage.total_tokens > 0
    await provider.aclose()


async def test_real_endpoint_structured_generation():
    provider = OpenAICompatibleProvider(
        base_url=os.environ.get("NOVEL_LLM_BASE_URL", "https://api.example.com/v1"),
        model=os.environ.get("NOVEL_TEST_MODEL", "gpt-4o-mini"),
        timeout_seconds=60,
    )
    result = await provider.generate_structured(
        StructuredRequest(
            system="只输出 JSON",
            messages=[
                LLMMessage(
                    role="user",
                    content='为一章玄幻小说给一个迷你计划：{"chapter_goal":"...","ending_hook":"..."}，中文，各不超过15字',
                )
            ],
            response_model=TinyPlan,
            max_repair_attempts=1,
            include_json_schema_hint=True,
            temperature=0.1,
        )
    )
    assert isinstance(result.data, TinyPlan)
    assert result.data.chapter_goal
    await provider.aclose()
