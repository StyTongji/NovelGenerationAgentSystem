"""Structured generation loop: extraction, validation, repair, exhaustion."""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel, Field

from app.llm.errors import JSONExtractionError, RetryExhaustedError
from app.llm.fake import FakeLLMProvider
from app.llm.json_utils import extract_json_object
from app.llm.models import LLMMessage, StructuredRequest


class Sword(BaseModel):
    name: str
    grade: str
    power: int = Field(ge=0, le=100)


def _request(**overrides) -> StructuredRequest:
    params = {
        "system": "你是测试助手",
        "messages": [LLMMessage(role="user", content="给我一把剑")],
        "response_model": Sword,
        "max_repair_attempts": 2,
    }
    params.update(overrides)
    return StructuredRequest(**params)


# ------------------------------------------------------------- json extraction


def test_extract_plain_json():
    assert extract_json_object('{"a": 1}') == {"a": 1}


def test_extract_fenced_json_with_commentary():
    raw = '好的，这是结果：\n```json\n{"name": "镜", "grade": "仙品", "power": 90}\n```\n以上。'
    assert extract_json_object(raw)["name"] == "镜"


def test_extract_json_with_leading_text():
    raw = '结果如下 {"name": "剑", "grade": "凡品", "power": 10} 希望有帮助'
    assert extract_json_object(raw)["power"] == 10


def test_extract_json_with_nested_braces_in_strings():
    raw = '{"name": "阵 { 奇门 }", "grade": "x", "power": 1}'
    assert extract_json_object(raw)["name"] == "阵 { 奇门 }"


def test_extract_failure_raises_extraction_error():
    with pytest.raises(JSONExtractionError):
        extract_json_object(" completely no json here ")
    with pytest.raises(JSONExtractionError):
        extract_json_object("")


# --------------------------------------------------- validation / repair loop


async def test_pydantic_validation_success():
    provider = FakeLLMProvider()
    provider.enqueue_structured({"name": "轮回古镜", "grade": "仙品", "power": 99})
    result = await provider.generate_structured(_request())
    assert result.data.grade == "仙品"


async def test_malformed_json_then_repair_success():
    provider = FakeLLMProvider()
    # attempt 1: not JSON at all; attempt 2: valid
    provider.enqueue_structured("我拒绝输出JSON", {"name": "青锋剑", "grade": "上品", "power": 40})
    result = await provider.generate_structured(_request())
    assert result.data.name == "青锋剑"
    assert result.metadata.attempts == 2
    assert len(result.raw_outputs) == 2
    # repair conversation included the error feedback
    assert any("invalid" in m.content for m in provider.calls[-1])


async def test_validation_failure_then_repair_success():
    provider = FakeLLMProvider()
    # attempt 1: parses as JSON but power out of range (schema violation)
    provider.enqueue_structured(
        '{"name": "剑", "grade": "凡品", "power": 900}',
        '{"name": "剑", "grade": "凡品", "power": 9}',
    )
    result = await provider.generate_structured(_request())
    assert result.data.power == 9
    assert result.metadata.attempts == 2


async def test_repair_attempts_exhausted_raises_retry_exhausted():
    provider = FakeLLMProvider()
    provider.enqueue_structured("no json", "still no json", "never json")
    with pytest.raises(RetryExhaustedError) as excinfo:
        await provider.generate_structured(_request(max_repair_attempts=2))
    assert excinfo.value.attempts == 3
    assert len(excinfo.value.raw_outputs) == 3
    assert len(excinfo.value.errors) == 3


async def test_no_unbounded_retry_default():
    provider = FakeLLMProvider()
    # enqueue more failures than allowed attempts; provider must stop early
    provider.enqueue_structured("bad1", "bad2")  # only 2 for max_repair_attempts=1
    with pytest.raises(RetryExhaustedError) as excinfo:
        await provider.generate_structured(_request(max_repair_attempts=1))
    assert excinfo.value.attempts == 2


async def test_raw_outputs_preserved_on_failure():
    provider = FakeLLMProvider()
    provider.enqueue_structured({"name": "剑", "grade": 123, "power": 5})  # wrong type
    with pytest.raises(RetryExhaustedError) as excinfo:
        await provider.generate_structured(_request(max_repair_attempts=0))
    assert excinfo.value.raw_outputs[0] == '{"name": "剑", "grade": 123, "power": 5}'


async def test_structured_request_includes_schema_hint():
    request = _request()
    instruction = request.json_instruction()
    assert "JSON" in instruction
    assert "grade" in instruction  # schema fields surfaced to the model


async def test_metadata_records_prompt_version_and_agent():
    provider = FakeLLMProvider()
    provider.enqueue_structured({"name": "剑", "grade": "凡品", "power": 1})
    request = _request(metadata={"agent": "director", "prompt_version": "director_v1"})
    result = await provider.generate_structured(request)
    assert result.metadata.agent == "director"
    assert result.metadata.prompt_version == "director_v1"


def test_json_dumps_roundtrip_with_chinese():
    payload = {"name": "苏青", "secrecy": "top_secret"}
    raw = json.dumps(payload, ensure_ascii=False)
    assert json.loads(raw) == payload
