"""Runtime recovery never silently reuses reports or regenerates saved prose."""
import asyncio
import uuid
import json
import httpx
import pytest

from tests.test_studio import studio, new_story, completed_task, counts


async def test_recheck_partial_draft_changes_runtime_without_replanning(studio):
    from app.services.studio_demo import build_demo_services
    manager, client = studio
    reached, hang = asyncio.Event(), asyncio.Event()

    async def factory(session, run, chapter):
        service = await build_demo_services(session, uuid.UUID(run.novel_id), chapter.number)
        if chapter.action == "recheck":
            async def forbidden(*args, **kwargs):
                raise AssertionError("Recheck must not call Director or Writer")
            service.planning_service.generate_preview = forbidden
            service.draft_service.generate_preview = forbidden
        else:
            async def check(*args, **kwargs):
                reached.set()
                await hang.wait()
            service.continuity_service.check_draft = check
        return service

    manager.service_factory = factory
    novel = await new_story(client)
    run = (await client.post("/api/v1/studio/runs", json={"novel_id": novel, "chapter_count": 1})).json()
    rid = run["id"]
    await asyncio.wait_for(reached.wait(), 10)
    await client.post(f"/api/v1/studio/runs/{rid}/cancel")
    old = manager.chapter_detail(rid, 1)
    export = await client.get(f"/api/v1/studio/runs/{rid}/export")
    assert "未完成草稿" in export.text
    assert old["partial"]["draft"]["full_text"] in export.text
    source = manager.root / rid / "chapter_01" / "attempts" / old["attempt_id"]
    original = {p.name: p.read_bytes() for p in source.glob("*.json")}
    # A paused record must be resumed or explicitly selected for rechecking.
    manager.get(rid).status = "blocked"
    result = await client.post(f"/api/v1/studio/runs/{rid}/chapters/1/recheck", json={
        "max_output_tokens": 8192,
        "runtime_capabilities": {"model": {"json_ok": True, "enable_thinking_false_accepted": True}},
        "role_models": {"continuity_checker": "GLM-5.2"},
    })
    assert result.status_code == 200, result.text
    await completed_task(manager, rid)
    current = manager.chapter_detail(rid, 1)
    assert current["attempt_id"] != old["attempt_id"]
    assert manager.get(rid).chapters[0].source_attempt == old["attempt_id"]
    assert current["package"]["draft"] == old["partial"]["draft"]
    assert manager.get(rid).config.max_output_tokens == 8192
    effective = manager._effective_settings(manager.get(rid))
    assert effective.llm.agents["continuity_checker"].model == "GLM-5.2"
    assert effective.llm.agents["director"].model == manager.get(rid).config.model
    assert current["package"]["memory_result"]["validation_report"]["valid"]
    assert {p.name: p.read_bytes() for p in source.glob("*.json")} == original
    assert await counts(manager) == [0] * 4


async def test_recheck_complete_package_is_new_attempt_and_old_approval_invalid(studio):
    manager, client = studio
    novel = await new_story(client)
    run = (await client.post("/api/v1/studio/runs", json={"novel_id": novel, "chapter_count": 1})).json()
    rid = run["id"]
    await completed_task(manager, rid)
    old = manager.chapter_detail(rid, 1)
    assert (await client.post(f"/api/v1/studio/runs/{rid}/chapters/1/recheck", json={"max_output_tokens": 8192})).status_code == 200
    await completed_task(manager, rid)
    current = manager.chapter_detail(rid, 1)
    assert current["attempt_id"] != old["attempt_id"]
    assert current["package"]["draft"] == old["package"]["draft"]
    assert not current["approval"]
    assert await counts(manager) == [0] * 4


@pytest.mark.parametrize("capabilities,expected", [({}, False), ({"thinking_disabled_accepted": True}, True)])
async def test_native_thinking_switch_is_capability_gated(monkeypatch, capabilities, expected):
    from app.llm.openai_compatible import OpenAICompatibleProvider
    from app.llm.client import AgentLLMClient
    monkeypatch.setenv("TEST_SCNET_KEY", "offline-only")
    bodies = []
    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        provider = OpenAICompatibleProvider(base_url="https://offline.test", api_key_env="TEST_SCNET_KEY", http_client=http)
        provider.model_capabilities = {"test": capabilities}
        await AgentLLMClient(provider, agent_name="test", model="test", max_tokens=256).generate_text(system="", user="check")
    assert (bodies[0].get("thinking") == {"type": "disabled"}) == expected
    assert "enable_thinking" not in bodies[0]
