"""Prompt loading / registry.

Prompts live in ``prompts/*.md`` (never hardcoded in Python). Files follow
``<name>.md`` or ``<name>_v<N>.md`` naming; ``<name>.md`` is version ``v1``.
Future prompt versions (``director_v2.md``, ``writer_xianxia_v1.md``) are
picked up without code changes. ``{{variable}}`` placeholders are rendered
at call time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from app.llm.errors import PromptError

_NAME_VERSION_PATTERN = re.compile(r"^(?P<name>[a-z0-9_]+?)(?:_v(?P<version>\d+))?\.md$", re.IGNORECASE)
_PLACEHOLDER_PATTERN = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


@dataclass(frozen=True)
class PromptTemplate:
    name: str
    version: str  # "v1", "v2", ...
    content: str

    def render(self, **variables: str) -> str:
        def substitute(match: re.Match[str]) -> str:
            key = match.group(1)
            if key not in variables:
                raise PromptError(f"Missing variable {key!r} for prompt {self.name!r}")
            return variables[key]

        return _PLACEHOLDER_PATTERN.sub(substitute, self.content)


class PromptRegistry:
    def __init__(self) -> None:
        # name -> version -> content
        self._prompts: dict[str, dict[str, PromptTemplate]] = {}

    def register(self, name: str, content: str, version: str = "v1") -> PromptTemplate:
        template = PromptTemplate(name=name, version=version, content=content)
        self._prompts.setdefault(name, {})[version] = template
        return template

    @classmethod
    def from_directory(cls, directory: str | Path) -> PromptRegistry:
        path = Path(directory)
        if not path.is_dir():
            raise PromptError(f"Prompt directory not found: {path}")
        registry = cls()
        for file in sorted(path.glob("*.md")):
            match = _NAME_VERSION_PATTERN.match(file.name)
            if match is None:
                raise PromptError(f"Cannot derive prompt name from file {file.name!r}")
            name = match.group("name")
            version = f"v{match.group('version')}" if match.group("version") else "v1"
            registry.register(name, file.read_text(encoding="utf-8"), version=version)
        return registry

    def get(self, name: str, version: str | None = None) -> PromptTemplate:
        versions = self._prompts.get(name)
        if not versions:
            raise PromptError(
                f"Unknown prompt {name!r}; known: {sorted(self._prompts)}"
            )
        if version is None:
            # highest version wins
            version = sorted(versions.keys(), key=lambda v: int(v[1:]))[-1]
        if version not in versions:
            raise PromptError(
                f"Prompt {name!r} has no version {version!r}; known: {sorted(versions)}"
            )
        return versions[version]

    def render(self, prompt_name: str, version: str | None = None, **variables: str) -> str:
        return self.get(prompt_name, version).render(**variables)

    def names(self) -> list[str]:
        return sorted(self._prompts)


_default_registry: PromptRegistry | None = None


def get_default_registry() -> PromptRegistry:
    """Prompt registry over the repo-level ``prompts/`` directory."""
    global _default_registry
    if _default_registry is None:
        prompts_dir = Path(__file__).resolve().parent.parent.parent / "prompts"
        _default_registry = PromptRegistry.from_directory(prompts_dir)
    return _default_registry
