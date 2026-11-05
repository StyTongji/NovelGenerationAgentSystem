"""Phase 5 API: scene-plan preview and chapter-draft preview (FakeLLM)."""

from __future__ import annotations

import uuid

import pytest
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_chapter_draft_service, get_scene_planner
from app.db import get_session
from tests.draft_helpers import build_scene_batch_dict, build_valid_plan_dict
from tests.helpers import create_novel, create_rich_world


@pytest.fixture
def draft_app(client):
    provider = FakeLLMProviderHolder()
    from app.agents.scene_planner import ScenePlannerAgent
    from app.agents.writer import WriterAgent
    from app.llm.client import AgentLLMClient
    from app.llm.prompts import get_default_registry
    from app.planning.scene_validator import ScenePlanValidator
    from app.services.chapter_draft import ChapterDraftService

    llm_planner = AgentLLMClient(provider.provider, agent_name="scene_planner", model="fake-model")
    llm_writer = AgentLLMClient(provider.provider, agent_name="writer", model="fake-model")

    def override_planner(session: AsyncSession = Depends(get_session)):
        return ScenePlannerAgent(
            llm=llm_planner,
            prompt_registry=get_default_registry(),
            validator=ScenePlanValidator(session),
        )

    def override_draft(session: AsyncSession = Depends(get_session)):
        return ChapterDraftService(
            session,
            override_planner.__wrapped__ if hasattr(override_planner, "__wrapped__") else None,
            None,
        ) if False else ChapterDraftService(
            session,
            ScenePlannerAgent(
                llm=llm_planner,
                prompt_registry=get_default_registry(),
                validator=ScenePlanValidator(session),
            ),
            WriterAgent(llm=llm_writer, prompt_registry=get_default_registry()),
        )

    app = client._transport.app
    app.dependency_overrides[get_scene_planner] = override_planner
    app.dependency_overrides[get_chapter_draft_service] = override_draft
    try:
        yield client, provider
    finally:
        app.dependency_overrides.pop(get_scene_planner, None)
        app.dependency_overrides.pop(get_chapter_draft_service, None)


class FakeLLMProviderHolder:
    def __init__(self) -> None:
        from app.llm.fake import FakeLLMProvider

        self.provider = FakeLLMProvider()


async def _bootstrap_world(client):
    novel = (await client.post("/api/v1/novels", json={"title": "九霄问道"})).json()
    from tests.test_api import BOOTSTRAP_PAYLOAD

    await client.post(f"/api/v1/novels/{novel['id']}/bootstrap", json=BOOTSTRAP_PAYLOAD)
    characters = (await client.get(f"/api/v1/novels/{novel['id']}/characters")).json()
    lin_fan_id = next(c["id"] for c in characters if c["name"] == "林凡")
    return novel, lin_fan_id


async def test_scene_plan_preview_endpoint(draft_app):
    client, holder = draft_app
    novel, _ = await _bootstrap_world(client)
    # NOTE: bootstrap world lacks rich-world entities; use a minimal plan with
    # no canonical id references so validation passes.
    plan = {
        "chapter_number": 5,
        "chapter_goal": "过渡章",
        "participating_characters": [{"temporary_ref": "new_character_1", "name": "神秘女子"}],
        "proposed_entities": [
            {"entity_type": "character", "temporary_ref": "new_character_1", "name_hint": "神秘女子"}
        ],
        "scenes": [
            {"index": 1, "goal": "beat1", "participants": [], "location": "青玄大陆", "beats": []},
            {"index": 2, "goal": "beat2", "participants": [], "location": "青玄大陆", "beats": []},
        ],
        "ending_hook": "h",
        "conflicts": [],
    }
    scene_batch = {
        "chapter_number": 5,
        "scenes": [
            {
                "scene_id": f"s{i}", "scene_index": i, "source_beat_ids": [str(i)],
                "pov": {"temporary_ref": "new_character_1"},
                "location": {"name": "青玄大陆"},
                "scene_goal": f"g{i}", "target_chars": 800,
                "temporary_entities": ["new_character_1"],
            }
            for i in (1, 2)
        ],
    }
    holder.provider.enqueue_structured(scene_batch)

    response = await client.post(
        f"/api/v1/novels/{novel['id']}/scene-plan/preview",
        json={"plan": plan},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["plan"]["scenes"][0]["scene_id"] == "s1"
    assert body["validation_report"]["valid"] is True
    assert body["metadata"]["agent"] == "scene_planner"


async def test_chapter_draft_preview_endpoint(draft_app):
    client, holder = draft_app
    novel, lin_fan_id = await _bootstrap_world(client)
    plan = {
        "chapter_number": 5,
        "chapter_goal": "过渡章",
        "participating_characters": [
            {"character_id": lin_fan_id, "name": "林凡"},
            {"temporary_ref": "new_character_1", "name": "神秘女子"},
        ],
        "proposed_entities": [
            {"entity_type": "character", "temporary_ref": "new_character_1", "name_hint": "神秘女子"}
        ],
        "scenes": [
            {"index": 1, "goal": "beat1", "participants": [], "location": "青玄大陆", "beats": []},
            {"index": 2, "goal": "beat2", "participants": [], "location": "青玄大陆", "beats": []},
        ],
        "ending_hook": "h",
        "conflicts": [],
    }
    scene_batch = {
        "chapter_number": 5,
        "scenes": [
            {
                "scene_id": f"s{i}", "scene_index": i, "source_beat_ids": [str(i)],
                "pov": {"character_id": lin_fan_id, "name": "林凡"},
                "location": {"name": "青玄大陆"},
                "participants": [{"character_id": lin_fan_id, "name": "林凡"}],
                "scene_goal": f"g{i}", "target_chars": 800,
                "temporary_entities": ["new_character_1"],
            }
            for i in (1, 2)
        ],
    }
    holder.provider.enqueue_structured(scene_batch)
    holder.provider.enqueue_text("场景一正文。")
    holder.provider.enqueue_text("场景二正文。")

    response = await client.post(
        f"/api/v1/novels/{novel['id']}/chapter-draft/preview",
        json={"plan": plan},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "preview"
    assert len(body["scenes"]) == 2
    assert "场景一正文" in body["full_text"]
    assert body["temporary_entities"][0]["temporary_ref"] == "new_character_1"


async def test_chapter_draft_preview_rejects_invalid_plan(draft_app):
    client, holder = draft_app
    novel, _ = await _bootstrap_world(client)
    plan = {
        "chapter_number": 5,
        "chapter_goal": "违规",
        "scenes": [{"index": 1, "goal": "g", "participants": [], "location": "青玄大陆", "beats": []}],
        "required_events": ["苏青战死"],  # hard constraint from bootstrap
        "ending_hook": "h",
        "conflicts": [],
    }
    response = await client.post(
        f"/api/v1/novels/{novel['id']}/chapter-draft/preview",
        json={"plan": plan},
    )
    assert response.status_code == 422
    assert "hard_constraint_violation" in response.text
