"""Evidence span validation (Phase 7 §40-§44).

An EvidenceSpan proves a mutation came from the draft. Validation rules:

- the referenced scene must exist in the draft
- ``exact_quote`` must be a substring of that scene's text
- when start/end are provided they must match the quote; the validator
  re-locates the quote itself (model-provided positions are not trusted)
- multiple occurrences are recorded, never silently resolved to the first
- plan text / canonical context can never serve as evidence: only the
  draft scene text is searched
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.draft.models import ChapterDraft
from app.memory.models import EvidenceSpan
from app.models.enums import Severity


@dataclass
class EvidenceIssue:
    code: str
    severity: Severity
    operation_id: str
    message: str


@dataclass
class EvidenceValidationResult:
    issues: list[EvidenceIssue] = field(default_factory=list)
    # operation_id -> resolved offsets per span (author-side provenance)
    resolved: dict[str, list[tuple[int, int]]] = field(default_factory=dict)

    @property
    def valid(self) -> bool:
        return not any(i.severity in (Severity.error, Severity.critical) for i in self.issues)


def validate_evidence_refs(operations, catalog) -> list[EvidenceIssue]:
    """Validate model-selected evidence ids before server resolution."""
    issues: list[EvidenceIssue] = []
    if catalog is None:
        return issues
    entries = catalog.by_id()
    for operation in operations:
        for ref in operation.evidence_refs:
            entry = entries.get(ref.evidence_id)
            if entry is None:
                issues.append(EvidenceIssue(
                    code="evidence_ref_unknown", severity=Severity.critical,
                    operation_id=operation.operation_id,
                    message=f"Evidence ref {ref.evidence_id!r} is not in the catalog",
                ))
            elif entry.scene_id != operation.source_scene_id:
                issues.append(EvidenceIssue(
                    code="evidence_ref_wrong_scene", severity=Severity.critical,
                    operation_id=operation.operation_id,
                    message=(f"Evidence ref {ref.evidence_id!r} belongs to scene "
                             f"{entry.scene_id!r}, not source scene {operation.source_scene_id!r}"),
                ))
    return issues


def validate_evidence_spans(operations, draft: ChapterDraft) -> EvidenceValidationResult:
    result = EvidenceValidationResult()
    scenes = {s.scene_id: s for s in draft.scenes}

    for operation in operations:
        op_id = operation.operation_id
        if not operation.evidence:
            result.issues.append(
                EvidenceIssue(
                    code="missing_evidence", severity=Severity.critical, operation_id=op_id,
                    message="Every mutation requires at least one draft evidence span",
                )
            )
            continue

        resolved: list[tuple[int, int]] = []
        for span in operation.evidence:
            scene = scenes.get(span.scene_id)
            if scene is None:
                result.issues.append(
                    EvidenceIssue(
                        code="evidence_scene_unknown", severity=Severity.critical,
                        operation_id=op_id,
                        message=f"Evidence scene {span.scene_id!r} does not exist in the draft",
                    )
                )
                continue

            quote = span.exact_quote.strip()
            if not quote:
                result.issues.append(
                    EvidenceIssue(
                        code="empty_evidence_quote", severity=Severity.critical,
                        operation_id=op_id,
                        message="Evidence quote is empty",
                    )
                )
                continue
            if quote not in scene.text:
                # covers hallucinated evidence AND plan/canonical text leaks:
                # plan text is not part of the draft, so it cannot match
                result.issues.append(
                    EvidenceIssue(
                        code="evidence_not_in_draft", severity=Severity.critical,
                        operation_id=op_id,
                        message=(
                            "Evidence quote not found in the referenced scene; "
                            "evidence must be a verbatim draft sentence"
                        ),
                    )
                )
                continue

            offsets = _all_offsets(scene.text, quote)
            if span.start_char is not None and span.end_char is not None:
                provided = (span.start_char, span.end_char)
                if provided in offsets:
                    resolved.append(provided)
                else:
                    # positions are untrusted: re-locate from the quote itself
                    result.issues.append(
                        EvidenceIssue(
                            code="evidence_position_corrected", severity=Severity.warning,
                            operation_id=op_id,
                            message="Provided char offsets did not match the quote; "
                            "re-located from the quote text",
                        )
                    )
                    resolved.extend(offsets)
            elif len(offsets) > 1:
                result.issues.append(
                    EvidenceIssue(
                        code="ambiguous_evidence", severity=Severity.warning,
                        operation_id=op_id,
                        message=f"Quote appears {len(offsets)} times in the scene; "
                        "all candidate offsets recorded",
                    )
                )
                resolved.extend(offsets)
            else:
                resolved.extend(offsets)
        result.resolved[op_id] = resolved
    return result


def _all_offsets(text: str, quote: str) -> list[tuple[int, int]]:
    offsets: list[tuple[int, int]] = []
    start = text.find(quote)
    while start != -1:
        offsets.append((start, start + len(quote)))
        start = text.find(quote, start + 1)
    return offsets
