"""Record lifecycle and model-suggested review remain separate from approval."""
import asyncio
import json
import uuid

import pytest
from sqlalchemy import select

from app.agents.literary_reviewer import LiteraryReviewerAgent
from app.llm.client import AgentLLMClient
from app.llm.fake import FakeLLMProvider
from app.llm.prompts import get_default_registry
from app.models import ChapterCommit
from app.services.studio_demo import build_demo_services
from tests.test_studio import studio, new_story, completed_task, counts, approval


async def start(client, novel, **kwargs):
    response = await client.post("/api/v1/studio/runs", json={"novel_id": novel, "chapter_count": 1, **kwargs})
    assert response.status_code == 201, response.text
    return response.json()["id"]


@pytest.mark.parametrize("action,status", [("cancel", "cancelled"), ("finish", "archived")])
async def test_ending_waiting_record_allows_new_and_preserves_drafts(studio, action, status):
    manager, client = studio
    novel = await new_story(client)
    first = await start(client, novel)
    await completed_task(manager, first)
    original = manager.chapter_detail(first, 1)["package"]
    result = await client.post(f"/api/v1/studio/runs/{first}/{action}", json={})
    assert result.status_code == 200 and result.json()["status"] == status
    assert manager.chapter_detail(first, 1)["package"] == original
    second = await start(client, novel)
    await completed_task(manager, second)
    assert manager.get(second).start_chapter == 1
    assert await counts(manager) == [0] * 4
    if action == "finish":
        assert (await client.post(f"/api/v1/studio/runs/{first}/resume", json={})).status_code == 409
        assert (await client.get(f"/api/v1/studio/runs/{first}/export")).status_code == 200


async def test_replace_running_record_cancels_and_awaits_before_new_generation(studio):
    manager, client = studio
    started, stopped = asyncio.Event(), asyncio.Event()
    async def factory(session, run, chapter):
        service = await build_demo_services(session, uuid.UUID(run.novel_id), chapter.number)
        if len(manager.runs) == 1:
            async def hang(*args, **kwargs):
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    stopped.set()
            service.planning_service.generate_preview = hang
        else:
            assert stopped.is_set()
        return service
    manager.service_factory = factory
    novel = await new_story(client)
    first = await start(client, novel)
    await asyncio.wait_for(started.wait(), 5)
    second = await start(client, novel, replace_existing=True)
    await completed_task(manager, second)
    assert stopped.is_set() and manager.tasks[first].done()
    assert manager.get(first).status == "archived"
    assert await counts(manager) == [0] * 4


async def test_assistance_is_advisory_persisted_and_requires_explicit_approval(studio):
    manager, client = studio
    novel = await new_story(client)
    rid = await start(client, novel)
    await completed_task(manager, rid)
    response = await client.post(f"/api/v1/studio/runs/{rid}/chapters/1/assist-review", json={})
    assert response.status_code == 200
    await completed_task(manager, rid)
    detail = manager.chapter_detail(rid, 1)
    report = detail["assistance"]
    assert report["source"] == "offline_demo"
    assert report["preview_package_hash"] == detail["package"]["preview_package_hash"]
    assert len(report["operation_reviews"]) == len(detail["package"]["memory_result"]["batch"]["operations"])
    assert all(score == 3 for score in report["quality_scores"].values())
    assert "approved" not in report and "semantic_review_complete" not in report
    review_file = manager._attempt_path(manager.get(rid), manager.get(rid).chapters[0]) / "review.json"
    assert not json.loads(review_file.read_text())["approved"]
    assert await counts(manager) == [0] * 4
    payload = {**approval(detail), "assisted_review_hash": report["report_hash"], "semantic_review_complete": False}
    assert (await client.post(f"/api/v1/studio/runs/{rid}/chapters/1/approve", json=payload)).status_code == 422
    payload["semantic_review_complete"] = True
    assert (await client.post(f"/api/v1/studio/runs/{rid}/chapters/1/approve", json=payload)).status_code == 200
    assert await counts(manager) == [1] * 4
    async with manager.session_factory() as session:
        record = (await session.scalars(select(ChapterCommit))).one()
        assert record.continuity_report_json["review_audit"]["approval"]["assisted_review_hash"] == report["report_hash"]


def review_factory(assessment):
    async def factory(session, run, chapter, package):
        provider = FakeLLMProvider()
        value = assessment(package)
        provider.enqueue_structured(value)
        agent = LiteraryReviewerAgent(AgentLLMClient(provider, agent_name="literary_reviewer"), get_default_registry())
        return agent, []
    return factory


async def test_assistant_preserves_negative_judgments_and_low_scores(studio):
    manager, client = studio
    novel = await new_story(client)
    rid = await start(client, novel)
    await completed_task(manager, rid)
    def assessment(package):
        return {"operation_reviews": [{"operation_id": op.operation_id, "verdict": "over-extracted", "reason": "需要人工缩小事实范围。"} for op in package.memory_result.batch.operations],
                "quality_scores": {key: 2 for key in ("readability", "character", "dialogue", "continuity", "progression")},
                "missing": ["尚需核对人物行动动机。"], "summary": "请先修订。"}
    manager.review_agent_factory = review_factory(assessment)
    await manager.assist_review(rid, 1)
    await completed_task(manager, rid)
    report = manager.chapter_detail(rid, 1)["assistance"]
    assert report["quality_scores"]["readability"] == 2
    assert report["missing"]
    assert all(r["verdict"] == "over-extracted" for r in report["operation_reviews"])
    assert await counts(manager) == [0] * 4


async def test_review_model_override_does_not_change_generation_config(studio, monkeypatch):
    manager, client = studio
    novel = await new_story(client)
    rid = await start(client, novel)
    await completed_task(manager, rid)
    run = manager.get(rid)
    run.mode = "live"
    monkeypatch.setattr(manager, "_validate_provider", lambda *args: None)
    captured = []
    async def factory(session, run, chapter, package):
        provider = FakeLLMProvider()
        provider.enqueue_structured({"operation_reviews": [{"operation_id": op.operation_id,
            "verdict": "correct", "reason": "已有正文证据"} for op in package.memory_result.batch.operations],
            "missing": [], "quality_scores": {key: 4 for key in
            ("readability", "character", "dialogue", "continuity", "progression")}, "summary": "检查完成"})
        agent = LiteraryReviewerAgent(AgentLLMClient(provider, agent_name="literary_reviewer", model="original", max_tokens=4096), get_default_registry())
        captured.append(agent)
        return agent, []
    manager.review_agent_factory = factory
    original = run.config.model
    response = await client.post(f"/api/v1/studio/runs/{rid}/chapters/1/assist-review", json={"model": "review-model", "max_output_tokens": 16384})
    assert response.status_code == 200
    await completed_task(manager, rid)
    assert captured[0].llm.model == "review-model"
    assert captured[0].llm.max_tokens == 16384
    assert run.config.model == original
    assert manager.chapter_detail(rid, 1)["assistance"]["source"] == "model"


async def test_unknown_operation_rejected_and_stale_advice_cannot_be_used(studio):
    manager, client = studio
    novel = await new_story(client)
    rid = await start(client, novel)
    await completed_task(manager, rid)
    manager.review_agent_factory = review_factory(lambda package: {
        "operation_reviews": [{"operation_id": "invented", "verdict": "correct", "reason": "假操作"}],
        "quality_scores": {key: 4 for key in ("readability", "character", "dialogue", "continuity", "progression")},
        "summary": "不可信", "missing": []})
    await manager.assist_review(rid, 1)
    await completed_task(manager, rid)
    assert manager.chapter_detail(rid, 1)["assistance"] is None
    assert "未完整核对" in manager.get(rid).error
    manager.review_agent_factory = None
    await manager.assist_review(rid, 1)
    await completed_task(manager, rid)
    original = manager.chapter_detail(rid, 1)
    old_hash = original["assistance"]["report_hash"]
    await manager.new_candidate(rid, 1, "memory", {"batch": original["package"]["memory_result"]["batch"]})
    await completed_task(manager, rid)
    current = manager.chapter_detail(rid, 1)
    assert current["assistance"] is None
    response = await client.post(f"/api/v1/studio/runs/{rid}/chapters/1/approve", json={**approval(current), "assisted_review_hash": old_hash})
    assert response.status_code == 409
    assert await counts(manager) == [0] * 4


async def test_finish_cancels_review_agent_without_committing(studio):
    manager, client = studio
    novel = await new_story(client)
    rid = await start(client, novel)
    await completed_task(manager, rid)
    started, stopped = asyncio.Event(), asyncio.Event()
    class HangingReviewer:
        async def assist_review(self, *args):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
    async def factory(*args):
        return HangingReviewer(), []
    manager.review_agent_factory = factory
    await manager.assist_review(rid, 1)
    await started.wait()
    await manager.finish(rid)
    assert stopped.is_set() and manager.tasks[rid].done()
    assert manager.get(rid).status == "archived"
    assert manager.chapter_detail(rid, 1)["assistance"] is None
    assert await counts(manager) == [0] * 4


async def test_review_guided_repair_creates_candidate_and_reassesses_without_commit(studio):
    from app.services.trial_artifacts import digest
    manager, client = studio
    novel = await new_story(client)
    rid = await start(client, novel)
    await completed_task(manager, rid)
    await manager.assist_review(rid, 1)
    await completed_task(manager, rid)
    run = manager.get(rid)
    chapter = run.chapters[0]
    path = manager._attempt_path(run, chapter) / chapter.assistance_file
    report = json.loads(path.read_text())
    report["source"] = "model"
    report["missing"] = ["检查遗漏"]
    report["report_hash"] = digest({k: v for k, v in report.items() if k != "report_hash"})
    path.write_text(json.dumps(report, ensure_ascii=False))
    old_attempt = chapter.attempt_id
    feedback_seen = []
    async def factory(session, run, chapter):
        service = await build_demo_services(session, uuid.UUID(run.novel_id), chapter.number)
        original = service.memory_service.extractor.extract
        async def extract(context, *, review_feedback=None):
            feedback_seen.append(review_feedback)
            return await original(context)
        service.memory_service.extractor.extract = extract
        return service
    manager.service_factory = factory
    response = await client.post(f"/api/v1/studio/runs/{rid}/chapters/1/repair-from-review", json={})
    assert response.status_code == 200, response.text
    await completed_task(manager, rid)
    detail = manager.chapter_detail(rid, 1)
    assert detail["attempt_id"] != old_attempt
    assert feedback_seen[0]["missing"] == ["检查遗漏"]
    assert detail["assistance"]["attempt_id"] == detail["attempt_id"]
    assert detail["approval"] is None
    assert await counts(manager) == [0] * 4
    from app.schemas.studio import StudioRun
    saved = json.loads((manager._path(run) / "run.json").read_text())
    assert StudioRun.model_validate(saved).chapters[0].action == "memory_assisted_repair"
