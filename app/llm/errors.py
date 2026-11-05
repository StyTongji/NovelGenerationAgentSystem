"""LLM error hierarchy.

Every failure mode has a distinct exception so callers (agents, chapter
service) can decide between repair, retry, or hard failure without
string-matching on messages.

Security rule: exceptions must never embed API keys. Providers must scrub
secrets before raising (see ``scrub_secrets``).
"""

from __future__ import annotations


class LLMError(Exception):
    """Base class for all LLM runtime errors."""


class ProviderConfigurationError(LLMError):
    """Provider misconfigured: missing api key / bad base_url / unknown name."""


class ProviderRequestError(LLMError):
    """The provider call itself failed (HTTP error, connection failure).

    ``status_code`` is set when the failure came from an HTTP response.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class ProviderTimeoutError(ProviderRequestError):
    """The provider call exceeded its timeout."""


class StructuredOutputError(LLMError):
    """Could not obtain a valid structured output.

    Carries every raw attempt so callers / logs can inspect what the model
    actually produced.
    """

    def __init__(
        self,
        message: str,
        *,
        raw_outputs: list[str] | None = None,
        errors: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.raw_outputs = raw_outputs or []
        self.errors = errors or []


class JSONExtractionError(StructuredOutputError):
    """No JSON object could be extracted from a single raw response."""


class StructuredValidationError(StructuredOutputError):
    """A single attempt produced JSON but it failed Pydantic validation."""


class RetryExhaustedError(StructuredOutputError):
    """All structured-generation attempts (initial + repairs) failed."""

    def __init__(
        self,
        message: str,
        *,
        attempts: int,
        raw_outputs: list[str] | None = None,
        errors: list[str] | None = None,
    ) -> None:
        super().__init__(message, raw_outputs=raw_outputs, errors=errors)
        self.attempts = attempts


class PromptError(LLMError):
    """Prompt loading / rendering failure."""


def scrub_secrets(text: str, *secrets: str | None) -> str:
    """Remove secret values from a message before it is raised or logged."""
    result = text
    for secret in secrets:
        if secret:
            result = result.replace(secret, "***REDACTED***")
    return result
