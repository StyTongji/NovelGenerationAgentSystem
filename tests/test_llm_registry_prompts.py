"""Registry, agent model config, AgentLLMClient, prompt loading, schemas."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import BaseModel

from app.config import Settings
from app.llm.client import AgentLLMClient
from app.llm.errors import ProviderConfigurationError
from app.llm.fake import FakeLLMProvider
from app.llm.models import GenerationMetadata, StructuredResult, TextResult
from app.llm.prompts import PromptError, PromptRegistry, get_default_registry
from app.llm.registry import ProviderRegistry, build_registry
from app.continuity.models import ContinuityReport
from app.memory.models import MemoryUpdateBatch, MemoryUpdateOperation
from app.models.enums import Severity
from app.schemas.agent_outputs import ChapterPlan

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------------ registry


def _settings_with(yaml_like: dict) -> Settings:
    settings = Settings()
    if "llm" in yaml_like:
        from app.config import LLMConfig

        settings.llm = LLMConfig(**yaml_like["llm"])
    return settings


def test_registry_unknown_provider_raises():
    registry = ProviderRegistry()
    registry.register("fake", FakeLLMProvider())
    with pytest.raises(ProviderConfigurationError, match="Unknown LLM provider"):
        registry.get("nonexistent")


def test_registry_builds_from_settings():
    settings = _settings_with(
        {
            "llm": {
                "default_provider": "main",
                "providers": {
                    "main": {
                        "type": "openai_compatible",
                        "base_url": "https://api.example.com/v1",
                        "api_key_env": "NOVEL_LLM_API_KEY",
                        "timeout_seconds": 30,
                        "max_retries": 1,
                    }
                },
                "agents": {
                    "director": {"provider": "main", "model": "m1", "temperature": 0.4},
                    "writer": {"provider": "main", "model": "m2", "temperature": 0.9},
                },
            }
        }
    )
    registry = build_registry(settings, include_fake=True)
    assert "main" in registry.names()

    client = registry.client_for_agent("director", settings, prompt_version="director_v1")
    assert isinstance(client, AgentLLMClient)
    assert client.model == "m1"
    assert client.temperature == 0.4
    assert client.prompt_version == "director_v1"

    writer = registry.client_for_agent("writer", settings)
    assert writer.temperature == 0.9
    assert writer.model == "m2"


def test_agent_falls_back_to_defaults_and_default_provider():
    settings = Settings()
    settings.llm.providers = {}
    settings.llm.default_provider = "fake"
    registry = build_registry(settings, include_fake=True)
    client = registry.client_for_agent("memory_extractor", settings)
    # AGENTS.md defaults: extractor temperature 0~0.2
    assert client.temperature == pytest.approx(0.1)
    assert isinstance(client.provider, FakeLLMProvider)


def test_business_code_needs_no_vendor_name():
    """Provider construction from settings only — vendor names appear
    exclusively in the registry layer."""
    settings = _settings_with(
        {"llm": {"providers": {"p": {"type": "openai_compatible", "base_url": "http://x"}}}}
    )
    registry = build_registry(settings)
    assert registry.get("p").name == "openai_compatible"


# ---------------------------------------------------------------- AgentLLMClient


async def test_agent_client_text_and_structured_with_metadata():
    provider = FakeLLMProvider()
    provider.enqueue_text("hello")
    provider.enqueue_structured({"chapter_goal": "g", "scenes": [{"index": 1, "goal": "s", "location": "l"}], "ending_hook": "h"})
    client = AgentLLMClient(
        provider,
        agent_name="director",
        model="fake-model",
        temperature=0.4,
        prompt_version="director_v1",
    )
    text = await client.generate_text(system="sys", user="usr")
    assert isinstance(text, TextResult)
    assert text.metadata.agent == "director"
    assert text.metadata.prompt_version == "director_v1"

    structured = await client.generate_structured(
        system="sys", user="usr", response_model=ChapterPlan
    )
    assert isinstance(structured, StructuredResult)
    assert structured.data.ending_hook == "h"
    assert structured.metadata.attempts == 1


# -------------------------------------------------------------- prompt loading


def test_prompt_registry_loads_repo_prompts():
    registry = get_default_registry()
    for expected in ["director", "writer", "memory_extractor", "continuity_checker"]:
        template = registry.get(expected)
        assert template.version == "v1"
        assert len(template.content) > 50


def test_prompt_directory_scanning_and_versioning(tmp_path):
    (tmp_path / "writer.md").write_text("v1 content", encoding="utf-8")
    (tmp_path / "writer_v2.md").write_text("v2 content {{style}}", encoding="utf-8")
    (tmp_path / "writer_xianxia_v1.md").write_text("xianxia v1", encoding="utf-8")

    registry = PromptRegistry.from_directory(tmp_path)
    # latest version wins by default
    assert registry.get("writer").version == "v2"
    assert registry.get("writer", version="v1").content == "v1 content"
    assert registry.get("writer_xianxia").content == "xianxia v1"

    rendered = registry.render("writer", style="古风")
    assert rendered == "v2 content 古风"


def test_prompt_missing_variable_raises():
    registry = PromptRegistry()
    registry.register("t", "hello {{name}}")
    with pytest.raises(PromptError, match="name"):
        registry.render("t")
    assert registry.render("t", name="林凡") == "hello 林凡"


def test_prompt_unknown_name_raises():
    registry = PromptRegistry()
    with pytest.raises(PromptError):
        registry.get("nope")


# --------------------------------------------- JSON Schema / Pydantic 对接检查


def _validate_file(schema_file: str, instance: dict):
    schema = json.loads((PROJECT_ROOT / "schemas" / schema_file).read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(instance)


def test_chapter_plan_matches_json_schema():
    plan = ChapterPlan(
        chapter_goal="进入拍卖会",
        scenes=[
            {"index": 1, "goal": "抵达天剑城", "participants": ["林凡"], "location": "天剑城"}
        ],
        must_not_reveal=["父亲去向"],
        ending_hook="神秘女子出现",
        conflicts=[],
    )
    _validate_file("chapter_plan.schema.json", plan.model_dump(exclude_none=True))


def test_memory_update_batch_matches_json_schema():
    batch = MemoryUpdateBatch(
        operations=[
            {
                "operation_id": "op1",
                "update_type": "create_event",
                "source_scene_id": "s1",
                "evidence": [
                    {"scene_id": "s1", "exact_quote": "苏青挡在林凡身前。"}
                ],
                "confidence": 0.9,
                "event": {
                    "event_type": "combat",
                    "author_summary": "苏青替林凡挡剑",
                    "reader_safe_summary": "苏青挡在林凡身前",
                    "participant_ids": ["su_qing", "lin_fan"],
                    "importance": 0.9,
                },
            }
        ],
        candidate_author_summary="作者摘要",
        candidate_reader_summary="读者摘要",
    )
    assert isinstance(batch.operations[0], MemoryUpdateOperation)
    _validate_file("memory_update.schema.json", batch.model_dump(exclude_none=True))


def test_continuity_report_matches_json_schema():
    report = ContinuityReport(
        valid=False,
        issues=[
            {
                "issue_id": "i1",
                "category": "dead_character",
                "severity": "critical",
                "scene_id": "s1",
                "evidence_excerpt": "死者开口说话",
                "author_explanation": "死亡角色不能无解释复活",
                "suggested_action": "删除该场景",
                "source": "deterministic",
                "blocking": True,
            }
        ],
        blocking_issue_count=1,
        highest_severity=Severity.critical,
    )
    _validate_file("continuity_report.schema.json", report.model_dump(exclude_none=True))


def test_severity_values_match_schema_enum():
    from app.models.enums import Severity

    assert [s.value for s in Severity] == ["info", "warning", "error", "critical"]


def test_generation_metadata_roundtrip():
    metadata = GenerationMetadata(
        provider="fake",
        model="fake-model",
        agent="writer",
        prompt_version="writer_v1",
        temperature=0.85,
        latency_ms=12.5,
        finish_reason="stop",
        attempts=2,
    )
    dumped = metadata.model_dump()
    assert GenerationMetadata(**dumped) == metadata
