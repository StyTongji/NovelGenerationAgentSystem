"""MemoryExtractionService: clean draft -> candidate deltas -> validated
preview. NO APPLY, NO COMMIT — Phase 8 implements CanonicalCommitService.

    verify continuity (valid + draft_hash match; else reject, zero LLM calls)
        -> MemoryExtractionContext
        -> MemoryExtractorAgent (structured)
        -> fingerprint dedupe
        -> MemoryUpdateValidator
        -> MemoryDryRun (pure domain simulation)
        -> MemoryExtractionResult
"""

from __future__ import annotations

import hashlib
import uuid

from pydantic import BaseModel, Field

from app.agents.memory_extractor import MemoryExtractorAgent
from app.context.builder import ContextBuilder
from app.continuity.models import ContinuityReport
from app.draft.models import ChapterDraft
from app.llm.models import GenerationMetadata
from app.memory.catalog import EvidenceCatalog, EvidenceCatalogBuilder, resolve_evidence_refs
from app.memory.dry_run import MemoryDryRun, MemoryDryRunResult
from app.memory.models import MemoryUpdateBatch
from app.memory.validator import MemoryValidationReport, MemoryUpdateValidator
from app.schemas.agent_outputs import ChapterPlan


class StaleContinuityReportError(Exception):
    """The continuity report does not match the current draft."""


class BlockingContinuityError(Exception):
    """The draft still has blocking continuity issues."""


class MemoryExtractionResult(BaseModel):
    batch: MemoryUpdateBatch
    validation_report: MemoryValidationReport
    draft_hash: str
    context_hash: str
    generation_metadata: GenerationMetadata | None = None
    dry_run: MemoryDryRunResult | None = None
    evidence_catalog: EvidenceCatalog | None = None


class MemoryExtractionService:
    def __init__(
        self, session, extractor: MemoryExtractorAgent | None = None,
        *, require_evidence_refs: bool = False,
    ) -> None:
        self.session = session
        self.extractor = extractor
        self.require_evidence_refs = require_evidence_refs
        self.builder = ContextBuilder(session)
        self.validator = MemoryUpdateValidator(session)
        self.dry_run = MemoryDryRun()

    @staticmethod
    def _draft_hash(draft: ChapterDraft) -> str:
        return hashlib.sha256(draft.full_text.encode("utf-8")).hexdigest()

    async def extract_preview(
        self,
        novel_id: uuid.UUID,
        draft: ChapterDraft,
        chapter_plan: ChapterPlan,
        continuity_report: ContinuityReport,
        *,
        scene_plans=None,
        review_feedback=None,
    ) -> MemoryExtractionResult:
        # provenance gates (§6/#67/#68): client claims are never trusted
        if str(draft.novel_id) != str(novel_id):
            raise StaleContinuityReportError(
                f"Draft novel_id {draft.novel_id} does not match path {novel_id}"
            )
        current_hash = self._draft_hash(draft)
        if continuity_report.draft_hash != current_hash:
            raise StaleContinuityReportError(
                "ContinuityReport is stale: report.draft_hash="
                f"{continuity_report.draft_hash[:12]} but current draft hash="
                f"{current_hash[:12]}"
            )
        if not continuity_report.valid:
            raise BlockingContinuityError(
                "Draft still has blocking continuity issues; extraction refused"
            )

        context = await self.builder.build_extraction_context(
            novel_id,
            chapter_number=draft.chapter_number,
            chapter_text=draft.full_text,
            chapter_plan=chapter_plan,
            scene_plans=scene_plans if scene_plans is not None else list(draft.scene_plans),
            draft=draft,
            draft_hash=current_hash,
        )

        if review_feedback:
            batch, metadata = await self.extractor.extract(context, review_feedback=review_feedback)
        else:
            batch, metadata = await self.extractor.extract(context)
        catalog = context.evidence_catalog or EvidenceCatalogBuilder.build(draft, current_hash)
        pre_report = await self.validator.validate(
            batch, context, draft, require_evidence_refs=self.require_evidence_refs,
        )
        batch = resolve_evidence_refs(batch, catalog)
        batch = self._dedupe_by_fingerprint(batch, context)

        report = await self.validator.validate(
            batch, context, draft,
            require_evidence_refs=self.require_evidence_refs,
        )
        if not pre_report.valid:
            report = pre_report

        dry_run_result = None
        if report.valid:
            from app.context.models import MemoryExtractionContext as _M  # noqa: F401

            before_state = await self._before_state(novel_id, draft.chapter_number, context)
            dry_run_result = self.dry_run.run(batch, before_state)

        return MemoryExtractionResult(
            batch=batch,
            validation_report=report,
            draft_hash=current_hash,
            context_hash=context.context_hash,
            generation_metadata=metadata,
            dry_run=dry_run_result,
            evidence_catalog=catalog,
        )

    async def validate_batch(
        self,
        novel_id: uuid.UUID,
        draft: ChapterDraft,
        chapter_plan: ChapterPlan,
        batch: MemoryUpdateBatch,
        *,
        dry_run: bool = False,
    ) -> MemoryExtractionResult:
        """Re-validate a (possibly user-edited) candidate batch. Still no apply."""
        current_hash = self._draft_hash(draft)
        context = await self.builder.build_extraction_context(
            novel_id,
            chapter_number=draft.chapter_number,
            chapter_text=draft.full_text,
            chapter_plan=chapter_plan,
            scene_plans=list(draft.scene_plans),
            draft=draft,
            draft_hash=current_hash,
        )
        catalog = context.evidence_catalog or EvidenceCatalogBuilder.build(draft, current_hash)
        pre_report = await self.validator.validate(
            batch, context, draft, require_evidence_refs=self.require_evidence_refs,
        )
        batch = resolve_evidence_refs(batch, catalog)
        batch = self._dedupe_by_fingerprint(batch, context)
        report = await self.validator.validate(
            batch, context, draft,
            require_evidence_refs=self.require_evidence_refs,
        )
        if not pre_report.valid:
            report = pre_report
        dry_run_result = None
        if dry_run and report.valid:
            before_state = await self._before_state(novel_id, draft.chapter_number, context)
            dry_run_result = self.dry_run.run(batch, before_state)
        return MemoryExtractionResult(
            batch=batch,
            validation_report=report,
            draft_hash=current_hash,
            context_hash=context.context_hash,
            dry_run=dry_run_result,
            evidence_catalog=catalog,
        )

    # ------------------------------------------------------------ internals

    @staticmethod
    def _dedupe_by_fingerprint(batch: MemoryUpdateBatch, context) -> MemoryUpdateBatch:
        seen: set[str] = set()
        unique_ops = []
        for operation in batch.operations:
            fp = operation.fingerprint(context.novel_id, context.draft_hash)
            if fp in seen:
                continue
            seen.add(fp)
            unique_ops.append(operation)
        return batch.model_copy(
            update={"operations": unique_ops},
        )

    async def _before_state(self, novel_id: uuid.UUID, chapter_number: int, context) -> dict:
        from app.memory.dry_run import build_memory_before_state
        return await build_memory_before_state(self.session, novel_id, chapter_number, context)
