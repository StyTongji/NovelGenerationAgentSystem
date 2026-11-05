"""Memory extraction preview / validate / dry-run API (Phase 7, no apply)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.deps import get_memory_extraction_service
from app.continuity.models import ContinuityReport
from app.draft.models import ChapterDraft
from app.memory.models import MemoryUpdateBatch
from app.schemas.agent_outputs import ChapterPlan
from app.services.memory_extraction import (
    BlockingContinuityError,
    MemoryExtractionService,
    StaleContinuityReportError,
)

router = APIRouter()


class MemoryPreviewRequest(BaseModel):
    draft: ChapterDraft
    chapter_plan: ChapterPlan
    continuity_report: ContinuityReport


class MemoryValidateRequest(BaseModel):
    draft: ChapterDraft
    chapter_plan: ChapterPlan
    batch: MemoryUpdateBatch
    dry_run: bool = False


@router.post("/novels/{novel_id}/chapter-draft/memory-preview")
async def memory_preview(
    novel_id: uuid.UUID,
    payload: MemoryPreviewRequest,
    service: MemoryExtractionService = Depends(get_memory_extraction_service),
):
    _verify(payload.draft, novel_id)
    try:
        return await service.extract_preview(
            novel_id, payload.draft, payload.chapter_plan, payload.continuity_report
        )
    except StaleContinuityReportError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except BlockingContinuityError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/novels/{novel_id}/memory-update/validate")
async def validate_memory_update(
    novel_id: uuid.UUID,
    payload: MemoryValidateRequest,
    service: MemoryExtractionService = Depends(get_memory_extraction_service),
):
    _verify(payload.draft, novel_id)
    return await service.validate_batch(
        novel_id, payload.draft, payload.chapter_plan, payload.batch,
        dry_run=payload.dry_run,
    )


def _verify(draft: ChapterDraft, novel_id: uuid.UUID) -> None:
    if str(draft.novel_id) != str(novel_id):
        raise HTTPException(status_code=409, detail="Draft novel_id mismatch")
