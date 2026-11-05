"""LLM runtime: provider abstraction, DTOs, registry, prompt loading.

Boundary: this package only performs model calls. Story state queries,
knowledge filtering and retrieval belong to Context Builder (Phase 3) and
domain services — never here.
"""

from app.llm.base import BaseLLMProvider
from app.llm.client import AgentLLMClient
from app.llm.errors import (
    JSONExtractionError,
    LLMError,
    PromptError,
    ProviderConfigurationError,
    ProviderRequestError,
    ProviderTimeoutError,
    RetryExhaustedError,
    StructuredOutputError,
    StructuredValidationError,
    scrub_secrets,
)
from app.llm.fake import FakeLLMProvider
from app.llm.json_utils import extract_json_object
from app.llm.models import (
    GenerationMetadata,
    LLMMessage,
    StructuredRequest,
    StructuredResult,
    TextRequest,
    TextResult,
    Usage,
)
from app.llm.openai_compatible import OpenAICompatibleProvider
from app.llm.prompts import PromptRegistry, PromptTemplate, get_default_registry
from app.llm.registry import ProviderRegistry, build_registry

__all__ = [
    "BaseLLMProvider",
    "AgentLLMClient",
    "FakeLLMProvider",
    "OpenAICompatibleProvider",
    "ProviderRegistry",
    "build_registry",
    "PromptRegistry",
    "PromptTemplate",
    "get_default_registry",
    "extract_json_object",
    "scrub_secrets",
    "LLMError",
    "ProviderConfigurationError",
    "ProviderRequestError",
    "ProviderTimeoutError",
    "StructuredOutputError",
    "JSONExtractionError",
    "StructuredValidationError",
    "RetryExhaustedError",
    "PromptError",
    "LLMMessage",
    "TextRequest",
    "StructuredRequest",
    "TextResult",
    "StructuredResult",
    "GenerationMetadata",
    "Usage",
]
