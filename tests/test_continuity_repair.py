"""Targeted repair tests: secret-safe projection, scene targeting,
preservation, DB zero-writes, bounded exhaustion."""

from __future__ import annotations

from sqlalchemy import func, select

from app.agents.continuity_checker import draft_hash
from app.agents.writer_repair import WriterRepairAgent, WriterRepairContext
from app.continuity.models import ContinuityCategory, ContinuityIssue, Severity
from app.continuity.repair_projection import project_issue_to_writer_safe
from app.context.builder import ContextBuilder
from app.draft.models import SceneDraft
from app.llm.client import AgentLLMClient
from app.llm.fake import FakeLLMProvider
from app.llm.prompts import get_default_registry
from app.schemas.agent_outputs import EntityRef, ScenePlan
from app.services.continuity import ContinuityService
from tests.draft_helpers import build_valid_plan_dict
from tests.helpers import (
    SECRET_STATEMENT,
    create_novel,
    create_rich_world,
)


def _make_agents(provider):
    repair = WriterRepairAgent(
        llm=AgentLLMClient(provider, agent_name="writer_repair", model="fake-model"),
        prompt_registry=get_default_registry(),
    )
    llm_checker = None  # deterministic_only by default in these tests
    return repair, llm_checker


async def _draft_world(session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    plan = __import__("app.schemas.agent_outputs", fromlist=["ChapterPlan"]).ChapterPlan.model_validate(
        build_valid_plan_dict(world)
    )
    # 5 scenes, only scene 3 has an issue (knowledge leak of a canonical secret)
    fact = await world["service"].add_fact(
        novel.id, "林凡的父亲仍然活着", secrecy="secret"
    )
    scene_plans = [
        ScenePlan(
            scene_id=f"s{i}", scene_index=i, source_beat_ids=["1"],
            pov=EntityRef(name="林凡"), location=EntityRef(name="天剑城"),
            scene_goal=f"场景{i}",
            required_outcomes=(["林凡取得玉佩"] if i == 3 else []),
            target_chars=800,
        )
        for i in range(1, 6)
    ]
    scene_texts = {
        f"s{i}": (f"场景{i}的正常叙事内容。" if i != 3 else f"场景三：林凡知道父亲仍然活着，心中震动。")
        for i in range(1, 6)
    }
    from datetime import UTC, datetime

    from app.draft.models import ChapterDraft

    draft = ChapterDraft(
        novel_id=str(novel.id),
        chapter_number=5,
        scenes=[
            SceneDraft(scene_id=sp.scene_id, scene_index=sp.scene_index, text=scene_texts[sp.scene_id])
            for sp in scene_plans
        ],
        full_text="\n\n".join(scene_texts.values()),
        chapter_plan_hash="0" * 64,
        story_context_hash="0" * 64,
        scene_plans=scene_plans,
        created_at=datetime.now(UTC),
    )
    return novel, world, plan, draft


def _service(session, provider):
    repair, _ = _make_agents(provider)
    return ContinuityService(
        session, llm_checker=None, repair_agent=repair,
        max_repairs=2, mode="deterministic_only",
    )


# ---------------------------------------------------------- projection (§24-28)

def test_knowledge_leak_projection_is_secret_safe():
    issue = ContinuityIssue(
        issue_id="i1", category=ContinuityCategory.knowledge_leak,
        severity=Severity.critical, scene_id="s3",
        evidence_excerpt="林凡知道父亲仍然活着",
        author_explanation=(
            "林凡不应知道父亲仍然活着并进入轮回秘境。"
        ),
        source="deterministic", blocking=True,
    )
    instruction = project_issue_to_writer_safe(issue, None)
    assert "父亲仍然活着" not in instruction.instruction
    assert "轮回秘境" not in instruction.instruction
    assert "不确定" in instruction.instruction
    # the author explanation is structurally absent from the instruction type
    assert "author_explanation" not in type(instruction).model_fields


def test_reveal_policy_projection_generic(session=None):
    issue = ContinuityIssue(
        issue_id="i2", category=ContinuityCategory.reveal_policy,
        severity=Severity.critical, scene_id="s1",
        author_explanation=f"正文明确揭露{SECRET_STATEMENT}，但本章只允许 hint_only。",
        source="deterministic", blocking=True,
    )
    instruction = project_issue_to_writer_safe(issue, None)
    assert SECRET_STATEMENT not in instruction.instruction
    assert "暗示" in instruction.instruction


def test_hard_constraint_projection_writer_visible():
    """苏青不能死亡 is a writer-visible constraint, so the instruction may
    name the requirement (§28)."""
    issue = ContinuityIssue(
        issue_id="i3", category=ContinuityCategory.hard_constraint,
        severity=Severity.critical, scene_id="s1",
        author_explanation="苏青死亡违反 hard constraint",
        source="deterministic", blocking=True,
    )
    instruction = project_issue_to_writer_safe(issue, None)
    assert "不要让该角色死亡" in instruction.instruction


# ------------------------------------------------------------ targeted (§54)

async def test_repair_only_affected_scene(session):
    novel, world, plan, draft = await _draft_world(session)
    provider = FakeLLMProvider()
    provider.enqueue_text("场景三：林凡心中泛起一丝疑惑，父亲的下落仍是谜。")
    service = _service(session, provider)

    result = await service.repair_preview(novel.id, draft, plan)

    texts = {s.scene_id: s.text for s in result.draft.scenes}
    assert texts["s1"] == "场景1的正常叙事内容。"
    assert texts["s2"] == "场景2的正常叙事内容。"
    assert "父亲仍然活着" not in texts["s3"], "blocking issue 修复后应消失"
    assert texts["s4"] == "场景4的正常叙事内容。"
    assert texts["s5"] == "场景5的正常叙事内容。"
    assert result.report.valid is True
    assert result.exhausted is False
    assert result.repair_history[0].scene_ids_repaired == ["s3"]


async def test_repair_targets_unique_excerpt_when_checker_omits_scene(session):
    from app.agents.continuity_checker import LLMContinuityChecker

    novel, world, plan, draft = await _draft_world(session)
    draft.scenes[2].text = "场景三只是安静的叙事。"
    draft.full_text = "\n\n".join(s.text for s in draft.scenes)
    checker_provider = FakeLLMProvider()
    checker_provider.enqueue_structured({"issues": [{
        "category": "scene_continuity", "severity": "error", "scene_id": None,
        "evidence_excerpt": "场景1的正常叙事内容。", "author_explanation": "与前文重复",
    }]}, {"issues": []})
    checker = LLMContinuityChecker(
        llm=AgentLLMClient(checker_provider, agent_name="continuity_checker", model="fake-model"),
        prompt_registry=get_default_registry(),
    )
    repair_provider = FakeLLMProvider()
    repair_provider.enqueue_text("场景一的修订叙事。")
    repair, _ = _make_agents(repair_provider)
    service = ContinuityService(session, llm_checker=checker, repair_agent=repair, mode="hybrid")
    result = await service.repair_preview(novel.id, draft, plan)
    assert result.report.valid and not result.exhausted
    assert result.repair_history[0].scene_ids_repaired == ["s1"]
    assert result.draft.scenes[0].text == "场景一的修订叙事。"
    assert result.draft.scenes[1].text == draft.scenes[1].text


# --------------------------------------------------------- preservation (§55)

async def test_repair_preserves_scene_outcomes(session):
    novel, world, plan, draft = await _draft_world(session)
    provider = FakeLLMProvider()
    provider.enqueue_text("场景三：林凡取得玉佩，同时对父亲的行踪心生疑窦。")
    service = _service(session, provider)

    result = await service.repair_preview(novel.id, draft, plan)

    repair_call = provider.calls[0]
    prompt = "\n".join(m.content for m in repair_call)
    assert "林凡取得玉佩" in prompt, "required outcome 必须出现在 preserve 清单"
    assert "取得玉佩" in result.draft.scenes[2].text


# ------------------------------------------------------------- no DB writes (§56)

async def test_repair_no_db_writes(session):
    novel, world, plan, draft = await _draft_world(session)
    provider = FakeLLMProvider()
    provider.enqueue_text("场景三修复版。")
    service = _service(session, provider)

    async def counts():
        from app.models import (
            Chapter,
            CharacterKnowledge,
            CharacterState,
            Event,
            Item,
            PlotDebt,
            PlotThread,
        )

        out = {}
        for model in (Chapter, Event, CharacterState, Item, PlotThread, PlotDebt, CharacterKnowledge):
            total = await session.execute(select(func.count()).select_from(model))
            out[model.__name__] = total.scalar_one()
        return out

    before = await counts()
    await service.repair_preview(novel.id, draft, plan)
    after = await counts()
    assert before == after


# ------------------------------------------------------------- exhausted (§57)

async def test_repair_exhausted_after_two_rounds(session):
    novel, world, plan, draft = await _draft_world(session)
    provider = FakeLLMProvider()
    # repairs keep reintroducing the leak
    provider.enqueue_text("场景三：林凡知道父亲仍然活着。")
    provider.enqueue_text("场景三：林凡仍知道父亲仍然活着。")
    service = _service(session, provider)

    result = await service.repair_preview(novel.id, draft, plan)

    assert result.exhausted is True
    assert len(result.repair_history) == 2, "不得超过 max_repairs"
    assert result.report.valid is False
    assert result.report.draft_hash == draft_hash(result.draft), "report 绑定最终 draft 版本"


# --------------------------------------------------------- checker prompt capture (§58)

async def test_checker_prompt_sees_secrets_and_wraps_draft_as_data(session):
    novel, world, plan, draft = await _draft_world(session)
    provider = FakeLLMProvider()
    provider.enqueue_structured({"issues": []})

    from app.agents.continuity_checker import LLMContinuityChecker

    checker = LLMContinuityChecker(
        llm=AgentLLMClient(provider, agent_name="continuity_checker", model="fake-model"),
        prompt_registry=get_default_registry(),
    )
    builder = ContextBuilder(session)
    context = await builder.build_continuity_context(
        novel.id, chapter_number=5, chapter_text=draft.full_text,
        chapter_plan=plan, scene_plans=draft.scene_plans, draft=draft,
    )
    await checker.check(context)

    call = provider.calls[0]
    everything = "\n".join(m.content for m in call)
    # checker is author-level: secrets ARE present in its context
    assert "父亲仍然活着" in everything or any(
        "secret" in f.content or "魔教卧底" in f.content for f in context.author_facts
    )
    # draft is delimited data, not instructions
    assert "<<<DRAFT" in everything and "DRAFT>>>" in everything


async def test_repair_prompt_never_contains_secrets(session):
    """§53 critical: the secret must never appear OUTSIDE the original-prose
    data block. The flawed prose itself (which the repair writer must see)
    contains the POV's wrong claim; the canonical truth and the checker's
    author_explanation must not leak into the instruction/section layers."""
    novel, world, plan, draft = await _draft_world(session)
    provider = FakeLLMProvider()
    provider.enqueue_text("场景三修复版。")
    service = _service(session, provider)

    result = await service.repair_preview(novel.id, draft, plan)

    for call in provider.calls:
        prompt = "\n".join(m.content for m in call)
        before_block, _, after_block = prompt.partition(">>>")  # writer_repair uses SCENE>>> marker
        original_block = prompt.split("<<<SCENE", 1)[-1].split("SCENE>>>", 1)[0]
        outside = prompt.replace(original_block, "")
        # canonical truth / author explanation absent from everything except
        # the flawed prose being repaired
        assert "轮回秘境" not in outside, "canonical truth 不得进入 repair prompt"
        assert "林凡不应知道" not in outside, "author_explanation 不得进入 repair prompt"
        # the flawed prose claim may only exist inside the data block
        assert "父亲仍然活着" in original_block or "父亲仍然活着" not in outside
        assert "轮回秘境" not in original_block
        # instructions are generic (secret-safe projection)
        assert "不确定" in prompt or "怀疑" in prompt
