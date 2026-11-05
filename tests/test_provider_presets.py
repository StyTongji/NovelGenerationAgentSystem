"""Provider selection must use the right endpoint, model ID and account key."""
import json

import httpx
import pytest

from app.config import Settings
from app.llm.errors import ProviderConfigurationError
from app.llm.registry import build_registry
from app.services.studio import StudioManager


@pytest.mark.parametrize("name,host,path,model,key_env", [
    ("deepseek", "api.deepseek.com", "/chat/completions", "deepseek-flash", "NOVEL_DEEPSEEK_API_KEY"),
    ("scnet", "api.scnet.cn", "/api/llm/v1/chat/completions", "DeepSeek-V4.1-Flash-1017", "NOVEL_LLM_API_KEY"),
])
async def test_agent_request_routes_to_selected_provider(monkeypatch, tmp_path, name, host, path, model, key_env):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NOVEL_DEEPSEEK_API_KEY", "test-official-account")
    monkeypatch.setenv("NOVEL_LLM_API_KEY", "test-scnet-account")
    settings = Settings(_env_file=None)
    settings.llm.default_provider = name
    registry = build_registry(settings)
    provider = registry.get(name)
    expected_key = "test-official-account" if name == "deepseek" else "test-scnet-account"
    def complete(request):
        assert request.url.host == host and request.url.path == path
        assert request.headers["Authorization"] == "Bearer " + expected_key
        body = json.loads(request.content)
        assert body["model"] == model
        return httpx.Response(200, json={"id": "mock-request", "model": model,
            "choices": [{"message": {"content": "场景正文"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(complete))
    try:
        result = await registry.client_for_agent("writer", settings).generate_text(system=None, user="写一个场景。")
        assert result.text == "场景正文"
    finally:
        await provider.aclose()


def test_existing_partial_provider_overrides_keep_both_presets():
    settings = Settings(_env_file=None, llm={"providers": {
        "scnet": {"deadline_seconds": 90},
        "custom": {"base_url": "https://custom.example/v1", "api_key_env": "CUSTOM_KEY", "model": "custom-model"},
    }})
    assert settings.llm.providers["scnet"].deadline_seconds == 90
    assert settings.llm.providers["scnet"].base_url == "https://api.scnet.cn/api/llm/v1"
    assert settings.llm.providers["deepseek"].model == "deepseek-flash"
    assert settings.llm.providers["custom"].api_key_env == "CUSTOM_KEY"


async def test_dotenv_keys_readiness_and_provider_auth_agree(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for name in ("NOVEL_DEEPSEEK_API_KEY", "NOVEL_LLM_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / ".env").write_text("NOVEL_DEEPSEEK_API_KEY=test-official-dotenv\nNOVEL_LLM_API_KEY=test-scnet-dotenv\n")
    settings = Settings()
    manager = StudioManager(tmp_path / "runs", None, settings)
    try:
        rows = {row["id"]: row for row in manager.providers()}
        assert rows["deepseek"]["configured"] and rows["scnet"]["configured"]
        assert "Auto" not in rows["deepseek"]["models"]
        assert "deepseek-flash" not in rows["scnet"]["models"]
        assert "test-official-dotenv" not in json.dumps(rows)
        registry = build_registry(settings)
        assert registry.get("deepseek")._get_api_key() == "test-official-dotenv"
        assert registry.get("scnet")._get_api_key() == "test-scnet-dotenv"
        monkeypatch.setenv("NOVEL_DEEPSEEK_API_KEY", "test-env-preferred")
        assert registry.get("deepseek")._get_api_key() == "test-env-preferred"
        monkeypatch.setenv("NOVEL_DEEPSEEK_API_KEY", "")
        assert not next(row for row in manager.providers() if row["id"] == "deepseek")["configured"]
        with pytest.raises(ProviderConfigurationError):
            registry.get("deepseek")._get_api_key()
    finally:
        await manager.close()


async def test_scnet_key_never_authenticates_official_service(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("NOVEL_DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("NOVEL_LLM_API_KEY", "test-scnet-only")
    settings = Settings(_env_file=None)
    manager = StudioManager(tmp_path / "runs", None, settings)
    try:
        rows = {row["id"]: row for row in manager.providers()}
        assert rows["scnet"]["configured"] and not rows["deepseek"]["configured"]
        with pytest.raises(ProviderConfigurationError, match="NOVEL_DEEPSEEK_API_KEY"):
            build_registry(settings).get("deepseek")._get_api_key()
    finally:
        await manager.close()
