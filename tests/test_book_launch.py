"""Market → human gates → new world → production Studio preview, offline only."""
import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.agents.book_launch import MarketResearchAgent, StoryArchitectAgent
from app.config import Settings
from app.llm.client import AgentLLMClient
from app.llm.fake import FakeLLMProvider
from app.main import create_app
from app.models import Novel, Chapter, ChapterCommit, Event, StorySnapshot, StoryFact, CharacterKnowledge, Character, Faction
from app.schemas.book_launch import BookBlueprint, LaunchStart, LaunchConfig, LaunchBudget
from app.services.book_launch import BookLaunchManager
from app.services.book_launch_demo import demo_sample, demo_research, demo_blueprint
from app.services.market_sources import import_sample, parse_fanqie, collect_fanqie
from app.services.studio import StudioManager

BASE = "/api/v1/book-launch"


@pytest.fixture
async def lab(engine, tmp_path):
    studio = StudioManager(tmp_path / "studio", async_sessionmaker(engine, expire_on_commit=False), Settings())
    manager = BookLaunchManager(studio)
    app = create_app()
    app.state.studio, app.state.book_launch = studio, manager
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        yield manager, studio, client
    await manager.close()
    await studio.close()


async def complete(manager, id):
    await asyncio.wait_for(asyncio.shield(manager.tasks[id]), 20)
    item = manager.get(id)
    assert item.status not in {"failed", "interrupted"}, item.error
    return item


async def prepared(lab):
    m, _, client = lab
    r = await client.post(BASE, json={"sample": demo_sample().model_dump(mode="json")})
    assert r.status_code == 201, r.text
    item = await complete(m, r.json()["id"])
    r = await client.post(f"{BASE}/{item.id}/select", json={"topic_id": "topic_1", "research_hash": item.research_hash})
    assert r.status_code == 200, r.text
    return await complete(m, item.id)


async def table_counts(studio, tables):
    async with studio.session_factory() as s:
        return [await s.scalar(select(func.count()).select_from(t)) for t in tables]


async def test_human_gates_new_world_idempotency_and_studio_preview(lab):
    m, studio, client = lab
    item = await prepared(lab)
    tables = [Novel, Chapter, ChapterCommit, Event, StorySnapshot]
    assert await table_counts(studio, tables) == [0]*5
    bad = await client.post(f"{BASE}/{item.id}/approve", json={"blueprint_hash": "stale", "reviewed_by": "test", "approved": True})
    assert bad.status_code == 409
    bad = await client.post(f"{BASE}/{item.id}/chapters", json={"novel_id": str(item.novel_id)})
    assert bad.status_code == 409
    payload = {"blueprint_hash": item.blueprint_hash, "reviewed_by": "offline-test-author", "approved": True}
    approved = await client.post(f"{BASE}/{item.id}/approve", json=payload)
    assert approved.status_code == 200, approved.text
    again = await client.post(f"{BASE}/{item.id}/approve", json=payload)
    assert again.status_code == 200
    assert await table_counts(studio, tables) == [1, 0, 0, 0, 0]
    async with studio.session_factory() as s:
        novel = await s.get(Novel, item.novel_id)
        assert novel.premise == item.blueprint.public_premise
        assert item.blueprint.author_outline not in novel.premise
        assert novel.current_arc_id is not None
        secret = await s.scalar(select(StoryFact).where(StoryFact.secrecy == "secret"))
        knowledge = list(await s.scalars(select(CharacterKnowledge).where(CharacterKnowledge.fact_id == secret.id)))
        assert len(knowledge) == 1
        assert (await s.get(Character, knowledge[0].character_id)).name == "贺叔"
        faction = await s.scalar(select(Faction).where(Faction.novel_id == novel.id))
        assert (await s.get(Character, faction.leader_character_id)).name == "贺叔"
    created = await client.post(f"{BASE}/{item.id}/chapters", json={"novel_id": str(item.novel_id), "chapter_count": 3})
    assert created.status_code == 200, created.text
    run_id = created.json()["id"]
    await asyncio.wait_for(asyncio.shield(studio.tasks[run_id]), 20)
    run = studio.get(run_id)
    assert run.status == "awaiting_review", run.error
    assert "这是计划，尚未发生" in run.config.chapter_directions[0]
    detail = studio.chapter_detail(run_id, 1)
    assert detail["package"]["memory_result"]["validation_report"]["valid"]
    assert detail["package"]["memory_result"]["dry_run"]["applicable"]
    assert await table_counts(studio, tables) == [1, 0, 0, 0, 0]
    duplicate = await client.post(f"{BASE}/{item.id}/chapters", json={"novel_id": str(item.novel_id), "chapter_count": 3})
    assert duplicate.json()["id"] == run_id
    assert (await client.get("/book-launch")).status_code == 200
    assert (await client.get("/static/book_launch.js")).status_code == 200


async def test_select_stale_unknown_and_live_synthetic_rejected(lab):
    m, studio, client = lab
    item = await m.create(LaunchStart(sample=demo_sample()))
    await complete(m, item.id)
    for selection in [{"topic_id": "topic_1", "research_hash": "old"}, {"topic_id": "unknown", "research_hash": item.research_hash}]:
        r = await client.post(f"{BASE}/{item.id}/select", json=selection)
        assert r.status_code == 409
    r = await client.post(BASE, json={"mode": "live", "sample": demo_sample().model_dump(mode="json")})
    assert r.status_code == 422
    assert await table_counts(studio, [Novel]) == [0]


async def test_fork_reuses_research_without_call_and_keeps_source(lab):
    manager, studio, client = lab
    source = await manager.create(LaunchStart(sample=demo_sample()))
    await complete(manager, source.id)
    original_hash = source.research_hash
    result = await client.post(f"{BASE}/{source.id}/fork-research", json={
        "provider": "scnet", "model": "another-model", "token_budget": 100000, "max_calls": 8,
    })
    assert result.status_code == 201, result.text
    fork = manager.get(result.json()["id"])
    assert fork.source_session_id == source.id and fork.research_hash == original_hash
    assert fork.config.model == "another-model" and fork.status == "awaiting_topic"
    assert fork.id not in manager.tasks and manager.budget(fork).summary()["run"]["calls"] == 0
    await manager.cancel(fork.id)
    await manager.resume(fork.id)
    assert fork.status == "awaiting_topic" and fork.id not in manager.tasks
    response = await client.post(f"{BASE}/{fork.id}/select", json={"topic_id": "topic_1", "research_hash": original_hash})
    assert response.status_code == 200
    await complete(manager, fork.id)
    assert fork.status == "awaiting_approval" and source.status == "awaiting_topic"
    assert await table_counts(studio, [Novel]) == [0]
    source.research.findings[0].observation = "tampered"
    rejected = await client.post(f"{BASE}/{source.id}/fork-research", json={"provider": "scnet", "model": "another-model"})
    assert rejected.status_code == 409


async def test_revision_preserves_original_and_invalidates_old_approval(lab):
    manager, studio, client = lab
    item = await prepared(lab)
    original_hash = item.blueprint_hash
    revised = item.blueprint.model_dump(mode="json")
    revised["public_premise"] = "一名契书吏面临一场尚未发生的交易。"
    r = await client.post(f"{BASE}/{item.id}/revise-blueprint", json={"source_hash": original_hash, "blueprint": revised})
    assert r.status_code == 200, r.text
    assert item.blueprint_hash != original_hash
    assert json.loads((manager.path(item) / "blueprint.json").read_text())["public_premise"] != revised["public_premise"]
    assert (manager.path(item) / f"blueprint_{item.blueprint_hash}.json").exists()
    stale = await client.post(f"{BASE}/{item.id}/approve", json={"blueprint_hash": original_hash, "reviewed_by": "test", "approved": True})
    assert stale.status_code == 409
    await manager.cancel(item.id)
    await manager.resume(item.id)
    assert item.status == "awaiting_approval" and item.blueprint.public_premise == revised["public_premise"]
    assert await table_counts(studio, [Novel]) == [0]


async def test_resume_reuses_published_checkpoint_and_rejects_changed_input(lab, monkeypatch):
    m, _, _ = lab
    item = await prepared(lab)
    await m.cancel(item.id)
    async def must_not_run(*args):
        raise AssertionError("completed stage must not call LLM again")
    monkeypatch.setattr(StoryArchitectAgent, "design", must_not_run)
    # Same checkpoint even after spending limits change.
    await m.update_budget(item.id, LaunchBudget(token_budget=300000, max_calls=20))
    await m.resume(item.id)
    await complete(m, item.id)
    assert item.status == "awaiting_approval"
    await m.cancel(item.id)
    item.config.brief = "modified input"
    with pytest.raises(ValueError, match="输入或 prompt"):
        await m.resume(item.id)


async def test_cancel_awaits_inflight_agent_no_subsequent_agent_or_db(lab, monkeypatch):
    m, studio, _ = lab
    entered, finished = asyncio.Event(), asyncio.Event()
    async def hang(*args):
        entered.set()
        try:
            await asyncio.Future()
        finally:
            finished.set()
    monkeypatch.setattr(MarketResearchAgent, "research", hang)
    item = await m.create(LaunchStart(sample=demo_sample()))
    await asyncio.wait_for(entered.wait(), 2)
    await m.cancel(item.id)
    assert finished.is_set() and m.tasks[item.id].done()
    assert item.status == "cancelled" and item.blueprint is None
    assert await table_counts(studio, [Novel, Chapter]) == [0, 0]


async def test_bootstrap_transaction_failure_and_receipt_recovery(lab, monkeypatch):
    from app.services.story_state import StoryStateService
    from app.schemas.book_launch import BlueprintApproval
    m, studio, _ = lab
    item = await prepared(lab)
    approval = BlueprintApproval(blueprint_hash=item.blueprint_hash, reviewed_by="test", approved=True)
    original = StoryStateService.bootstrap_world
    async def fail(*args):
        raise ValueError("simulated domain failure")
    monkeypatch.setattr(StoryStateService, "bootstrap_world", fail)
    with pytest.raises(ValueError):
        await m.approve(item.id, approval)
    assert await table_counts(studio, [Novel, Character]) == [0, 0]
    monkeypatch.setattr(StoryStateService, "bootstrap_world", original)
    # Simulate a receipt-save interruption after SQL commit, then retry.
    save = m.save
    def fail_receipt(value):
        if value.status == "created":
            raise OSError("receipt interrupted")
        save(value)
    monkeypatch.setattr(m, "save", fail_receipt)
    with pytest.raises(OSError):
        await m.approve(item.id, approval)
    assert await table_counts(studio, [Novel]) == [1]
    recovered = BookLaunchManager(studio)
    await recovered.approve(item.id, approval)
    assert recovered.get(item.id).status == "created"
    assert await table_counts(studio, [Novel, Character]) == [1, 3]


async def test_agent_rejects_fabricated_sources_and_uses_common_deadline():
    sample = demo_sample()
    fake = FakeLLMProvider()
    fake.enqueue_structured(demo_research("invented_source"))
    agent = MarketResearchAgent(AgentLLMClient(fake, agent_name="market_research"))
    with pytest.raises(ValueError, match="未知来源"):
        await agent.research(LaunchConfig(sample=sample))
    from app.llm.errors import ProviderTimeoutError
    fake.simulate_latency_ms = 5000
    fake.enqueue_structured(demo_research("demo_1"))
    agent = MarketResearchAgent(AgentLLMClient(fake, agent_name="market_research", deadline_seconds=0.03))
    with pytest.raises(ProviderTimeoutError):
        await agent.research(LaunchConfig(sample=sample))


async def test_live_runtime_budget_trace_and_key_not_persisted(lab):
    m, _, _ = lab
    sample = demo_sample().model_copy(update={"synthetic": False})
    # Mock the HTTP transport; exercises the production provider without network.
    runtime = m.runtime
    seen = []
    def mock_runtime(item):
        clients, providers = runtime(item)
        async def handler(request):
            seen.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "test-request", "model": "mock-response-model",
                "choices": [{"message": {"content": demo_research("demo_1").model_dump_json()}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 200, "completion_tokens": 300, "total_tokens": 500}})
        for p in providers:
            p._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return clients, providers
    m.runtime = mock_runtime
    item = await m.create(LaunchStart(sample=sample, mode="live", provider="custom_input",
        custom_base_url="https://mock.invalid/v1", model="model-under-test", api_key="test-secret-123"))
    await complete(m, item.id)
    assert seen[0]["model"] == "model-under-test"
    assert m.budget(item).summary()["run"]["known_tokens"] == 500
    traces = (m.path(item) / "llm_calls.jsonl").read_text()
    assert "market_research" in traces and "mock-response-model" in traces
    assert "test-secret-123" not in "".join(p.read_text() for p in m.path(item).rglob("*.json*"))


def test_blueprint_rejects_dangling_references_and_invalid_chapter_order():
    b = demo_blueprint(demo_research("demo_1").topics[0]).model_dump(mode="json")
    b["world"]["characters"][0]["realm_name"] = "unknown"
    with pytest.raises(ValidationError, match="realm/location"):
        BookBlueprint.model_validate(b)
    b = demo_blueprint(demo_research("demo_1").topics[0]).model_dump(mode="json")
    b["opening_chapters"][0]["chapter"] = 2
    with pytest.raises(ValidationError, match="1..10"):
        BookBlueprint.model_validate(b)


def test_sources_import_provenance_and_public_ssr_no_invented_rank():
    from app.services.fanqie_text import decode_text
    assert decode_text(chr(58344+69)) == "1"
    sample = demo_sample()
    assert import_sample(sample.model_dump_json()).books[0].id == "demo_1"
    csv = "id,platform,title,source_url,observed_at,metric_name,metric_value,rank\nq1,起点,用户摘录标题,https://www.qidian.com/rank/xuanhuan/,2026-11-02T12:00:00+08:00,月票,123,\n"
    imported = import_sample(csv, "csv")
    assert imported.books[0].platform == "起点" and imported.books[0].rank is None
    html = 'window.__INITIAL_STATE__=' + json.dumps({"rank": {"book_list": [{"bookName": "元数据样本", "bookId": "1", "readCount": 0}]}}) + ";"
    books = parse_fanqie(html, url="https://fanqienovel.com/rank/1_2_258", category="传统玄幻", rank_type="阅读榜",
        observed_at=sample.books[0].observed_at, limit=10)
    assert books[0].metric_value == "0" and books[0].rank is None
    with pytest.raises(ValueError, match="SSR"):
        parse_fanqie("captcha", url="x", category="x", rank_type="x", observed_at=sample.books[0].observed_at, limit=1)


async def test_collector_stops_on_blocked_page(monkeypatch):
    from app.services import market_sources
    calls = []
    def blocked(request):
        calls.append(request.url)
        return httpx.Response(202, text="browser verification")
    client_cls = httpx.AsyncClient
    def client(**kwargs):
        kwargs.pop("proxy", None)
        return client_cls(transport=httpx.MockTransport(blocked), **kwargs)
    monkeypatch.setattr(market_sources.httpx, "AsyncClient", client)
    with pytest.raises(ValueError, match="不绕过"):
        await collect_fanqie([258, 257])
    assert len(calls) == 1


async def test_collector_normalizes_socks_proxy_and_respects_no_proxy(monkeypatch):
    from app.services import market_sources
    client_cls = httpx.AsyncClient
    seen = []
    html = 'window.__INITIAL_STATE__=' + json.dumps({"rank": {"book_list": [{"bookName": "公开样本", "bookId": "1"}]}}) + ';'

    def client(**kwargs):
        seen.append(kwargs.pop("proxy"))
        return client_cls(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=html)), **kwargs)

    monkeypatch.setattr(market_sources.httpx, "AsyncClient", client)
    monkeypatch.setattr(market_sources, "getproxies", lambda: {"https": "socks://127.0.0.1:7891"})
    monkeypatch.setattr(market_sources, "proxy_bypass", lambda host: False)
    sample = await collect_fanqie([258], limit=1)
    assert seen[-1] == "socks5://127.0.0.1:7891"
    assert sample.books[0].title == "公开样本" and not sample.synthetic

    monkeypatch.setattr(market_sources, "proxy_bypass", lambda host: True)
    await collect_fanqie([258], limit=1)
    assert seen[-1] is None
