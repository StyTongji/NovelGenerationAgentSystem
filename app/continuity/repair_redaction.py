"""Repair redaction (Phase 6 Fix A).

Leak-category issues and unclassified issues (which may contain leaks)
must NOT hand the offending prose to the repair writer: the original text
itself carries the secret. Before building the repair prompt, the offending
evidence spans are redacted at sentence granularity:

    他终于明白，父亲仍然活着，并已经进入轮回秘境。
    -> 他终于明白，<REDACTED_PROTECTED_INFORMATION>。

If the span cannot be located, redaction widens to every sentence that
contains a protected statement, and finally to the whole scene
(conservative: never keep the secret in the repair prompt).
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from app.continuity.models import ContinuityIssue

REDACTED_MARKER = "<REDACTED_PROTECTED_INFORMATION>"

LEAK_CATEGORIES = {"knowledge_leak", "reveal_policy", "other"}

_SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?\n])")


class RedactionRecord(BaseModel):
    """Author-side record. Never serialized into any writer prompt."""

    issue_id: str
    category: str
    start: int
    end: int
    note: str


class RedactedSceneForRepair(BaseModel):
    scene_id: str
    redacted_text: str
    redactions: list[RedactionRecord] = Field(default_factory=list)
    redacted_any: bool = False


def _sentence_bounds(text: str, start: int, end: int) -> tuple[int, int]:
    """Widen a span to enclosing sentence boundaries (。！？!? or newline)."""
    left = 0
    for i in range(min(start, len(text)) - 1, -1, -1):
        if text[i] in "。！？!?\n":
            left = i + 1
            break
    right = len(text)
    for i in range(end, len(text)):
        if text[i] in "。！？!?\n":
            right = i + 1
            break
    return left, right


def redact_scene_for_repair(
    scene_id: str,
    original_text: str,
    issues: list[ContinuityIssue],
    protected_statements: list[str] | None = None,
) -> RedactedSceneForRepair:
    """Build the redacted scene text for leak-category issues.

    Known non-leak categories do not trigger redaction. An ``other`` issue
    is redacted because the checker may have left a secret unclassified.
    """
    redactions: list[RedactionRecord] = []
    text = original_text
    leak_issues = [i for i in issues if i.category.value in LEAK_CATEGORIES]
    if not leak_issues:
        return RedactedSceneForRepair(
            scene_id=scene_id, redacted_text=text, redactions=[], redacted_any=False
        )

    offset_shift = 0
    for issue in leak_issues:
        span = issue.evidence_excerpt
        span = span.strip() if span else ""
        located = False
        if span:
            position = text.find(span)
            if position != -1:
                start, end = _sentence_bounds(text, position, position + len(span))
                text = text[:start] + REDACTED_MARKER + text[end:]
                redactions.append(
                    RedactionRecord(
                        issue_id=issue.issue_id,
                        category=issue.category.value,
                        start=start,
                        end=end,
                        note="evidence span widened to sentence",
                    )
                )
                located = True
        if not located and protected_statements:
            # fallback: redact every sentence containing a protected statement
            for statement in protected_statements:
                position = text.find(statement)
                while position != -1:
                    start, end = _sentence_bounds(text, position, position + len(statement))
                    text = text[:start] + REDACTED_MARKER + text[end:]
                    redactions.append(
                        RedactionRecord(
                            issue_id=issue.issue_id,
                            category=issue.category.value,
                            start=start,
                            end=end,
                            note="protected statement sentence redacted",
                        )
                    )
                    position = text.find(statement, start + len(REDACTED_MARKER))

    if not redactions:
        # cannot locate anything reliably -> conservative: redact everything
        text = REDACTED_MARKER
        redactions.append(
            RedactionRecord(
                issue_id=leak_issues[0].issue_id,
                category=leak_issues[0].category.value,
                start=0,
                end=len(original_text),
                note="span unlocatable; whole scene redacted (conservative)",
            )
        )

    return RedactedSceneForRepair(
        scene_id=scene_id,
        redacted_text=text,
        redactions=redactions,
        redacted_any=True,
    )
