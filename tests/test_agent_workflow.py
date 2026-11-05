"""Real LangGraph routing, failure stop/resume, and literary reviewer contract."""

import asyncio
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.agents.literary_reviewer import LiteraryReviewerAgent
from app.commit.models import ChapterPreviewPackage
from app.continuity.models import ContinuityRepairResult, ContinuityReport
from app.llm.client import AgentLLMClient
from app.llm.errors import ProviderTimeoutError
from app.llm.fake import FakeLLMProvider
from app.llm.prompts import get_default_registry
from app.quality.models import LiteraryScores
from app.services.canonical_commit import draft_hash_of
from app.services.chapter_generation import ChapterGenerationService, ContinuityRepairExhaustedError
from app.services.trial_artifacts import TrialAttempt
from app.workflow.chapter import ChapterWorkflow, workflow_metadata


@pytest.fixture
def package():
    return ChapterPreviewPackage.model_validate_json((
        Path(__file__).parent / "fixtures/real_memory_closure/chapter_01/preview_package.json"
    ).read_text())


def service_for(package, *, invalid=False, exhausted=False):
    good = ContinuityReport(valid=True, draft_hash=draft_hash_of(package.draft))
    bad = ContinuityReport(valid=False, draft_hash=draft_hash_of(package.draft))
    return ChapterGenerationService(
        None,
        SimpleNamespace(generate_preview=AsyncMock(return_value=package.plan_result)),
        SimpleNamespace(generate_preview=AsyncMock(return_value=package.draft)),
        SimpleNamespace(
            check_draft=AsyncMock(return_value=bad if invalid else good),
            repair_preview=AsyncMock(return_value=ContinuityRepairResult(
                draft=package.draft, report=bad if exhausted else good, exhausted=exhausted))),
        SimpleNamespace(extract_preview=AsyncMock(return_value=package.memory_result)),
        SimpleNamespace(commit_chapter=AsyncMock()),
    )


class ObservedAttempt(TrialAttempt):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.stages = []

    async def stage(self, name, model, produce):
        self.stages.append(name)
        return await super().stage(name, model, produce)


@pytest.mark.parametrize("invalid", [False, True])
async def test_production_generate_executes_graph_and_conditional_repair(package, tmp_path, invalid):
    service = service_for(package, invalid=invalid)
    attempt = ObservedAttempt(tmp_path / "a", {"state": package.story_state_hash})
    graph = ChapterWorkflow(service).build().get_graph()
    assert any(e.source == "continuity" and e.target == "repair" and e.conditional
               for e in graph.edges)
    result = await service.generate_preview(uuid.UUID(package.novel_id), "继续故事", checkpoints=attempt)
    expected = ["director", "draft", "continuity"]
    if invalid:
        expected.append("repair")
    assert attempt.stages == expected + ["memory", "package"]
    assert result.draft == package.draft
    assert service.continuity_service.repair_preview.await_count == int(invalid)
    service.commit_service.commit_chapter.assert_not_awaited()


async def test_exhausted_repair_never_reaches_memory_or_commit(package, tmp_path):
    service = service_for(package, invalid=True, exhausted=True)
    attempt = ObservedAttempt(tmp_path / "a", {"state": package.story_state_hash})
    with pytest.raises(ContinuityRepairExhaustedError):
        await service.generate_preview(uuid.UUID(package.novel_id), None, checkpoints=attempt)
    assert attempt.stages == ["director", "draft", "continuity", "repair"]
    service.memory_service.extract_preview.assert_not_awaited()
    service.commit_service.commit_chapter.assert_not_awaited()


async def test_resume_after_memory_failure_rehydrates_repaired_draft_and_skips_calls(package, tmp_path):
    service = service_for(package, invalid=True)
    service.memory_service.extract_preview.side_effect = [RuntimeError("provider failed"), package.memory_result]
    attempt = ObservedAttempt(tmp_path / "a", {"state": package.story_state_hash})
    with pytest.raises(RuntimeError, match="provider failed"):
        await service.generate_preview(uuid.UUID(package.novel_id), None, checkpoints=attempt)
    resumed = ObservedAttempt(attempt.path, attempt.signature, resume=True)
    result = await service.generate_preview(uuid.UUID(package.novel_id), None, checkpoints=resumed)
    assert result.draft == package.draft
    assert service.planning_service.generate_preview.await_count == 1
    assert service.draft_service.generate_preview.await_count == 1
    assert service.continuity_service.check_draft.await_count == 1
    assert service.continuity_service.repair_preview.await_count == 1
    assert service.memory_service.extract_preview.await_count == 2
    assert (attempt.path / "stage_package.json").exists()


async def test_graph_cancellation_never_starts_next_agent(package):
    service = service_for(package)
    started = asyncio.Event()

    async def hang(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    service.planning_service.generate_preview.side_effect = hang
    task = asyncio.create_task(service.generate_preview(uuid.UUID(package.novel_id), None))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    service.draft_service.generate_preview.assert_not_awaited()
    service.memory_service.extract_preview.assert_not_awaited()
    service.commit_service.commit_chapter.assert_not_awaited()


def assessment(package):
    scene = package.draft.scenes[0]
    return {
        "scores": {"readability": 4, "character": 4, "dialogue": 2, "continuity": 4, "progression": 4},
        "summary": "叙事清楚，对话声音有待区分。", "strengths": ["场景有推进"],
        "findings": [{"dimension": "dialogue", "scene_id": scene.scene_id,
                      "exact_quote": scene.text[:24], "explanation": "对话未体现人物身份。",
                      "suggestion": "以该人物的生活经验组织句子，保留事件。"}],
    }


async def test_literary_review_is_typed_exact_evidence_and_draft_bound(package):
    provider = FakeLLMProvider()
    provider.enqueue_structured(assessment(package))
    agent = LiteraryReviewerAgent(
        AgentLLMClient(provider, agent_name="literary_reviewer"), get_default_registry())
    report = await agent.review(package.draft, package.plan_result.plan)
    assert report.draft_hash == draft_hash_of(package.draft)
    assert report.needs_revision and report.advisory_only
    assert report.generation_metadata.agent == "literary_reviewer"
    assert len(provider.calls) == 1
    assert report.findings[0].exact_quote in package.draft.scenes[0].text
    with pytest.raises(ValidationError):
        LiteraryScores(readability=6, character=4, dialogue=4, continuity=4, progression=4)


@pytest.mark.parametrize("bad_field,bad_value", [("scene_id", "invented-scene"), ("exact_quote", "未出现在正文的原文")])
async def test_literary_review_rejects_fabricated_evidence(package, bad_field, bad_value):
    provider = FakeLLMProvider()
    raw = assessment(package)
    raw["findings"][0][bad_field] = bad_value
    provider.enqueue_structured(raw)
    agent = LiteraryReviewerAgent(
        AgentLLMClient(provider, agent_name="literary_reviewer"), get_default_registry())
    with pytest.raises(ValueError):
        await agent.review(package.draft, package.plan_result.plan)


async def test_literary_review_uses_agent_total_deadline(package):
    provider = FakeLLMProvider(simulate_latency_ms=1000)
    provider.enqueue_structured(assessment(package))
    agent = LiteraryReviewerAgent(AgentLLMClient(
        provider, agent_name="literary_reviewer", deadline_seconds=.02), get_default_registry())
    with pytest.raises(ProviderTimeoutError):
        await agent.review(package.draft, package.plan_result.plan)


def test_workflow_inventory_counts_roles_not_program_nodes():
    meta = workflow_metadata()
    assert meta["framework"] == "LangGraph"
    assert len(meta["agents"]) == 7
    assert "orchestrator" not in {agent["id"] for agent in meta["agents"]}
    assert len(meta["nodes"]) > len(meta["agents"])
