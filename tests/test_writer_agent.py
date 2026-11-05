"""WriterAgent tests: prose-only output, guard, prompt structure (N)."""

from __future__ import annotations

import pytest

from app.agents.writer import WriterAgent, WriterOutputError
from app.context.builder import ContextBuilder
from app.llm.client import AgentLLMClient
from app.llm.errors import ProviderRequestError
from app.llm.fake import FakeLLMProvider
from app.llm.prompts import get_default_registry
from tests.helpers import create_novel, create_rich_world


async def _context(session, novel, world):
    builder = ContextBuilder(session)
    return await builder.build_writer_context(
        novel.id, chapter_number=5, pov_character_id=world["characters"]["林凡"].id
    )


async def _writer(session):
    provider = FakeLLMProvider()
    llm = AgentLLMClient(provider, agent_name="writer", model="fake-model", temperature=0.85)
    return WriterAgent(llm=llm, prompt_registry=get_default_registry()), provider


async def test_n_writer_returns_prose_with_metadata(session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    agent, provider = await _writer(session)
    provider.enqueue_text("林凡走进大殿，目光扫过四周。")

    context = await _context(session, novel, world)
    draft = await agent.write_scene(context)

    assert draft.text == "林凡走进大殿，目光扫过四周。"
    assert draft.generation_metadata.agent == "writer"
    assert draft.generation_metadata.prompt_version == "writer_v2"
    assert len(draft.context_hash) == 64


async def test_writer_guard_strips_fences_and_scaffolding(session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    agent, provider = await _writer(session)
    provider.enqueue_text("```markdown\n林凡皱眉。\n```")

    draft = await agent.write_scene(await _context(session, novel, world))
    assert draft.text == "林凡皱眉。"

    provider.enqueue_text("以下是正文：林凡冷笑。")
    draft2 = await agent.write_scene(await _context(session, novel, world))
    assert draft2.text == "林凡冷笑。"


async def test_writer_guard_rejects_json_and_empty(session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    agent, provider = await _writer(session)
    context = await _context(session, novel, world)

    provider.enqueue_text('{"scene": "not prose"}')
    with pytest.raises(WriterOutputError):
        await agent.write_scene(context)

    provider.enqueue_text("   ")
    with pytest.raises(WriterOutputError):
        await agent.write_scene(context)


async def test_writer_prompt_contains_sections_and_not_director_notes(session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    agent, provider = await _writer(session)
    provider.enqueue_text("正文。")

    context = await _context(session, novel, world)
    await agent.write_scene(context)

    call = provider.calls[0]
    system_content = call[0].content
    user_content = next(m for m in call if m.role == "user").content
    assert system_content.startswith("# SYSTEM — Xuanhuan Chapter Writer")
    for section in ("POV KNOWLEDGE", "SCENE PLAN", "HARD CONSTRAINTS", "OUTPUT RULES"):
        assert section in user_content
    # author-level content must not appear anywhere in the request
    everything = "\n".join(m.content for m in call)
    assert "父亲仍然活着" not in everything
    assert "玄清真人是魔教卧底" not in everything
