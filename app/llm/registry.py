"""Provider registry + agent model resolution.

Business code asks the registry for a provider by name (or by agent role);
it never references a vendor SDK. Everything comes from configuration:

    llm:
      providers:
        default:
          type: openai_compatible
          base_url: ...
          api_key_env: NOVEL_LLM_API_KEY
      agents:
        director:
          provider: default
          model: xxx
          temperature: 0.4
"""

from __future__ import annotations

from pydantic import SecretStr

from app.config import AgentModelConfig, LLMProviderConfig, Settings
from app.llm.base import BaseLLMProvider
from app.llm.client import AgentLLMClient
from app.llm.errors import ProviderConfigurationError

# default temperatures per AGENTS.md §8
DEFAULT_AGENT_TEMPERATURES: dict[str, float] = {
    "director": 0.5,
    "scene_planner": 0.5,
    "writer": 0.85,
    "memory_extractor": 0.1,
    "continuity_checker": 0.1,
    "literary_reviewer": 0.3,
}

_PROVIDER_TYPES: dict[str, type[BaseLLMProvider]] = {}


def register_provider_type(type_name: str, provider_cls: type[BaseLLMProvider]) -> None:
    _PROVIDER_TYPES[type_name] = provider_cls


def _known_provider_types() -> dict[str, type[BaseLLMProvider]]:
    if not _PROVIDER_TYPES:
        from app.llm.fake import FakeLLMProvider
        from app.llm.openai_compatible import OpenAICompatibleProvider

        register_provider_type("openai_compatible", OpenAICompatibleProvider)
        register_provider_type("fake", FakeLLMProvider)
    return dict(_PROVIDER_TYPES)


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, BaseLLMProvider] = {}

    def register(self, name: str, provider: BaseLLMProvider) -> None:
        self._providers[name] = provider

    def get(self, name: str) -> BaseLLMProvider:
        try:
            return self._providers[name]
        except KeyError:
            raise ProviderConfigurationError(
                f"Unknown LLM provider {name!r}; registered: {sorted(self._providers)}"
            ) from None

    def names(self) -> list[str]:
        return sorted(self._providers)

    # ---------------------------------------------------------------- agents

    def resolve_agent_config(self, agent: str, settings: Settings) -> AgentModelConfig:
        configured = settings.llm.agents.get(agent)
        if configured is not None:
            provider_name = configured.provider or settings.llm.default_provider
            return AgentModelConfig(
                provider=provider_name,
                model=configured.model,
                temperature=(
                    configured.temperature
                    if configured.temperature != 0.5 or agent not in DEFAULT_AGENT_TEMPERATURES
                    else DEFAULT_AGENT_TEMPERATURES.get(agent, configured.temperature)
                ),
                max_tokens=configured.max_tokens,
            )
        return AgentModelConfig(
            provider=settings.llm.default_provider,
            model="",
            temperature=DEFAULT_AGENT_TEMPERATURES.get(agent, 0.5),
        )

    def client_for_agent(
        self,
        agent: str,
        settings: Settings,
        *,
        prompt_version: str | None = None,
    ) -> AgentLLMClient:
        """Build an AgentLLMClient bound to the configured provider/model."""
        config = self.resolve_agent_config(agent, settings)
        provider = self.get(config.provider)
        return AgentLLMClient(
            provider=provider,
            agent_name=agent,
            model=config.model,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            prompt_version=prompt_version,
            deadline_seconds=getattr(provider, "deadline_seconds", 240.0),
        )


def build_registry(settings: Settings, *, include_fake: bool = False,
                   api_keys: dict[str, SecretStr] | None = None) -> ProviderRegistry:
    """Construct providers from settings.

    OpenAI-compatible providers are constructed eagerly but authenticate
    lazily (api key read at call time), so a missing key does not break
    app startup — only an actual call.
    """
    registry = ProviderRegistry()
    known = _known_provider_types()

    for name, cfg in (settings.llm.providers or {}).items():
        config = LLMProviderConfig.model_validate(cfg)
        cls = known.get(config.type)
        if cls is None:
            raise ProviderConfigurationError(
                f"Unknown provider type {config.type!r} for provider {name!r}"
            )
        if cls.__name__ == "OpenAICompatibleProvider":
            from app.llm.openai_compatible import OpenAICompatibleProvider

            registry.register(
                name,
                OpenAICompatibleProvider(
                    base_url=config.base_url,
                    api_key_env=config.api_key_env,
                    api_key=(api_keys or {}).get(name),
                    model=config.model,
                    timeout_seconds=config.timeout_seconds,
                    connect_timeout_seconds=config.connect_timeout_seconds,
                    deadline_seconds=config.deadline_seconds,
                    max_retries=config.max_retries,
                ),
            )
        else:
            registry.register(name, cls())

    if include_fake and "fake" not in registry.names():
        from app.llm.fake import FakeLLMProvider

        registry.register("fake", FakeLLMProvider())
    return registry
