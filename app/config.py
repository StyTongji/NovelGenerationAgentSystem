"""Application configuration.

Configuration is layered:
1. defaults below
2. optional ``config.yaml`` (see ``config.example.yaml``)
3. environment variables / ``.env`` (highest priority, prefix ``NOVEL_``)

Business code must never read API keys or model names directly;
everything goes through :class:`Settings`.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class AgentModelConfig(BaseSettings):
    """Model assignment for a single agent role."""

    model_config = SettingsConfigDict(extra="ignore")

    provider: str = ""
    model: str = ""
    temperature: float = 0.5
    max_tokens: int = 4096


class LLMProviderConfig(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    type: str = "openai_compatible"
    base_url: str = "https://api.example.com/v1"
    api_key_env: str = "NOVEL_LLM_API_KEY"
    model: str = ""
    display_name: str = ""
    models: list[str] = Field(default_factory=list)
    timeout_seconds: float = Field(default=120.0, gt=0)
    connect_timeout_seconds: float = Field(default=10.0, gt=0)
    deadline_seconds: float = Field(default=240.0, gt=0)
    max_retries: int = Field(default=2, ge=0, le=2)
    model_capabilities: dict[str, dict[str, bool]] = Field(default_factory=dict)


def builtin_llm_providers() -> dict[str, dict[str, Any]]:
    """Public presets only; credentials remain in the server environment."""
    return {
        "deepseek": {
            "display_name": "DeepSeek 官方",
            "base_url": "https://api.deepseek.com",
            "api_key_env": "NOVEL_DEEPSEEK_API_KEY",
            "model": "deepseek-flash",
            "models": ["deepseek-flash", "deepseek-v4-pro"],
        },
        "scnet": {
            "display_name": "SCNet 超算互联网",
            "base_url": "https://api.scnet.cn/api/llm/v1",
            # Preserve the key variable used by existing SCNet trials.
            "api_key_env": "NOVEL_LLM_API_KEY",
            "model": "DeepSeek-V4.1-Flash-1017",
            "models": ["DeepSeek-V4.1-Flash-1017", "Auto", "DeepSeek-V4-Pro"],
        },
    }


class LLMConfig(BaseSettings):
    default_provider: str = "scnet"
    providers: dict[str, LLMProviderConfig] = Field(default_factory=dict)
    agents: dict[str, AgentModelConfig] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def include_builtin_providers(cls, value):
        if not isinstance(value, dict):
            return value
        data = dict(value)
        providers = builtin_llm_providers()
        for name, configured in (data.get("providers") or {}).items():
            providers[name] = ({**providers.get(name, {}), **configured}
                               if isinstance(configured, dict) else configured)
        data["providers"] = providers
        return data


class RetrievalConfig(BaseSettings):
    vector_backend: str = "in_memory"
    recent_chapters: int = 5
    previous_chapter_tail_chars: int = 1200
    top_k_events: int = 12
    weight_semantic: float = 0.50
    weight_recency: float = 0.25
    weight_importance: float = 0.25


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="NOVEL_",
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
    )

    environment: str = "development"
    api_prefix: str = "/api/v1"

    # async driver (app runtime)
    database_url: str = "sqlite+aiosqlite:///./novel_engine.db"
    # sync driver (alembic)
    sync_database_url: str = "sqlite:///./novel_engine.db"

    llm: LLMConfig = Field(default_factory=LLMConfig)
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)

    generation_scenes_per_chapter_min: int = 3
    generation_scenes_per_chapter_max: int = 6
    generation_default_target_chinese_chars: int = 4500
    generation_max_rewrite_rounds: int = 2
    # Phase 5: scene sizing and continuity
    generation_chapter_target_chars: int = 5000
    generation_scene_min_chars: int = 500
    generation_scene_max_chars: int = 1800
    generation_previous_scene_tail_chars: int = 1500
    generation_max_scene_plan_repairs: int = 2

    continuity_block_finalize_on: list[str] = Field(
        default_factory=lambda: ["critical"]
    )
    continuity_mode: str = "hybrid"  # deterministic_only | hybrid
    continuity_max_repairs: int = 2
    # development-only preview artifact export; empty = disabled
    preview_artifact_dir: str = ""
    studio_directory: str = str(PROJECT_ROOT / "studio_runs")


def _load_yaml_overlay(path: Path | None = None) -> dict[str, Any]:
    config_path = path or PROJECT_ROOT / "config.yaml"
    if not config_path.exists():
        return {}
    with config_path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Build settings once per process; YAML supplies LLM/retrieval blocks."""
    overlay = _load_yaml_overlay()
    # YAML is a lower-priority settings source, not constructor arguments
    # (constructor arguments would override the environment).
    class LayeredSettings(Settings):
        @classmethod
        def settings_customise_sources(cls, settings_cls, init_settings, env_settings,
                                      dotenv_settings, file_secret_settings):
            return (init_settings, env_settings, dotenv_settings,
                    lambda: overlay, file_secret_settings)
    return LayeredSettings()
