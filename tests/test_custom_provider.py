"""Author-entered credentials route requests without leaking into artifacts."""
import asyncio
import json
import uuid

import httpx
import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Settings
from app.llm.registry import build_registry
from app.main import create_app
from app.schemas.studio import RunInput, StartRunInput, StudioRun, ChapterProgress
from app.services.studio import StudioManager
from app.services.studio_seed import StudioNovelInput, create_studio_novel


def custom_config(novel_id=None, **overrides):
    return RunInput(novel_id=novel_id or uuid.uuid4(), mode="live", provider="custom_input",
        model="my-custom-model", custom_base_url="https://gateway.example/v1/chat/completions", **overrides)


def test_incoming_secret_excluded_and_full_completion_url_normalized():
    payload = StartRunInput(**custom_config().model_dump(), api_key="test-secret")
    assert payload.custom_base_url == "https://gateway.example/v1"
    assert "test-secret" not in payload.model_dump_json()
    assert "api_key" not in payload.model_dump()
    assert "test-secret" not in repr(payload)
    for url in ["file:///tmp/x", "https://user:pass@example.test/v1", "https://example.test/v1?api_key=bad"]:
        with pytest.raises(ValidationError):
            custom_config().model_copy().model_validate({**custom_config().model_dump(), "custom_base_url": url})


async def test_custom_agent_uses_entered_key_and_preserves_config_isolation(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVEL_LLM_API_KEY", "test-unrelated-key")
    settings = Settings(_env_file=None)
    manager = StudioManager(tmp_path / "runs", None, settings)
    config = custom_config()
    run = StudioRun(id="custom", novel_id=str(config.novel_id), title="Custom", mode="live", status="running",
        current_chapter=1, start_chapter=1, total_chapters=1, chapters=[ChapterProgress(number=1)], config=config)
    effective = manager._effective_settings(run)
    registry = build_registry(effective, api_keys={"custom_input": SecretStr("test-entered-key")})
    provider = registry.get("custom_input")
    def complete(request):
        assert str(request.url) == "https://gateway.example/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer test-entered-key"
        assert json.loads(request.content)["model"] == "my-custom-model"
        return httpx.Response(200, json={"choices": [{"message": {"content": "自定义模型正文"}, "finish_reason": "stop"}]})
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(complete))
    try:
        for role in ("director", "writer", "memory_extractor", "literary_reviewer"):
            assert registry.client_for_agent(role, effective).provider is provider
        result = await registry.client_for_agent("writer", effective).generate_text(system=None, user="写一段。")
        assert result.text == "自定义模型正文"
        assert "test-entered-key" not in result.model_dump_json()
        assert "custom_input" not in settings.llm.providers
    finally:
        await provider.aclose()
        await manager.close()


async def test_http_custom_start_redacts_key_and_restart_accepts_new_key(engine, tmp_path):
    maker = async_sessionmaker(engine, expire_on_commit=False)
    root = tmp_path / "runs"
    manager = StudioManager(root, maker, Settings(_env_file=None))
    entered_key = "test-runtime-secret-123"
    async with maker() as session:
        novel = await create_studio_novel(session, StudioNovelInput(
            title="自定义服务试验", synopsis="只做离线接入验证。", protagonist="许知遥"))
        await session.commit()
        novel_id = novel.id
    async def failed_factory(session, run, chapter):
        # Even exceptions from outside the provider are scrubbed.
        raise RuntimeError("mock provider failed: " + entered_key)
    manager.service_factory = failed_factory
    app = create_app()
    app.state.studio = manager
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        body = {**custom_config(novel_id).model_dump(mode="json"), "api_key": entered_key}
        response = await client.post("/api/v1/studio/runs", json=body)
        assert response.status_code == 201, response.text
        rid = response.json()["id"]
        await manager.tasks[rid]
        assert manager.get(rid).status == "blocked"
        assert "REDACTED" in manager.get(rid).error
        assert entered_key not in (await client.get("/api/v1/studio/bootstrap")).text
        for path in root.rglob("*.json*"):
            assert entered_key not in path.read_text(), path
        assert entered_key not in response.text
    await manager.close()
    assert not manager._custom_keys
    restored = StudioManager(root, maker, Settings(_env_file=None))
    try:
        run = restored.get(rid)
        assert run.requires_api_key
        with pytest.raises(ValueError, match="重新输入"):
            restored._validate_provider(run.config)
        app.state.studio = restored
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(f"/api/v1/studio/runs/{rid}/credentials", json={"api_key": "test-replacement-key"})
            assert response.status_code == 200
            assert not response.json()["requires_api_key"]
            assert "test-replacement-key" not in response.text
        assert restored._custom_keys[rid].get_secret_value() == "test-replacement-key"
        for path in root.rglob("*.json*"):
            assert "test-replacement-key" not in path.read_text()
    finally:
        await restored.close()


async def test_custom_runs_have_independent_keys_and_use_actual_service_factory(tmp_path):
    manager = StudioManager(tmp_path / "runs", None, Settings(_env_file=None))
    try:
        for index in (1, 2):
            config = custom_config()
            run = StudioRun(id=f"run{index}", novel_id=str(config.novel_id), title="Custom", mode="live", status="running",
                current_chapter=1, start_chapter=1, total_chapters=1, chapters=[ChapterProgress(number=1)], config=config)
            manager.runs[run.id] = run
            manager._path(run).mkdir()
            manager._custom_keys[run.id] = SecretStr(f"test-key-{index}")
            service, reviewer, providers = await manager._services(None, run, run.chapters[0])
            try:
                assert service.draft_service.writer.llm.provider._get_api_key() == f"test-key-{index}"
                assert reviewer.llm.provider._get_api_key() == f"test-key-{index}"
            finally:
                for provider in providers:
                    await provider.aclose()
    finally:
        await manager.close()
