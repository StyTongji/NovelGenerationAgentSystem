"""Plan preview / validate API (Phase 4). No commit endpoints here."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_chapter_planning_service
from app.db import get_session
from app.planning.models import PlanGenerationResult, PlanValidationReport
from app.schemas.agent_outputs import ChapterPlan
from app.services.chapter_planning import ChapterPlanningService

router = APIRouter()


class PlanPreviewRequest(BaseModel):
    user_direction: str | None = Field(
        default=None, description="自然语言剧情指导（不覆盖 hard constraint / canonical truth）"
    )
    chapter_number: int | None = None


class PlanValidateRequest(BaseModel):
    plan: ChapterPlan
    chapter_number: int | None = None


@router.post("/novels/{novel_id}/chapter-plan/preview", response_model=PlanGenerationResult)
async def preview_chapter_plan(
    novel_id: uuid.UUID,
    payload: PlanPreviewRequest,
    service: ChapterPlanningService = Depends(get_chapter_planning_service),
):
    try:
        return await service.generate_preview(
            novel_id, payload.user_direction, chapter_number=payload.chapter_number
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/novels/{novel_id}/chapter-plan/validate", response_model=PlanValidationReport)
async def validate_chapter_plan(
    novel_id: uuid.UUID,
    payload: PlanValidateRequest,
    session: AsyncSession = Depends(get_session),
):
    service = ChapterPlanningService(session)
    try:
        return await service.validate_plan(
            novel_id, payload.plan, chapter_number=payload.chapter_number
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
