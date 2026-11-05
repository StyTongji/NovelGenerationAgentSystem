"""Production-entry safety tests. All approval receipts here are test data."""
import asyncio
import hashlib
import json
import uuid
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import text

from app.commit.models import ChapterPreviewPackage, CommitError, ReviewApproval
from app.llm.budget import BudgetExceededError, TrialBudget
from app.llm.client import AgentLLMClient
from app.llm.errors import ProviderTimeoutError
from app.memory.catalog import EvidenceCatalogBuilder
from app.memory.models import FactRef, MemoryUpdateBatch
from app.services.canonical_commit import CanonicalCommitService
from app.services.chapter_generation import ChapterGenerationService
from app.services.memory_extraction import MemoryExtractionService
from tests.e2e.test_preview_pipeline import db_fingerprint

FIXTURE = Path(__file__).parent / "fixtures/real_memory_closure/chapter_01"


async def valid_package(session):
    for line in (FIXTURE / "before_state.sql").read_text().splitlines():
        if line.strip():
            await session.execute(text(line))
    await session.commit()
    package = ChapterPreviewPackage.model_validate_json((FIXTURE / "preview_package.json").read_text())
    catalog = EvidenceCatalogBuilder.build(package.draft)
    entry = catalog.entries[0]
    common = {"source_scene_id": entry.scene_id, "evidence_refs": [{"evidence_id": entry.evidence_id}]}
    batch = MemoryUpdateBatch.model_validate({"contract_version": 3, "operations": [
        {**common, "operation_id": "fact", "update_type": "story_fact_create",
         "story_fact": {"statement": "林凡已经抵达天剑城", "temporary_fact_ref": "new_fact_1"}},
        {**common, "operation_id": "knowledge", "update_type": "character_knowledge_update",
         "knowledge": {"character_id": "a5448bd9-6a04-4b0d-a22b-8156c393cc9a",
                       "fact_ref": {"temporary_fact_ref": "new_fact_1"}, "status": "known"}},
    ]})
    package.memory_result = await MemoryExtractionService(session, require_evidence_refs=True).validate_batch(
        uuid.UUID(package.novel_id), package.draft, package.plan_result.plan, batch, dry_run=True,
    )
    assert package.memory_result.validation_report.valid, package.memory_result.validation_report
    rehash(package)
    return package


def rehash(package):
    package.preview_package_hash = ChapterPreviewPackage.compute_hash(
        plan=package.plan_result.plan, draft=package.draft,
        continuity_report=package.continuity_report, memory_result=package.memory_result,
        story_state_hash=package.story_state_hash,
    )


def approval(package):
    return ReviewApproval(
        attempt_id="test-only", reviewed_by="pytest synthetic reviewer", approved=True,
        draft_hash=hashlib.sha256(package.draft.full_text.encode()).hexdigest(),
        memory_hash=ReviewApproval.hash_memory(package.memory_result),
        preview_package_hash=package.preview_package_hash,
        operation_reviews={op.operation_id: "correct" for op in package.memory_result.batch.operations},
        semantic_review_complete=True,
        quality_scores={k: 4 for k in ("readability", "character", "dialogue", "continuity", "progression")},
    )


def generation(session):
    return ChapterGenerationService(session, None, None, None, None, CanonicalCommitService(session))


async def test_production_v3_commit_dependency_provenance_and_duplicate(session):
    from sqlalchemy import select, func
    from app.models import Chapter, ChapterCommit, StorySnapshot, CharacterKnowledge
    package = await valid_package(session)
    receipt = approval(package)
    service = generation(session)
    result = await service.finalize(package, attempt_id="test-only", approval=receipt)
    for model in (Chapter, ChapterCommit, StorySnapshot):
        assert await session.scalar(select(func.count()).select_from(model)) == 1
    rows = (await session.execute(select(CharacterKnowledge))).scalars().all()
    assert any(row.learned_chapter == 1 for row in rows)
    record = (await session.execute(select(ChapterCommit))).scalar_one()
    for op in record.operations_json:
        for span in op["evidence"]:
            scene = next(s for s in package.draft.scenes if s.scene_id == span["scene_id"])
            assert scene.text[span["start_char"]:span["end_char"]] == span["exact_text"]
            assert span["evidence_id"]
    before = await db_fingerprint(session)
    duplicate = await service.finalize(package, attempt_id="test-only", approval=receipt)
    assert duplicate.duplicate and duplicate.commit_id == result.commit_id
    assert duplicate.after_story_state_hash == result.after_story_state_hash
    assert await db_fingerprint(session) == before


async def test_cancelled_generation_never_starts_later_agents_or_mutates_db(session):
    from app.llm.fake import FakeLLMProvider
    from app.llm.models import TextRequest
    package = await valid_package(session)
    before = await db_fingerprint(session)
    started = asyncio.Event()
    client = AgentLLMClient(FakeLLMProvider(simulate_latency_ms=60_000), agent_name="director")
    class Planning:
        async def generate_preview(self, *args, **kwargs):
            started.set()
            await client.run_request(TextRequest())
            return package.plan_result
    class Forbidden:
        def __getattr__(self, name):
            raise AssertionError(f"Later stage called after cancellation: {name}")
    service = ChapterGenerationService(session, Planning(), Forbidden(), Forbidden(), Forbidden(), Forbidden())
    task = asyncio.create_task(service.generate_preview(uuid.UUID(package.novel_id), None))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await db_fingerprint(session) == before


async def test_concurrent_production_finalize_has_one_atomic_winner(tmp_path):
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from sqlalchemy import select, func
    from app.models import Base, Chapter, ChapterCommit, StorySnapshot, StoryFact
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'race.db'}")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with maker() as setup:
            package = await valid_package(setup)
            facts_before = await setup.scalar(select(func.count()).select_from(StoryFact))
        reached = 0
        ready = asyncio.Event()
        async def commit():
            nonlocal reached
            async with maker() as session:
                service = generation(session)
                original = service.commit_service._commit_in_transaction
                async def synchronize(**kwargs):
                    nonlocal reached
                    reached += 1
                    if reached == 2:
                        ready.set()
                    await asyncio.wait_for(ready.wait(), 3)
                    return await original(**kwargs)
                service.commit_service._commit_in_transaction = synchronize
                return await service.finalize(package, attempt_id="test-only", approval=approval(package))
        outcomes = await asyncio.gather(commit(), commit(), return_exceptions=True)
        assert sum(not isinstance(result, BaseException) for result in outcomes) == 1
        assert any(isinstance(result, CommitError) for result in outcomes)
        async with maker() as check:
            for model in (Chapter, ChapterCommit, StorySnapshot):
                assert await check.scalar(select(func.count()).select_from(model)) == 1
            assert await check.scalar(select(func.count()).select_from(StoryFact)) == facts_before + 1
    finally:
        await engine.dispose()


@pytest.mark.parametrize("mutation", ["strip_refs", "downgrade", "offset", "catalog", "unknown_ref", "double_temp"])
async def test_production_rejects_tampering_even_with_recomputed_hash(session, mutation):
    package = await valid_package(session)
    batch = package.memory_result.batch
    if mutation == "strip_refs":
        for op in batch.operations:
            op.evidence_refs = []
    elif mutation == "downgrade":
        batch.contract_version = 2
    elif mutation == "offset":
        batch.operations[0].evidence[0].start_char += 1
    elif mutation == "catalog":
        package.memory_result.evidence_catalog.entries[0].exact_text = "fabricated"
    elif mutation == "unknown_ref":
        batch.operations[1].knowledge.fact_ref.temporary_fact_ref = "new_fact_999"
    else:
        extra = batch.operations[0].model_copy(deep=True)
        extra.operation_id = "other"
        extra.story_fact.statement = "another proposition"
        batch.operations.append(extra)
    rehash(package)
    before = await db_fingerprint(session)
    with pytest.raises(CommitError):
        await generation(session).finalize(package, attempt_id="test-only", approval=approval(package))
    assert await db_fingerprint(session) == before


async def test_production_requires_current_human_approval_and_bound_hash(session):
    package = await valid_package(session)
    service = generation(session)
    with pytest.raises(CommitError):
        await service.finalize(package)
    receipt = approval(package)
    package.memory_result.batch.candidate_author_summary = "edited after review"
    rehash(package)
    with pytest.raises(CommitError):
        await service.finalize(package, attempt_id="test-only", approval=receipt)
    receipt = approval(package)
    package.preview_package_hash = "0" * 64
    receipt.preview_package_hash = package.preview_package_hash
    with pytest.raises(CommitError):
        await service.finalize(package, attempt_id="test-only", approval=receipt)


@pytest.mark.parametrize("data", [{}, {"fact_id": "a", "temporary_fact_ref": "new_fact_1"}, {"fact_id": ""}])
def test_factref_exclusivity_is_schema_enforced(data):
    with pytest.raises(ValidationError):
        FactRef.model_validate(data)


async def test_real_memory_agent_uses_deadline_and_cancellation():
    from app.agents.memory_extractor import MemoryExtractorAgent
    from app.llm.prompts import get_default_registry
    from tests.test_llm_deadline import HangingProvider
    client = AgentLLMClient(HangingProvider(), agent_name="memory_extractor", model="test", deadline_seconds=.02)
    agent = MemoryExtractorAgent(client, get_default_registry())
    agent._build_prompt = lambda context: "test"
    with pytest.raises(ProviderTimeoutError):
        await agent.extract(None)
    client.deadline_seconds = 1
    task = asyncio.create_task(agent.extract(None))
    await asyncio.sleep(.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_json_repair_and_domain_operations_share_deadline():
    from app.llm.fake import FakeLLMProvider
    from app.memory.models import MemoryUpdateBatch
    provider = FakeLLMProvider(simulate_latency_ms=30)
    provider.enqueue_structured("bad", "bad", {"operations": []})
    client = AgentLLMClient(provider, agent_name="test", model="test", deadline_seconds=.05)
    with pytest.raises(ProviderTimeoutError):
        await client.generate_structured(system="", user="test", response_model=MemoryUpdateBatch)
    assert len(provider.calls) == 2
    client.deadline_seconds = .02
    with pytest.raises(ProviderTimeoutError):
        async with client.operation():
            await asyncio.sleep(.015)
            async with client.operation():
                await asyncio.sleep(.015)


async def test_5xx_bounded_retry_and_unknown_usage_are_recorded(tmp_path, monkeypatch):
    from app.llm.openai_compatible import OpenAICompatibleProvider
    monkeypatch.setenv("TEST_KEY", "test-secret")
    budget = TrialBudget(tmp_path / "usage.json", 1, max_calls=3)
    statuses = iter([503, 503, 200])
    def handle(request):
        status = next(statuses)
        return httpx.Response(status, json={"id": "req1", "model": "Auto", "choices": [{"message": {"content": "ok"}}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        provider = OpenAICompatibleProvider(base_url="https://test", api_key_env="TEST_KEY", http_client=http,
                                           retry_backoff_seconds=0, budget=budget)
        result = await AgentLLMClient(provider, agent_name="test", model="Auto", max_tokens=100).generate_text(system="", user="test")
    assert len(result.metadata.extra["http_attempts"]) == 3
    assert result.metadata.extra["usage_complete"] is False
    assert not result.metadata.extra["http_attempts"][-1]["model_disclosed"]
    rows = json.loads((tmp_path / "usage.json").read_text())["reservations"]
    assert all(r["charged_tokens"] > 0 for r in rows.values())
    with pytest.raises(BudgetExceededError):
        budget.reserve([], 100)


async def test_optional_parameters_only_follow_selected_model_capability(monkeypatch):
    from app.llm.openai_compatible import OpenAICompatibleProvider
    from app.llm.models import StructuredRequest
    from pydantic import BaseModel
    class Answer(BaseModel):
        ok: bool
    monkeypatch.setenv("TEST_KEY", "not-a-real-key")
    payloads = []
    def handle(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"model": "Auto", "choices": [
            {"message": {"content": '{"ok": true}'}}], "usage": {"total_tokens": None}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        provider = OpenAICompatibleProvider(base_url="https://test", api_key_env="TEST_KEY", http_client=http)
        provider.model_capabilities = {"verified": {"json_ok": True, "enable_thinking_false_accepted": True}}
        client = AgentLLMClient(provider, agent_name="test", max_tokens=100)
        for model in ("verified", "unverified"):
            result = await client.run_request(StructuredRequest(model=model, response_model=Answer,
                metadata={"prompt_version": "test-v3"}))
            assert not result.metadata.extra["usage_complete"]
            assert result.metadata.extra["http_attempts"][0]["prompt_version"] == "test-v3"
    assert payloads[0]["response_format"] == {"type": "json_object"}
    assert payloads[0]["enable_thinking"] is False
    assert "response_format" not in payloads[1] and "enable_thinking" not in payloads[1]


def test_catalog_covers_unterminated_lines_dialogue_and_all_scenes():
    from app.draft.models import ChapterDraft
    draft = ChapterDraft.model_validate_json((FIXTURE / "draft.json").read_text())
    draft.scenes[0].text = '无句号的一行\n“真的吗？”\n最后一句'
    draft.full_text = "\n\n".join(s.text for s in draft.scenes)
    catalog = EvidenceCatalogBuilder.build(draft, "wrong-caller-hash")
    assert catalog.draft_hash == hashlib.sha256(draft.full_text.encode()).hexdigest()
    for scene in draft.scenes:
        entries = [e for e in catalog.entries if e.scene_id == scene.scene_id]
        assert "".join(e.exact_text for e in entries).replace(" ", "") == "".join(scene.text.split())
        assert all(scene.text[e.start_char:e.end_char] == e.exact_text for e in entries)
