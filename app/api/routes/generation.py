"""Generate preview / finalize API (Phase 8). Finalize never calls an LLM."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.deps import get_chapter_generation_service
from app.commit.models import ChapterCommitResult, ChapterPreviewPackage, ReviewApproval
from app.services.chapter_generation import (
    ChapterGenerationService,
    ContinuityRepairExhaustedError,
)

router = APIRouter()


class GeneratePreviewRequest(BaseModel):
    user_direction: str | None = None
    chapter_number: int | None = None


class FinalizeRequest(BaseModel):
    package: ChapterPreviewPackage
    expected_story_state_hash: str | None = None
    attempt_id: str | None = None
    approval: ReviewApproval | None = None


@router.post("/novels/{novel_id}/chapters/generate-preview", response_model=ChapterPreviewPackage)
async def generate_preview(
    novel_id: uuid.UUID,
    payload: GeneratePreviewRequest,
    service: ChapterGenerationService = Depends(get_chapter_generation_service),
):
    try:
        return await service.generate_preview(
            novel_id, payload.user_direction, chapter_number=payload.chapter_number
        )
    except ContinuityRepairExhaustedError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/novels/{novel_id}/chapters/finalize", response_model=ChapterCommitResult)
async def finalize_chapter(
    novel_id: uuid.UUID,
    payload: FinalizeRequest,
    service: ChapterGenerationService = Depends(get_chapter_generation_service),
):
    if payload.package.novel_id != str(novel_id):
        raise HTTPException(status_code=409, detail="package novel_id mismatch")
    from app.commit.models import CommitError, CommitValidationCode

    status_by_code = {
        CommitValidationCode.stale_state: 409,
        CommitValidationCode.duplicate_commit: 409,
        CommitValidationCode.chapter_number_collision: 409,
        CommitValidationCode.draft_hash_mismatch: 422,
        CommitValidationCode.plan_hash_mismatch: 422,
        CommitValidationCode.continuity_invalid: 422,
        CommitValidationCode.memory_invalid: 422,
        CommitValidationCode.dry_run_stale: 422,
        CommitValidationCode.precondition_failed: 422,
    }
    try:
        return await service.finalize(
            payload.package, expected_story_state_hash=payload.expected_story_state_hash,
            attempt_id=payload.attempt_id, approval=payload.approval,
        )
    except CommitError as exc:
        # never leak internal DB details to the client (§74)
        raise HTTPException(
            status_code=status_by_code.get(exc.code, 422),
            detail={"code": exc.code.value, "message": exc.message},
        ) from exc
