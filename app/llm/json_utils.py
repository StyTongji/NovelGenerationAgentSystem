"""Extract a JSON object from raw LLM output.

LLMs wrap JSON in markdown fences, prefix it with commentary, or append
trailing text. Extraction must tolerate all of that — but extraction is
NOT validation; Pydantic validation happens later in the provider base.
"""

from __future__ import annotations

import json
from typing import Any

from app.llm.errors import JSONExtractionError


def extract_json_object(raw: str) -> dict[str, Any]:
    """Return the first JSON object found in ``raw``.

    Strategy:
    1. try the whole string (fast path for well-behaved models)
    2. try fenced code blocks (```json ... ```)
    3. try the first balanced {...} span
    """
    text = raw.strip()
    if not text:
        raise JSONExtractionError("Empty response; no JSON to extract", raw_outputs=[raw])

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    for candidate in _fenced_blocks(text):
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue

    candidate = _first_balanced_object(text)
    if candidate is not None:
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    raise JSONExtractionError(
        "No JSON object found in model response", raw_outputs=[raw]
    )


def _fenced_blocks(text: str) -> list[str]:
    blocks: list[str] = []
    lines = text.splitlines()
    buffer: list[str] = []
    inside = False
    for line in lines:
        stripped = line.strip()
        if not inside and stripped.startswith("```"):
            inside = True
            buffer = []
            continue
        if inside and stripped.startswith("```"):
            inside = False
            blocks.append("\n".join(buffer))
            continue
        if inside:
            buffer.append(line)
    return blocks


def _first_balanced_object(text: str) -> str | None:
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None
