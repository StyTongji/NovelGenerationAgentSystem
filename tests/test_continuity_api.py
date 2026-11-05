"""Phase 6 API: chapter-draft check + repair-preview endpoints."""

from __future__ import annotations

import uuid

import pytest
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_continuity_service
from app.db import get_session
from app.services.continuity import ContinuityService
from tests.helpers import create_novel, create_rich_world


@pytest.fixture
def continuity_app(client):
    def override(session: AsyncSession = Depends(get_session)):
        # deterministic_only: no LLM needed for the check path
        return ContinuityService(session, llm_checker=None, repair_agent=None, mode="deterministic_only")

    app = client._transport.app
    app.dependency_overrides[get_continuity_service] = override
    try:
        yield client
    finally:
        app.dependency_overrides.pop(get_continuity_service, None)


def _build_draft_and_plan(world):
    from datetime import UTC, datetime

    from app.draft.models import ChapterDraft, SceneDraft
    from app.schemas.agent_outputs import ChapterPlan
    from tests.draft_helpers import build_valid_plan_dict

    plan = ChapterPlan.model_validate(build_valid_plan_dict(world))
    draft = ChapterDraft(
        novel_id=str(world["result"].novel_id),
        chapter_number=5,
        scenes=[SceneDraft(scene_id="s1", scene_index=1, text="正常正文。")],
        full_text="正常正文。",
        chapter_plan_hash="0" * 64,
        story_context_hash="0" * 64,
        created_at=datetime.now(UTC),
    )
    return plan, draft


async def test_check_endpoint_returns_report(continuity_app, session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    plan, draft = _build_draft_and_plan(world)

    response = await continuity_app.post(
        f"/api/v1/novels/{novel.id}/chapter-draft/check",
        json={"draft": draft.model_dump(mode="json"), "chapter_plan": plan.model_dump(mode="json")},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is True
    assert body["draft_hash"]
    assert body["checked_scene_ids"] == ["s1"]


async def test_check_endpoint_detects_issue(continuity_app, session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    plan, draft = _build_draft_and_plan(world)
    draft.scenes[0].text = "苏青魂飞魄散。"
    draft.full_text = draft.scenes[0].text

    response = await continuity_app.post(
        f"/api/v1/novels/{novel.id}/chapter-draft/check",
        json={"draft": draft.model_dump(mode="json"), "chapter_plan": plan.model_dump(mode="json")},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is False
    assert any(i["category"] == "hard_constraint" for i in body["issues"])


async def test_check_endpoint_rejects_foreign_novel(continuity_app, session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    plan, draft = _build_draft_and_plan(world)

    response = await continuity_app.post(
        f"/api/v1/novels/{uuid.uuid4()}/chapter-draft/check",
        json={"draft": draft.model_dump(mode="json"), "chapter_plan": plan.model_dump(mode="json")},
    )
    assert response.status_code == 409


async def test_repair_preview_endpoint_requires_repair_agent(continuity_app, session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    plan, draft = _build_draft_and_plan(world)

    response = await continuity_app.post(
        f"/api/v1/novels/{novel.id}/chapter-draft/repair-preview",
        json={"draft": draft.model_dump(mode="json"), "chapter_plan": plan.model_dump(mode="json")},
    )
    # deterministic_only override has no repair agent -> 503, not a crash
    assert response.status_code == 503
