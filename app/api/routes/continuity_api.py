"""Continuity check / targeted repair API (Phase 6, no commit)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.deps import get_continuity_service
from app.continuity.models import ContinuityReport
from app.draft.models import ChapterDraft
from app.schemas.agent_outputs import ChapterPlan
from app.services.continuity import ContinuityService

router = APIRouter()


class ContinuityCheckRequest(BaseModel):
    draft: ChapterDraft
    chapter_plan: ChapterPlan


class ContinuityRepairRequest(BaseModel):
    draft: ChapterDraft
    chapter_plan: ChapterPlan


@router.post("/novels/{novel_id}/chapter-draft/check", response_model=ContinuityReport)
async def check_chapter_draft(
    novel_id: uuid.UUID,
    payload: ContinuityCheckRequest,
    service: ContinuityService = Depends(get_continuity_service),
):
    _verify_novel(payload, novel_id)
    try:
        return await service.check_draft(novel_id, payload.draft, payload.chapter_plan)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/novels/{novel_id}/chapter-draft/repair-preview")
async def repair_chapter_draft(
    novel_id: uuid.UUID,
    payload: ContinuityRepairRequest,
    service: ContinuityService = Depends(get_continuity_service),
):
    _verify_novel(payload, novel_id)
    try:
        result = await service.repair_preview(novel_id, payload.draft, payload.chapter_plan)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "draft": result.draft,
        "report": result.report,
        "repair_history": result.repair_history,
        "exhausted": result.exhausted,
        "generation_metadata": result.generation_metadata,
    }


def _verify_novel(payload: ContinuityCheckRequest, novel_id: uuid.UUID) -> None:
    # provenance: the draft must belong to this novel
    if str(payload.draft.novel_id) != str(novel_id):
        raise HTTPException(
            status_code=409,
            detail=f"Draft novel_id {payload.draft.novel_id} does not match path {novel_id}",
        )
