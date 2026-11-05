"""Phase 4 API: plan preview / validate endpoints with FakeLLM."""

from __future__ import annotations

import uuid

import pytest
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_chapter_planning_service
from app.db import get_session
from app.llm.client import AgentLLMClient
from app.llm.fake import FakeLLMProvider
from app.services.chapter_planning import ChapterPlanningService, build_director_agent
from tests.helpers import create_novel, create_rich_world
from tests.test_director_agent import _valid_plan_dict


@pytest.fixture
def planning_app(client):
    """App whose planning service is backed by a FakeLLMProvider."""
    provider = FakeLLMProvider()
    llm = AgentLLMClient(provider, agent_name="director", model="fake-model")

    def override_service(session: AsyncSession = Depends(get_session)):
        return ChapterPlanningService(session, director=build_director_agent(session, llm))

    app = client._transport.app
    app.dependency_overrides[get_chapter_planning_service] = override_service
    try:
        yield client, provider
    finally:
        app.dependency_overrides.pop(get_chapter_planning_service, None)


async def test_preview_endpoint_returns_plan_report_metadata(planning_app):
    client, provider = planning_app
    novel = (await client.post("/api/v1/novels", json={"title": "九霄问道"})).json()
    from tests.test_api import BOOTSTRAP_PAYLOAD

    await client.post(f"/api/v1/novels/{novel['id']}/bootstrap", json=BOOTSTRAP_PAYLOAD)

    # enqueue a valid plan (bootstrap world has 林凡 but no items/threads ids
    # known here; use a plan without entity ids to stay schema-valid and
    # domain-valid for warnings-only issues)
    plan = {
        "chapter_goal": "推进剧情",
        "scenes": [
            {"index": 1, "goal": "抵达", "participants": ["林凡"], "location": "天剑城", "beats": ["x"]}
        ],
        "ending_hook": "hook",
        "conflicts": [],
    }
    provider.enqueue_structured(plan)

    response = await client.post(
        f"/api/v1/novels/{novel['id']}/chapter-plan/preview",
        json={"user_direction": "下一章去天剑城"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["plan"]["chapter_goal"] == "推进剧情"
    assert body["validation_report"]["valid"] is True
    assert body["metadata"]["provider"] == "fake"
    assert len(body["context_hash"]) == 64
    assert "director_notes" not in body["writer_safe_plan"]


async def test_preview_endpoint_404_for_unknown_novel(planning_app):
    client, provider = planning_app
    response = await client.post(
        f"/api/v1/novels/{uuid.uuid4()}/chapter-plan/preview",
        json={"user_direction": None},
    )
    assert response.status_code == 404


async def test_validate_endpoint_revalidates_user_edited_plan(planning_app):
    client, provider = planning_app
    novel = (await client.post("/api/v1/novels", json={"title": "九霄问道"})).json()
    from tests.test_api import BOOTSTRAP_PAYLOAD

    await client.post(f"/api/v1/novels/{novel['id']}/bootstrap", json=BOOTSTRAP_PAYLOAD)
    # hard constraint exists in bootstrap payload ("苏青不能死亡")

    bad_plan = {
        "chapter_goal": "悲剧",
        "scenes": [
            {"index": 1, "goal": "结局", "participants": ["苏青"], "location": "天剑城", "beats": ["决斗"]}
        ],
        "required_events": ["苏青战死"],
        "ending_hook": "h",
        "conflicts": [],
    }
    response = await client.post(
        f"/api/v1/novels/{novel['id']}/chapter-plan/validate",
        json={"plan": bad_plan, "chapter_number": 5},
    )
    assert response.status_code == 200
    report = response.json()
    assert report["valid"] is False
    assert any(i["code"] == "hard_constraint_violation" for i in report["issues"])
