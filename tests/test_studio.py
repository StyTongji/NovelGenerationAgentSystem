"""Exercise the same HTTP/control plane used by the author, with no paid API."""
import asyncio
import json

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Settings
from app.main import create_app
from app.models import Chapter, ChapterCommit, Event, Novel, StorySnapshot
from app.services.studio import StudioManager


@pytest.fixture
async def studio(engine, tmp_path):
    manager = StudioManager(tmp_path / "studio", async_sessionmaker(engine, expire_on_commit=False), Settings())
    app = create_app()
    app.state.studio = manager
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield manager, client
    await manager.close()


async def new_story(client):
    response = await client.post("/api/v1/studio/novels", json={
        "title": "雨城来信", "genre": "悬疑", "synopsis": "旧档案中的缺页指向一段无人承认的往事。",
        "protagonist": "许知遥", "companions": ["程越"], "world_rules": ["未经核实的猜测不能作为已知事实。"],
    })
    assert response.status_code == 201, response.text
    return response.json()["id"]


def approval(detail):
    package = detail["package"]
    return {"attempt_id": detail["attempt_id"], "reviewed_by": "offline-test-reviewer",
            "approved": True, "semantic_review_complete": True,
            "preview_package_hash": package["preview_package_hash"],
            "operation_reviews": {op["operation_id"]: "correct" for op in package["memory_result"]["batch"]["operations"]},
            "quality_scores": {key: 4 for key in ("readability", "character", "dialogue", "continuity", "progression")},
            "missing": []}


async def completed_task(manager, run_id):
    await asyncio.wait_for(asyncio.shield(manager.tasks[run_id]), timeout=20)
    run = manager.get(run_id)
    assert run.status == "awaiting_review", run.error
    return run


async def counts(manager):
    async with manager.session_factory() as session:
        return [await session.scalar(select(func.count()).select_from(table))
                for table in (Chapter, ChapterCommit, StorySnapshot, Event)]


async def test_five_committed_chapters_then_continue_in_new_record(studio):
    manager, client = studio
    novel_id = await new_story(client)
    first = (await client.post("/api/v1/studio/runs", json={
        "novel_id": novel_id, "mode": "demo", "chapter_count": 5})).json()
    for number in range(1, 6):
        await completed_task(manager, first["id"])
        detail = manager.chapter_detail(first["id"], number)
        response = await client.post(f"/api/v1/studio/runs/{first['id']}/chapters/{number}/approve", json=approval(detail))
        assert response.status_code == 200, response.text
    assert manager.get(first["id"]).status == "completed"
    assert (await counts(manager))[:3] == [5, 5, 5]
    second_response = await client.post("/api/v1/studio/runs", json={
        "novel_id": novel_id, "mode": "demo", "chapter_count": 2})
    assert second_response.status_code == 201, second_response.text
    second = second_response.json()
    assert second["start_chapter"] == 6
    for number in (6, 7):
        await completed_task(manager, second["id"])
        detail = manager.chapter_detail(second["id"], number)
        assert detail["package"]["draft"]["chapter_number"] == number
        assert "上一次" in detail["package"]["draft"]["full_text"]
        response = await client.post(f"/api/v1/studio/runs/{second['id']}/chapters/{number}/approve", json=approval(detail))
        assert response.status_code == 200, response.text
    assert manager.get(second["id"]).status == "completed"
    assert (await counts(manager))[:3] == [7, 7, 7]
    assert manager.chapter_detail(first["id"], 5)["commit"]


async def test_http_three_chapters_review_gates_state_inheritance_and_duplicate(studio):
    manager, client = studio
    novel_id = await new_story(client)
    response = await client.post("/api/v1/studio/runs", json={"novel_id": novel_id, "mode": "demo", "chapter_count": 3})
    assert response.status_code == 201, response.text
    run_id = response.json()["id"]
    first_approval = None
    for number in range(1, 4):
        await completed_task(manager, run_id)
        assert await counts(manager) == [number - 1] * 4
        detail = (await client.get(f"/api/v1/studio/runs/{run_id}/chapters/{number}")).json()
        assert detail["package"]["memory_result"]["validation_report"]["valid"]
        assert detail["package"]["memory_result"]["dry_run"]["applicable"]
        if number > 1:
            assert "上一次" in detail["package"]["draft"]["full_text"]
        payload = approval(detail)
        if number == 1:
            first_approval = payload
            bad = {**payload, "semantic_review_complete": False}
            rejected = await client.post(f"/api/v1/studio/runs/{run_id}/chapters/1/approve", json=bad)
            assert rejected.status_code == 422
            assert await counts(manager) == [0] * 4
            duplicate_run = await client.post("/api/v1/studio/runs", json={"novel_id": novel_id})
            assert duplicate_run.status_code == 409
        approved = await client.post(f"/api/v1/studio/runs/{run_id}/chapters/{number}/approve", json=payload)
        assert approved.status_code == 200, approved.text
    assert manager.get(run_id).status == "completed"
    assert await counts(manager) == [3] * 4
    again = await client.post(f"/api/v1/studio/runs/{run_id}/chapters/1/approve", json=first_approval)
    assert again.status_code == 200
    assert await counts(manager) == [3] * 4
    state = (await client.get(f"/api/v1/studio/novels/{novel_id}/state")).json()
    assert state["current_chapter_number"] == 3
    assert len(state["knowledge"]) >= 3
    export = await client.get(f"/api/v1/studio/runs/{run_id}/export")
    assert "离线脚本演示" in export.text and "第 3 章" in export.text
    events = manager.events(run_id)
    assert any(e.get("stage") == "director" for e in events)
    assert any(e.get("stage") == "memory" and "output" in e for e in events)
    assert len({e["sequence"] for e in events}) == len(events)


async def test_candidate_memory_edit_invalidates_old_approval_and_is_offline(studio):
    manager, client = studio
    novel = await new_story(client)
    run = (await client.post("/api/v1/studio/runs", json={"novel_id": novel, "chapter_count": 1})).json()
    rid = run["id"]
    await completed_task(manager, rid)
    detail = manager.chapter_detail(rid, 1)
    old = approval(detail)
    batch = detail["package"]["memory_result"]["batch"]
    # Even an identical edit is a new reviewable attempt, never old approval reuse.
    response = await client.post(f"/api/v1/studio/runs/{rid}/chapters/1/memory", json={"batch": batch})
    assert response.status_code == 200
    await completed_task(manager, rid)
    assert manager.chapter_detail(rid, 1)["attempt_id"] != old["attempt_id"]
    assert (await client.post(f"/api/v1/studio/runs/{rid}/chapters/1/approve", json=old)).status_code == 409
    assert await counts(manager) == [0] * 4


async def test_cancel_awaits_and_resume_reuses_completed_stage(studio):
    manager, client = studio
    from app.services.studio_demo import build_demo_services
    reached, hang = asyncio.Event(), asyncio.Event()
    calls = []

    async def factory(session, run, chapter):
        service = await build_demo_services(session, __import__('uuid').UUID(run.novel_id), chapter.number)
        original = service.draft_service.generate_preview
        async def draft(*args, **kwargs):
            calls.append("draft")
            if len(calls) == 1:
                reached.set()
                await hang.wait()
            return await original(*args, **kwargs)
        service.draft_service.generate_preview = draft
        return service

    manager.service_factory = factory
    novel = await new_story(client)
    run = (await client.post("/api/v1/studio/runs", json={"novel_id": novel, "chapter_count": 1})).json()
    rid = run["id"]
    await asyncio.wait_for(reached.wait(), timeout=10)
    cancelled = await client.post(f"/api/v1/studio/runs/{rid}/cancel")
    assert cancelled.json()["status"] == "cancelled"
    assert manager.tasks[rid].done()
    assert await counts(manager) == [0] * 4
    assert (await client.post(f"/api/v1/studio/runs/{rid}/resume")).status_code == 200
    await completed_task(manager, rid)
    assert any(e.get("stage") == "director" and e.get("status") == "cached" for e in manager.events(rid))


async def test_studio_bootstrap_assets_and_unconfigured_live(studio):
    manager, client = studio
    novel = await new_story(client)
    info = (await client.get("/api/v1/studio/bootstrap")).json()
    assert info["framework"] == "LangGraph" and len(info["roles"]) == 7
    response = await client.post("/api/v1/studio/runs", json={"novel_id": novel, "mode": "live", "model": "Auto"})
    assert response.status_code == 409
    assert not manager.tasks
    assert (await client.get("/")).status_code == 200
    assert (await client.get("/static/studio.js")).status_code == 200
    assert (await client.get("/static/../config.yaml")).status_code == 404


async def test_failed_continuity_partial_can_be_edited_without_replanning(studio):
    from app.continuity.models import ContinuityRepairResult, ContinuityReport
    from app.services.studio_demo import build_demo_services
    from app.services.canonical_commit import draft_hash_of
    from unittest.mock import AsyncMock
    manager, client = studio
    first = True
    async def factory(session, run, chapter):
        nonlocal first
        service = await build_demo_services(session, __import__('uuid').UUID(run.novel_id), chapter.number)
        if first:
            first = False
            async def fail_check(novel_id, draft, *args, **kwargs):
                return ContinuityReport(valid=False, draft_hash=draft_hash_of(draft))
            async def fail_repair(novel_id, draft, *args, **kwargs):
                return ContinuityRepairResult(draft=draft, report=await fail_check(novel_id, draft), exhausted=True)
            service.continuity_service.check_draft = fail_check
            service.continuity_service.repair_preview = fail_repair
        else:
            service.planning_service.generate_preview = AsyncMock(side_effect=AssertionError("No replanning"))
            service.draft_service.generate_preview = AsyncMock(side_effect=AssertionError("No rewriting"))
        return service
    manager.service_factory = factory
    novel = await new_story(client)
    rid = (await client.post("/api/v1/studio/runs", json={"novel_id": novel, "chapter_count": 1})).json()["id"]
    await manager.tasks[rid]
    assert manager.get(rid).status == "blocked"
    detail = manager.chapter_detail(rid, 1)
    assert detail["package"] is None
    scene = detail["partial"]["draft"]["scenes"][0]
    response = await client.post(f"/api/v1/studio/runs/{rid}/chapters/1/revise", json={
        "replacements": {scene["scene_id"]: scene["text"] + "\n窗外的雨渐渐停了。"}})
    assert response.status_code == 200, response.text
    await completed_task(manager, rid)
    assert "窗外的雨渐渐停了" in manager.chapter_detail(rid, 1)["package"]["draft"]["full_text"]
    assert await counts(manager) == [0] * 4


async def test_resume_refuses_changed_state_or_tampered_source(studio):
    manager, client = studio
    from app.services.studio_seed import StudioNovelInput
    novel = await new_story(client)
    rid = (await client.post("/api/v1/studio/runs", json={"novel_id": novel, "chapter_count": 1})).json()["id"]
    await completed_task(manager, rid)
    run = manager.get(rid)
    package = manager._package(run, run.chapters[0])
    package.draft.full_text += "非法篡改"
    with pytest.raises(ValueError, match="hash"):
        manager._check_source(package, package.story_state_hash)
    await manager.cancel(rid)
    async with manager.session_factory() as session:
        story = await session.get(Novel, __import__('uuid').UUID(novel))
        story.premise = "不同的故事"
        await session.commit()
    await manager.resume(rid)
    await manager.tasks[rid]
    assert run.status == "blocked" and "Resume refused" in run.error


async def test_recover_commit_after_artifact_write_failure(studio, monkeypatch):
    import app.services.studio as module
    manager, client = studio
    novel = await new_story(client)
    rid = (await client.post("/api/v1/studio/runs", json={"novel_id": novel, "chapter_count": 1})).json()["id"]
    await completed_task(manager, rid)
    detail = manager.chapter_detail(rid, 1)
    original = module.write_new
    def interrupted(path, value, **kwargs):
        if path.name == "commit.json":
            raise OSError("simulated disk interruption after canonical commit")
        original(path, value, **kwargs)
    monkeypatch.setattr(module, "write_new", interrupted)
    with pytest.raises(OSError):
        await manager.approve(rid, 1, __import__('app.schemas.studio', fromlist=['ApprovalInput']).ApprovalInput(**approval(detail)))
    assert await counts(manager) == [1] * 4
    monkeypatch.setattr(module, "write_new", original)
    await manager.resume(rid)
    assert manager.get(rid).status == "completed"
    assert manager.chapter_detail(rid, 1)["commit"]["duplicate"]
    assert await counts(manager) == [1] * 4


async def test_sse_disconnect_cancels_and_awaits_observed_task(studio):
    from app.api.routes.studio import events
    from app.services.studio_demo import build_demo_services
    manager, client = studio
    started = asyncio.Event()
    async def factory(session, run, chapter):
        service = await build_demo_services(session, __import__('uuid').UUID(run.novel_id), chapter.number)
        async def hang(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()
        service.planning_service.generate_preview = hang
        return service
    manager.service_factory = factory
    novel = await new_story(client)
    rid = (await client.post("/api/v1/studio/runs", json={"novel_id": novel, "chapter_count": 1})).json()["id"]
    await started.wait()
    class Disconnected:
        headers = {}
        async def is_disconnected(self):
            return True
    response = await events(rid, Disconnected(), 0, manager)
    async for _ in response.body_iterator:
        pass
    assert manager.tasks[rid].done()
    assert manager.get(rid).status == "cancelled"
    assert await counts(manager) == [0] * 4


async def test_old_stream_cannot_cancel_replacement_task(studio):
    manager, client = studio
    novel = await new_story(client)
    rid = (await client.post("/api/v1/studio/runs", json={"novel_id": novel, "chapter_count": 1})).json()["id"]
    await completed_task(manager, rid)
    old = manager.tasks[rid]
    new = asyncio.create_task(asyncio.Event().wait())
    manager.tasks[rid] = new
    manager.get(rid).status = "running"
    await manager.cancel(rid, expected_task=old)
    assert not new.done()
    assert manager.get(rid).status == "running"
    await manager.cancel(rid)
    assert new.done()
