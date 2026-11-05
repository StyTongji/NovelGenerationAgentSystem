"""Continuity layer: hybrid checker, merger, writer-safe repair projection."""

from app.continuity.deterministic import DeterministicContinuityChecker
from app.continuity.merger import ContinuityIssueMerger
from app.continuity.models import (
    BLOCKING_SEVERITIES,
    ContinuityCategory,
    ContinuityIssue,
    ContinuityRepairInstruction,
    ContinuityRepairRecord,
    ContinuityRepairResult,
    ContinuityReport,
)
from app.continuity.repair_projection import project_issue_to_writer_safe
from app.continuity.repair_redaction import (
    REDACTED_MARKER,
    RedactedSceneForRepair,
    redact_scene_for_repair,
)

__all__ = [
    "DeterministicContinuityChecker",
    "ContinuityIssueMerger",
    "ContinuityCategory",
    "ContinuityIssue",
    "ContinuityReport",
    "ContinuityRepairInstruction",
    "ContinuityRepairRecord",
    "ContinuityRepairResult",
    "BLOCKING_SEVERITIES",
    "project_issue_to_writer_safe",
    "REDACTED_MARKER",
    "RedactedSceneForRepair",
    "redact_scene_for_repair",
]
