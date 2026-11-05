"""Budget controls preserve spending history and valid generation checkpoints."""
import asyncio
import json
import uuid

import pytest

from app.llm.budget import BudgetExceededError, TrialBudget
from app.llm.models import LLMMessage, StructuredRequest
from app.schemas.agent_outputs import ScenePlanBatch
from tests.test_studio import studio, new_story, completed_task, counts


def test_summary_distinguishes_known_usage_and_unknown_reservations(tmp_path):
    path = tmp_path / "usage.json"
    budget = TrialBudget(path, 1, chapter_tokens=1000, run_tokens=2000)
    first = budget.reserve([], 100)
    budget.settle(first, 40)
    budget.reserve([], 200)
    TrialBudget(path, 2).reserve([], 300)
    summary = budget.summary()
    assert summary["chapter"] == {"calls": 2, "charged_tokens": 240, "known_tokens": 40,
                                   "unknown_calls": 1, "unknown_reserved_tokens": 200}
    assert summary["run"]["charged_tokens"] == 540
    assert summary["run"]["unknown_calls"] == 2
    assert summary["limits"]["chapter_tokens"] == 1000


@pytest.mark.parametrize("limits,reason", [
    ({"max_calls": 1}, "请求次数"),
    ({"chapter_tokens": 150}, "下次预留"),
    ({"run_tokens": 150}, "任务已计入"),
])
def test_rejection_explains_limit_and_does_not_reserve(tmp_path, limits, reason):
    path = tmp_path / "usage.json"
    budget = TrialBudget(path, 1, **limits)
    budget.reserve([], 100)
    original = path.read_bytes()
    with pytest.raises(BudgetExceededError, match=reason):
        budget.reserve([], 60)
    assert path.read_bytes() == original


async def test_budget_http_update_resume_keeps_ledger_and_director_checkpoint(studio):
    from app.services.studio_demo import build_demo_services
    manager, client = studio
    reached, hang = asyncio.Event(), asyncio.Event()
    drafts = []

    async def factory(session, run, chapter):
        service = await build_demo_services(session, uuid.UUID(run.novel_id), chapter.number)
        original = service.draft_service.generate_preview
        async def draft(*args, **kwargs):
            drafts.append(1)
            if len(drafts) == 1:
                reached.set()
                await hang.wait()
            return await original(*args, **kwargs)
        service.draft_service.generate_preview = draft
        return service

    manager.service_factory = factory
    novel = await new_story(client)
    run = (await client.post("/api/v1/studio/runs", json={"novel_id": novel, "chapter_count": 1,
        "chapter_tokens": 200000, "run_tokens": 2000000, "max_calls": 40})).json()
    rid = run["id"]
    await asyncio.wait_for(reached.wait(), 10)
    original_attempt = manager.get(rid).chapters[0].attempt_id
    url = f"/api/v1/studio/runs/{rid}/budget"
    limits = {"chapter_tokens": 300000, "run_tokens": 3000000, "max_calls": 50}
    assert (await client.post(url, json=limits)).status_code == 409
    await client.post(f"/api/v1/studio/runs/{rid}/cancel")
    path = manager.root / rid
    checkpoint = next(path.glob("chapter_*/attempts/*/stage_director.json"))
    original = checkpoint.read_bytes()
    manifest = checkpoint.with_name("manifest.json").read_bytes()
    budget = TrialBudget(path / "usage_ledger.json", 1)
    budget.reserve([], 110000)  # Simulate an unknown-usage request; never release it.
    ledger = (path / "usage_ledger.json").read_bytes()
    result = await client.post(url, json=limits)
    assert result.status_code == 200, result.text
    assert result.json()["config"]["chapter_tokens"] == 300000
    assert (path / "usage_ledger.json").read_bytes() == ledger
    assert checkpoint.read_bytes() == original
    summary = (await client.get(url)).json()
    assert summary["chapter"]["unknown_reserved_tokens"] == 110000
    assert summary["limits"]["max_calls"] == 50
    too_small = await client.post(url, json={**limits, "chapter_tokens": 100000})
    assert too_small.status_code == 409
    assert (await client.post(url, json={**limits, "max_calls": 101})).status_code == 422
    assert (await client.post(f"/api/v1/studio/runs/{rid}/resume")).status_code == 200
    await completed_task(manager, rid)
    assert checkpoint.read_bytes() == original
    assert checkpoint.with_name("manifest.json").read_bytes() == manifest
    assert manager.get(rid).chapters[0].attempt_id == original_attempt
    assert (path / "usage_ledger.json").read_bytes() == ledger
    assert any(e.get("stage") == "director" and e.get("status") == "cached" for e in manager.events(rid))
    assert await counts(manager) == [0] * 4
    # Unrelated content changes must still fail checkpoint reuse.
    await client.post(f"/api/v1/studio/runs/{rid}/cancel")
    manager.get(rid).config.direction = "changed content"
    await client.post(f"/api/v1/studio/runs/{rid}/resume")
    await manager.tasks[rid]
    assert "Resume refused" in manager.get(rid).error


def test_schema_minification_preserves_full_semantics():
    request = StructuredRequest(response_model=ScenePlanBatch)
    encoded = request.json_instruction().split("schema:\n", 1)[1]
    assert json.loads(encoded) == ScenePlanBatch.model_json_schema()
    assert len(encoded) < len(json.dumps(ScenePlanBatch.model_json_schema(), ensure_ascii=False))


async def test_json_repair_keeps_only_latest_failed_response():
    from pydantic import BaseModel
    from app.llm.fake import FakeLLMProvider
    class Result(BaseModel):
        value: int
    provider = FakeLLMProvider()
    provider.enqueue_structured("invalid-first-" + "x" * 1000, "invalid-second", '{"value":3}')
    result = await provider.generate_structured(StructuredRequest(response_model=Result,
        messages=[LLMMessage(role="user", content="original task")]))
    assert result.data.value == 3
    assert len(result.raw_outputs) == 3  # audit retained outside the next prompt
    final_messages = provider.calls[-1]
    assert any("original task" in m.content for m in final_messages)
    assert any("invalid-second" in m.content for m in final_messages)
    assert not any("invalid-first-" in m.content for m in final_messages)


async def test_empty_truncated_output_stops_without_repeating_same_limit():
    from app.llm.fake import FakeLLMProvider
    from app.llm.models import Usage
    from app.llm.errors import RetryExhaustedError
    from pydantic import BaseModel
    class Result(BaseModel):
        value: int
    class Truncated(FakeLLMProvider):
        async def _complete(self, messages, **kwargs):
            self.calls.append(messages)
            return "", "length", Usage(completion_tokens=4096, total_tokens=4096)
    provider = Truncated()
    with pytest.raises(RetryExhaustedError, match="单次输出上限"):
        await provider.generate_structured(StructuredRequest(response_model=Result))
    assert len(provider.calls) == 1


def test_single_output_limit_flows_to_all_agents_without_changing_old_defaults():
    from app.config import Settings
    from app.schemas.studio import RunInput, StudioRun
    from app.services.studio import StudioManager
    run = StudioRun(id="test", novel_id=str(uuid.uuid4()), title="test", mode="live",
        status="blocked", current_chapter=1, start_chapter=1, total_chapters=1, chapters=[],
        config=RunInput(novel_id=uuid.uuid4(), mode="live", provider="scnet", model="test", max_output_tokens=8192))
    manager = object.__new__(StudioManager)
    manager.settings = Settings()
    assert all(agent.max_tokens == 8192 for agent in manager._effective_settings(run).llm.agents.values())
    run.config.max_output_tokens = None
    assert manager._effective_settings(run).llm.agents["scene_planner"].max_tokens == 4096
