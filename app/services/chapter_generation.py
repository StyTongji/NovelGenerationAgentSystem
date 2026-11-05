"""ChapterGenerationService: the product-level entry point (Phase 8).

    generate_preview()  — full preview pipeline, zero canonical mutation
    finalize()          — deterministic revalidation + single-transaction
                          commit via CanonicalCommitService; NEVER calls an
                          LLM (the user approved THIS draft, not a new
                          generation)
"""

from __future__ import annotations

import uuid

from app.commit.models import (
    ChapterCommitRequest,
    ChapterCommitResult,
    ChapterPreviewPackage,
    ReviewApproval,
)
from app.continuity.models import ContinuityRepairResult  # noqa: F401
from app.draft.models import ChapterDraft
from app.planning.models import PlanGenerationResult
from app.services.canonical_commit import CanonicalCommitService
from app.services.chapter_draft import ChapterDraftService
from app.services.chapter_planning import ChapterPlanningService
from app.services.continuity import ContinuityService
from app.services.memory_extraction import MemoryExtractionService


class ContinuityRepairExhaustedError(Exception):
    """Repair could not clear blocking issues; no valid memory preview exists."""


class ChapterGenerationService:
    def __init__(
        self,
        session,
        planning_service: ChapterPlanningService,
        draft_service: ChapterDraftService,
        continuity_service: ContinuityService,
        memory_service: MemoryExtractionService,
        commit_service: CanonicalCommitService,
    ) -> None:
        self.session = session
        self.planning_service = planning_service
        self.draft_service = draft_service
        self.continuity_service = continuity_service
        self.memory_service = memory_service
        self.commit_service = commit_service

    # ---------------------------------------------------------------- preview

    async def generate_preview(
        self,
        novel_id: uuid.UUID,
        user_direction: str | None,
        *,
        chapter_number: int | None = None,
        checkpoints=None,
    ) -> ChapterPreviewPackage:
        from app.workflow.chapter import ChapterWorkflow

        return await ChapterWorkflow(self, checkpoints=checkpoints).run({
            "novel_id": novel_id, "user_direction": user_direction,
            "chapter_number": chapter_number,
        })

    async def revise_preview(
        self, package: ChapterPreviewPackage, replacements: dict[str, str], *, checkpoints=None,
    ) -> ChapterPreviewPackage:
        """Recheck author-edited scenes without replanning or rewriting other scenes."""
        from app.commit.models import PreviewPackageTamperedError, StaleStoryStateError
        from app.planning.provenance import compute_story_state_hash

        expected = ChapterPreviewPackage.compute_hash(
            plan=package.plan_result.plan, draft=package.draft,
            continuity_report=package.continuity_report, memory_result=package.memory_result,
            story_state_hash=package.story_state_hash,
        )
        if expected != package.preview_package_hash:
            raise PreviewPackageTamperedError(package.preview_package_hash, expected)
        novel_id = uuid.UUID(package.novel_id)
        current = await compute_story_state_hash(self.session, novel_id)
        if current != package.story_state_hash:
            raise StaleStoryStateError(package.story_state_hash, current)
        scene_ids = {scene.scene_id for scene in package.draft.scenes}
        if not replacements or replacements.keys() - scene_ids:
            raise ValueError("Select existing scene IDs to revise")
        if any(not text.strip() for text in replacements.values()):
            raise ValueError("Replacement scene text must not be empty")

        draft = package.draft.model_copy(deep=True)
        for scene in draft.scenes:
            if scene.scene_id in replacements:
                scene.text = replacements[scene.scene_id]
                # This prose is author-edited, not the original model response.
                scene.generation_metadata = None
        draft.full_text = "\n\n".join(scene.text for scene in draft.scenes)
        draft.status = "preview"
        # The parent attempt retains original generation timing and provenance.
        draft.generation_metadata = [s.generation_metadata for s in draft.scenes
                                     if s.generation_metadata is not None]
        if checkpoints:
            async def revised_draft():
                return draft
            draft = await checkpoints.stage("draft", ChapterDraft, revised_draft)
        return await self._finish_preview(
            novel_id, package.user_direction, package.plan_result, draft,
            checkpoints=checkpoints, auto_repair=False,
        )

    async def _finish_preview(
        self, novel_id: uuid.UUID, user_direction: str | None,
        planning: PlanGenerationResult, draft: ChapterDraft, *,
        checkpoints=None, auto_repair: bool = True,
    ) -> ChapterPreviewPackage:
        from app.workflow.chapter import ChapterWorkflow

        return await ChapterWorkflow(
            self, checkpoints=checkpoints, auto_repair=auto_repair,
        ).run({
            "novel_id": novel_id, "user_direction": user_direction,
            "planning": planning, "draft": draft,
        }, from_draft=True)

    # --------------------------------------------------------------- finalize

    async def finalize(
        self,
        package: ChapterPreviewPackage,
        *,
        expected_story_state_hash: str | None = None,
        attempt_id: str | None = None,
        approval: ReviewApproval | None = None,
    ) -> ChapterCommitResult:
        """Deterministic commit of an approved preview. No LLM is invoked:
        the user approved this exact draft, and finalize revalidates
        everything against the CURRENT canonical state."""
        request = ChapterCommitRequest(
            novel_id=package.novel_id,
            chapter_plan=package.plan_result.plan,
            chapter_draft=package.draft,
            continuity_report=package.continuity_report,
            memory_result=package.memory_result,
            expected_story_state_hash=(
                expected_story_state_hash or package.story_state_hash
            ),
            preview_package_hash=package.preview_package_hash,
            attempt_id=attempt_id,
            approval=approval,
        )
        return await self.commit_service.commit_chapter(request)
