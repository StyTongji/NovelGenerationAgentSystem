"""Scene plan preview / chapter draft preview API (Phase 5, no commit)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.deps import get_chapter_draft_service, get_scene_planner
from app.db import get_session
from app.agents.scene_planner import ScenePlannerAgent
from app.draft.models import ChapterDraft
from app.llm.models import GenerationMetadata
from app.planning.models import PlanGenerationResult
from app.schemas.agent_outputs import ChapterPlan
from app.services.chapter_draft import ChapterDraftService

router = APIRouter()


class ScenePlanPreviewRequest(BaseModel):
    plan: ChapterPlan
    chapter_number: int | None = None


class ChapterDraftPreviewRequest(BaseModel):
    plan: ChapterPlan
    expected_context_hash: str | None = None


@router.post("/novels/{novel_id}/scene-plan/preview", response_model=PlanGenerationResult)
async def preview_scene_plan(
    novel_id: uuid.UUID,
    payload: ScenePlanPreviewRequest,
    session=Depends(get_session),
    scene_planner: ScenePlannerAgent = Depends(get_scene_planner),
):
    from app.context.builder import ContextBuilder
    from app.planning.validator import ChapterPlanValidator

    builder = ContextBuilder(session)
    number = payload.plan.chapter_number or payload.chapter_number or 1
    context = await builder.build_director_context(novel_id, chapter_number=number)
    report = await ChapterPlanValidator(session).validate(payload.plan, context)
    if not report.valid:
        raise HTTPException(status_code=422, detail="Plan invalid against canonical state")
    result = await scene_planner.plan_scenes(
        payload.plan.to_writer_safe(), novel_id, number
    )
    return result


@router.post("/novels/{novel_id}/chapter-draft/preview", response_model=ChapterDraft)
async def preview_chapter_draft(
    novel_id: uuid.UUID,
    payload: ChapterDraftPreviewRequest,
    service: ChapterDraftService = Depends(get_chapter_draft_service),
):
    from app.draft.models import ChapterDraftGenerationError, StalePlanError

    try:
        return await service.generate_preview(
            novel_id, payload.plan, expected_context_hash=payload.expected_context_hash
        )
    except StalePlanError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ChapterDraftGenerationError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "message": str(exc),
                "completed_scenes": [d.scene_id for d in exc.completed_scene_drafts],
                "failed_scene_id": exc.failed_scene_id,
            },
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
