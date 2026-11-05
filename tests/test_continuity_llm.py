"""LLM semantic checker, merger, and hallucination guard tests."""

from __future__ import annotations

import pytest

from app.agents.continuity_checker import LLMContinuityChecker, draft_hash
from app.continuity.merger import ContinuityIssueMerger
from app.continuity.models import ContinuityCategory, ContinuityIssue, ContinuityReport
from app.context.builder import ContextBuilder
from app.draft.models import SceneDraft
from app.llm.client import AgentLLMClient
from app.llm.fake import FakeLLMProvider
from app.llm.prompts import get_default_registry
from app.models.enums import Severity
from app.schemas.agent_outputs import EntityRef, ScenePlan
from tests.draft_helpers import build_valid_plan_dict
from tests.helpers import SECRET_STATEMENT, create_novel, create_rich_world


async def _context_with_text(session, novel, world, scene_texts: dict[str, str]):
    from datetime import UTC, datetime

    from app.draft.models import ChapterDraft

    plan = __import__("app.schemas.agent_outputs", fromlist=["ChapterPlan"]).ChapterPlan.model_validate(
        build_valid_plan_dict(world)
    )
    scenes = [
        ScenePlan(
            scene_id=f"s{i}", scene_index=i, source_beat_ids=[str(i)],
            pov=EntityRef(name="林凡"), location=EntityRef(name="天剑城"),
            scene_goal="g", target_chars=800,
        )
        for i in range(1, len(scene_texts) + 1)
    ]
    draft = ChapterDraft(
        novel_id=str(novel.id),
        chapter_number=5,
        scenes=[
            SceneDraft(scene_id=sid, scene_index=i, text=text)
            for i, (sid, text) in enumerate(scene_texts.items(), start=1)
        ],
        full_text="\n\n".join(scene_texts.values()),
        chapter_plan_hash="0" * 64,
        story_context_hash="0" * 64,
        scene_plans=scenes,
        created_at=datetime.now(UTC),
    )
    builder = ContextBuilder(session)
    context = await builder.build_continuity_context(
        novel.id, chapter_number=5, chapter_text=draft.full_text,
        chapter_plan=plan, scene_plans=scenes, draft=draft,
    )
    return context, draft, plan


def _checker(provider) -> LLMContinuityChecker:
    return LLMContinuityChecker(
        llm=AgentLLMClient(provider, agent_name="continuity_checker", model="fake-model", temperature=0.1),
        prompt_registry=get_default_registry(),
    )


# ------------------------------------------------------------------ case 7

async def test_case7_paraphrased_leak_caught_by_llm_checker(session):
    """规则层漏检的改写泄密由语义层捕获。"""
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    await world["service"].add_fact(novel.id, SECRET_STATEMENT, secrecy="top_secret")
    context, draft, plan = await _context_with_text(
        session, novel, world,
        {"s1": "林凡早已确定，自己的师尊实际上一直替魔门效命。"},
    )
    provider = FakeLLMProvider()
    provider.enqueue_structured({
        "issues": [{
            "category": "knowledge_leak",
            "severity": "critical",
            "scene_id": "s1",
            "evidence_excerpt": "林凡早已确定，自己的师尊实际上一直替魔门效命。",
            "author_explanation": f"正文以改写方式确认了秘密『{SECRET_STATEMENT}』，而 POV 林凡并不知道该事实",
            "suggested_action": "改为怀疑或模糊线索",
        }]
    })
    issues, metadata = await _checker(provider).check(context)

    assert any(
        i.category == ContinuityCategory.knowledge_leak and i.severity == Severity.critical
        for i in issues
    )
    assert metadata.agent == "continuity_checker"
    # deterministic layer indeed missed it (paraphrase)
    det = __import__("app.continuity.deterministic", fromlist=["DeterministicContinuityChecker"]).DeterministicContinuityChecker().check(context)
    assert not any(i.category == ContinuityCategory.knowledge_leak for i in det)


# ------------------------------------------------------------- hallucination

async def test_hallucinated_scene_id_dropped(session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    context, draft, plan = await _context_with_text(
        session, novel, world, {"s1": "正常正文。"}
    )
    provider = FakeLLMProvider()
    provider.enqueue_structured({
        "issues": [
            {
                "category": "knowledge_leak", "severity": "critical",
                "scene_id": "scene_999",
                "evidence_excerpt": "x",
                "author_explanation": "hallucinated scene",
            },
            {
                "category": "plan_adherence", "severity": "warning",
                "scene_id": "s1",
                "evidence_excerpt": "不存在的证据文本",
                "author_explanation": "evidence blanked but issue kept",
            },
        ]
    })
    issues, _ = await _checker(provider).check(context)

    assert all(i.scene_id != "scene_999" for i in issues), "hallucinated scene 必须被丢弃"
    kept = [i for i in issues if i.scene_id == "s1"]
    assert kept and kept[0].evidence_excerpt is None, "伪造证据必须被清空，issue 保留但不可信"


async def test_missing_scene_id_is_resolved_only_from_unique_exact_excerpt(session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    context, _, _ = await _context_with_text(session, novel, world, {
        "s1": "他想起那道熟悉的声音。重复的话。",
        "s2": "她摸到了旧刻痕。重复的话。",
    })
    provider = FakeLLMProvider()
    provider.enqueue_structured({"issues": [
        {"category": "knowledge_leak", "severity": "error", "scene_id": None,
         "evidence_excerpt": "她摸到了旧刻痕。", "author_explanation": "泄漏"},
        {"category": "other", "severity": "error", "scene_id": None,
         "evidence_excerpt": "重复的话。", "author_explanation": "模糊"},
        {"category": "other", "severity": "error", "scene_id": None,
         "evidence_excerpt": "正文中不存在", "author_explanation": "不可信"},
    ]})
    issues, _ = await _checker(provider).check(context)
    assert [i.scene_id for i in issues] == ["s2", None, None]
    assert issues[2].evidence_excerpt is None
    assert all(i.blocking for i in issues)
    request = "\n".join(m.content for m in provider.calls[0])
    assert "<<<SCENE s1>>>" in request and "<<<SCENE s2>>>" in request


async def test_unknown_category_mapped_to_other(session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    context, draft, plan = await _context_with_text(
        session, novel, world, {"s1": "正文。"}
    )
    provider = FakeLLMProvider()
    provider.enqueue_structured({
        "issues": [{
            "category": "made_up_category", "severity": "warning",
            "scene_id": "s1", "author_explanation": "x",
        }]
    })
    issues, _ = await _checker(provider).check(context)
    assert issues and issues[0].category == ContinuityCategory.other


# ------------------------------------------------------------------ merger

def test_merger_keeps_deterministic_severity():
    det = ContinuityIssue(
        issue_id="d1", category=ContinuityCategory.hard_constraint,
        severity=Severity.critical, scene_id="s1",
        evidence_excerpt="苏青魂飞魄散", author_explanation="违反 hard constraint",
        source="deterministic", blocking=True,
    )
    llm = ContinuityIssue(
        issue_id="l1", category=ContinuityCategory.hard_constraint,
        severity=Severity.warning, scene_id="s1",
        evidence_excerpt="苏青魂飞魄散", author_explanation="疑似死亡",
        source="llm", blocking=False,
    )
    merged = ContinuityIssueMerger().merge([det], [llm])
    assert len(merged) == 1, "同一问题不得重复显示"
    assert merged[0].severity == Severity.critical, "LLM 不得降级 deterministic finding"
    assert merged[0].blocking is True


def test_merger_merges_evidence_keeps_deterministic_explanation():
    det = ContinuityIssue(
        issue_id="d1", category=ContinuityCategory.knowledge_leak,
        severity=Severity.error, scene_id="s2",
        evidence_excerpt="苏青魂飞魄散的场面被详细描写",
        author_explanation="deterministic 解释",
        source="deterministic", blocking=True,
    )
    llm = ContinuityIssue(
        issue_id="l1", category=ContinuityCategory.knowledge_leak,
        severity=Severity.error, scene_id="s2",
        evidence_excerpt="苏青魂飞魄散", author_explanation="llm 解释",
        suggested_action="改写",
        source="llm", blocking=True,
    )
    merged = ContinuityIssueMerger().merge([det], [llm])
    assert len(merged) == 1
    assert merged[0].author_explanation == "deterministic 解释"
    assert "苏青魂飞魄散" in merged[0].evidence_excerpt
    assert merged[0].suggested_action == "改写"


def test_fix_b_same_scene_two_leaks_stay_separate():
    """Phase 7 Fix B: 两个不同秘密的泄漏不得被吞成一条。"""
    det_a = ContinuityIssue(
        issue_id="d1", category=ContinuityCategory.knowledge_leak,
        severity=Severity.critical, scene_id="s3",
        evidence_excerpt="林凡知道父亲仍然活着",
        author_explanation="secret A leak", source="deterministic", blocking=True,
    )
    det_b = ContinuityIssue(
        issue_id="d2", category=ContinuityCategory.knowledge_leak,
        severity=Severity.critical, scene_id="s3",
        evidence_excerpt="苏青早已确信玄清真人是卧底",
        author_explanation="secret B leak", source="deterministic", blocking=True,
    )
    merged = ContinuityIssueMerger().merge([det_a, det_b], [])
    assert len(merged) == 2, "两个独立问题不得错误合并"


def test_fix_b_llm_overlap_merges_but_distinct_evidence_kept():
    det = ContinuityIssue(
        issue_id="d1", category=ContinuityCategory.knowledge_leak,
        severity=Severity.critical, scene_id="s1",
        evidence_excerpt="林凡知道父亲仍然活着",
        author_explanation="det", source="deterministic", blocking=True,
    )
    overlapping = ContinuityIssue(
        issue_id="l1", category=ContinuityCategory.knowledge_leak,
        severity=Severity.warning, scene_id="s1",
        evidence_excerpt="知道父亲仍然活着的描写需要删去",
        author_explanation="same problem via LLM", source="llm", blocking=False,
    )
    distinct = ContinuityIssue(
        issue_id="l2", category=ContinuityCategory.knowledge_leak,
        severity=Severity.warning, scene_id="s1",
        evidence_excerpt="完全无关的另一个问题证据",
        author_explanation="different problem", source="llm", blocking=False,
    )
    merged = ContinuityIssueMerger().merge([det], [overlapping, distinct])
    assert len(merged) == 2, "overlap 合并为一条，无关问题保留"
    merged_det = next(i for i in merged if i.source == "deterministic" or "deterministic" in str(i))
    assert merged_det.severity == Severity.critical


def test_merger_orders_by_severity():
    warn = ContinuityIssue(issue_id="a", category=ContinuityCategory.timeline,
                           severity=Severity.warning, scene_id="s1",
                           author_explanation="w", source="deterministic")
    crit = ContinuityIssue(issue_id="b", category=ContinuityCategory.hard_constraint,
                           severity=Severity.critical, scene_id="s2",
                           author_explanation="c", source="deterministic")
    merged = ContinuityIssueMerger().merge([warn, crit], [])
    assert merged[0].severity == Severity.critical


# -------------------------------------------------------------- service/report

async def test_check_draft_hybrid_report_counts_and_hash(session):
    from app.services.continuity import ContinuityService

    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    await world["service"].add_fact(novel.id, SECRET_STATEMENT, secrecy="top_secret")
    context, draft, plan = await _context_with_text(
        session, novel, world,
        {"s1": f"正文提到{SECRET_STATEMENT}。"},
    )
    provider = FakeLLMProvider()
    provider.enqueue_structured({"issues": []})
    service = ContinuityService(session, _checker(provider), None, mode="hybrid")

    report = await service.check_draft(novel.id, draft, plan)

    assert report.valid is False
    assert report.deterministic_issue_count >= 1
    assert report.llm_issue_count == 0
    assert report.blocking_issue_count >= 1
    assert report.highest_severity == Severity.critical
    assert report.draft_hash == draft_hash(draft)
    assert report.checked_scene_ids == ["s1"]


async def test_deterministic_only_mode_skips_llm(session):
    from app.services.continuity import ContinuityService

    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    context, draft, plan = await _context_with_text(
        session, novel, world, {"s1": "正常正文。"}
    )
    provider = FakeLLMProvider()  # empty: any LLM call would fail
    service = ContinuityService(session, _checker(provider), None, mode="deterministic_only")

    report = await service.check_draft(novel.id, draft, plan)
    assert provider.calls == []
    assert report.valid is True
