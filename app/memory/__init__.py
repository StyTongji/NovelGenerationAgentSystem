"""Memory extraction layer: contracts, evidence validation, domain
validation, fingerprint dedupe, pure-domain dry-run."""

from app.memory.models import (
    EvidenceRef,
    EvidenceSpan,
    FactRef,
    MemoryUpdateBatch,
    MemoryUpdateOperation,
    MemoryUpdateType,
    MemoryVisibility,
)
__all__ = [
    "EvidenceSpan",
    "EvidenceRef",
    "FactRef",
    "MemoryUpdateBatch",
    "MemoryUpdateOperation",
    "MemoryUpdateType",
    "MemoryVisibility",
]


def __getattr__(name):
    """Keep package imports lazy so draft/schema modules do not cycle."""
    if name == "validate_evidence_spans":
        from app.memory.evidence import validate_evidence_spans
        return validate_evidence_spans
    if name == "MemoryUpdateValidator":
        from app.memory.validator import MemoryUpdateValidator
        return MemoryUpdateValidator
    if name in {"MemoryDryRun", "MemoryDryRunResult", "OperationDryRunResult"}:
        from app.memory.dry_run import MemoryDryRun, MemoryDryRunResult, OperationDryRunResult
        return locals()[name]
    raise AttributeError(name)


__all__ += [
    "validate_evidence_spans",
    "MemoryUpdateValidator",
    "MemoryDryRun",
    "MemoryDryRunResult",
    "OperationDryRunResult",
]
