"""ContinuityIssueMerger: dedupe and merge deterministic + LLM findings.

Merge policy (Phase 6 §19-21 + Phase 7 Fix B):
- deterministic findings are never downgraded or dropped by the LLM
- exact key match: (category, scene_id, primary entity, normalized evidence)
- fallback for LLM issues without stable refs: evidence overlap with a
  deterministic issue of the same category+scene (containment or >= 8
  shared normalized characters)
- principle: rather keep two real issues than wrongly swallow one
"""

from __future__ import annotations

import re

from app.continuity.models import ContinuityIssue
from app.models.enums import Severity

_SEVERITY_ORDER = {
    Severity.info: 0,
    Severity.warning: 1,
    Severity.error: 2,
    Severity.critical: 3,
}

_NORMALIZE = re.compile(r"[\s，。、！？：；,.!?;:<>\"'()（）\[\]【】-]+")


def _norm_evidence(text: str | None) -> str:
    return _NORMALIZE.sub("", text or "")


def _evidence_overlap(a: str | None, b: str | None) -> bool:
    na, nb = _norm_evidence(a), _norm_evidence(b)
    if not na or not nb:
        return False
    if na in nb or nb in na:
        return True
    window = 8
    if len(na) < window or len(nb) < window:
        return na == nb
    return any(na[i : i + window] in nb for i in range(len(na) - window + 1))


class ContinuityIssueMerger:
    @staticmethod
    def _absorb(base: ContinuityIssue, incoming: ContinuityIssue) -> None:
        """Merge an incoming finding into the base (deterministic wins)."""
        if _SEVERITY_ORDER[incoming.severity] > _SEVERITY_ORDER[base.severity]:
            base.severity = incoming.severity
        base.blocking = base.severity in (Severity.error, Severity.critical)
        if incoming.evidence_excerpt and incoming.evidence_excerpt not in (base.evidence_excerpt or ""):
            base.evidence_excerpt = ((base.evidence_excerpt or "") + " | " + incoming.evidence_excerpt)[:400]
        if base.suggested_action is None and incoming.suggested_action:
            base.suggested_action = incoming.suggested_action

    def merge(
        self, deterministic: list[ContinuityIssue], llm: list[ContinuityIssue]
    ) -> list[ContinuityIssue]:
        merged: dict[tuple, ContinuityIssue] = {}
        source_map: dict[tuple, set[str]] = {}
        deterministic_issues: list[ContinuityIssue] = []

        for issue in deterministic:
            key = issue.merge_key()
            merged[key] = issue.model_copy(deep=True)
            source_map[key] = {"deterministic"}
            deterministic_issues.append(issue)

        for issue in llm:
            key = issue.merge_key()
            if key in merged:
                self._absorb(merged[key], issue)
                source_map[key].add("llm")
                continue
            fallback = next(
                (
                    det
                    for det in deterministic_issues
                    if det.category == issue.category
                    and det.scene_id == issue.scene_id
                    and _evidence_overlap(det.evidence_excerpt, issue.evidence_excerpt)
                ),
                None,
            )
            if fallback is not None:
                fkey = fallback.merge_key()
                self._absorb(merged[fkey], issue)
                source_map[fkey].add("llm")
            else:
                merged[key] = issue.model_copy(deep=True)
                source_map[key] = {issue.source}

        out: list[ContinuityIssue] = []
        for key, issue in merged.items():
            sources = source_map[key]
            if len(sources) == 1:
                issue.source = next(iter(sources))
            out.append(issue)
        out.sort(key=lambda i: (-_SEVERITY_ORDER[i.severity], i.category.value, i.scene_id or ""))
        return out
