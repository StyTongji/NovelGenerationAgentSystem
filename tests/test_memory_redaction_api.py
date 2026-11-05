"""Phase 6 Fix A regression: repair redaction (secret never re-enters a
repair prompt), plus Phase 7 memory API tests."""

from __future__ import annotations

import hashlib

import pytest
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_memory_extraction_service
from app.continuity.repair_redaction import (
    REDACTED_MARKER,
    redact_scene_for_repair,
)
from app.continuity.models import ContinuityCategory, ContinuityIssue
from app.db import get_session
from app.models.enums import Severity
from app.services.continuity import ContinuityService
from tests.helpers import create_novel, create_rich_world


# ------------------------------------------------------------- unit redaction

def test_redaction_widens_evidence_to_sentence():
    issue = ContinuityIssue(
        issue_id="i1", category=ContinuityCategory.knowledge_leak,
        severity=Severity.critical, scene_id="s1",
        evidence_excerpt="父亲仍然活着",
        author_explanation="POV 不应知道", source="deterministic", blocking=True,
    )
    original = "他终于明白，父亲仍然活着，并已经进入轮回秘境。第二天他继续赶路。"
    redacted = redact_scene_for_repair("s1", original, [issue], protected_statements=[])

    assert REDACTED_MARKER in redacted.redacted_text
    assert "父亲仍然活着" not in redacted.redacted_text
    assert "轮回秘境" not in redacted.redacted_text
    assert "第二天他继续赶路。" in redacted.redacted_text
    assert redacted.redacted_any


def test_unclassified_issue_evidence_is_redacted_before_repair():
    issue = ContinuityIssue(
        issue_id="other-secret", category=ContinuityCategory.other,
        severity=Severity.error, scene_id="s1", evidence_excerpt="刻痕是他留下的",
        author_explanation="角色不该知道刻痕来源", source="llm", blocking=True,
    )
    result = redact_scene_for_repair("s1", "他知道刻痕是他留下的。随后他保持安静。", [issue])
    assert "刻痕是他留下的" not in result.redacted_text
    assert REDACTED_MARKER in result.redacted_text and "随后他保持安静。" in result.redacted_text


def test_redaction_fallback_protected_statements():
    issue = ContinuityIssue(
        issue_id="i1", category=ContinuityCategory.knowledge_leak,
        severity=Severity.critical, scene_id="s1",
        evidence_excerpt=None,  # unlocatable
        author_explanation="leak", source="deterministic", blocking=True,
    )
    original = "林凡知道父亲仍然活着。他继续前行。玄清真人提及轮回秘境的传闻。"
    redacted = redact_scene_for_repair(
        "s1", original, [issue],
        protected_statements=["父亲仍然活着", "轮回秘境"],
    )
    assert "父亲仍然活着" not in redacted.redacted_text
    assert "轮回秘境" not in redacted.redacted_text
    assert redacted.redacted_text.count(REDACTED_MARKER) == 2


def test_redaction_conservative_whole_scene():
    issue = ContinuityIssue(
        issue_id="i1", category=ContinuityCategory.reveal_policy,
        severity=Severity.critical, scene_id="s1",
        evidence_excerpt="完全无法定位的文本",
        author_explanation="leak", source="deterministic", blocking=True,
    )
    original = "没有任何可匹配保护语句的正文。"
    redacted = redact_scene_for_repair("s1", original, [issue], protected_statements=[])
    assert redacted.redacted_text == REDACTED_MARKER


def test_non_leak_issues_not_redacted():
    issue = ContinuityIssue(
        issue_id="i1", category=ContinuityCategory.timeline,
        severity=Severity.error, scene_id="s1",
        evidence_excerpt="正午时分", author_explanation="时间倒流",
        source="deterministic", blocking=True,
    )
    original = "正午时分，众人出发。"
    redacted = redact_scene_for_repair("s1", original, [issue])
    assert redacted.redacted_text == original
    assert not redacted.redacted_any


# ------------------------------------------------- service-level capture test

async def test_repair_prompt_redacts_leak_evidence(session):
    """§3 critical: 最终 repair request 中秘密被替换为 marker。"""
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    plan = __import__("app.schemas.agent_outputs", fromlist=["ChapterPlan"]).ChapterPlan.model_validate(
        __import__("tests.draft_helpers", fromlist=["build_valid_plan_dict"]).build_valid_plan_dict(world)
    )
    from datetime import UTC, datetime

    from app.draft.models import ChapterDraft, SceneDraft
    from app.schemas.agent_outputs import EntityRef, ScenePlan

    secret_fact = await world["service"].add_fact(
        novel.id, "林凡的父亲仍然活着", secrecy="secret"
    )
    scene_plans = [
        ScenePlan(
            scene_id="s1", scene_index=1, source_beat_ids=["1"],
            pov=EntityRef(name="林凡"), location=EntityRef(name="天剑城"),
            scene_goal="g", target_chars=800,
        )
    ]
    leak_text = "林凡已经确定父亲仍然活着，并进入了轮回秘境。他握紧了拳头。"
    draft = ChapterDraft(
        novel_id=str(novel.id),
        chapter_number=5,
        scenes=[SceneDraft(scene_id="s1", scene_index=1, text=leak_text)],
        full_text=leak_text,
        chapter_plan_hash="0" * 64,
        story_context_hash="0" * 64,
        scene_plans=scene_plans,
        created_at=datetime.now(UTC),
    )
    provider = __import__("app.llm.fake", fromlist=["FakeLLMProvider"]).FakeLLMProvider()
    provider.enqueue_text("林凡握紧了拳头，心中浮起一丝疑虑。")
    repair_agent = __import__("app.agents.writer_repair", fromlist=["WriterRepairAgent"]).WriterRepairAgent(
        llm=__import__("app.llm.client", fromlist=["AgentLLMClient"]).AgentLLMClient(
            provider, agent_name="writer_repair", model="fake-model"
        ),
        prompt_registry=__import__("app.llm.prompts", fromlist=["get_default_registry"]).get_default_registry(),
    )
    service = ContinuityService(
        session, llm_checker=None, repair_agent=repair_agent,
        max_repairs=2, mode="deterministic_only",
    )

    result = await service.repair_preview(novel.id, draft, plan)

    for call in provider.calls:
        prompt = "\n".join(m.content for m in call)
        assert "父亲仍然活着" not in prompt, "repair prompt 泄漏 secret"
        assert "轮回秘境" not in prompt, "repair prompt 泄漏 secret"
        assert REDACTED_MARKER in prompt
    assert result.report.valid is True


# ----------------------------------------------------------------- memory API

@pytest.fixture
def memory_app(client):
    from app.services.memory_extraction import MemoryExtractionService

    def override(session: AsyncSession = Depends(get_session)):
        # no extractor: validation/dry-run endpoints work without LLM
        return MemoryExtractionService(session, extractor=None)

    app = client._transport.app
    app.dependency_overrides[get_memory_extraction_service] = override
    try:
        yield client
    finally:
        app.dependency_overrides.pop(get_memory_extraction_service, None)


def _build_payload(world):
    from datetime import UTC, datetime

    from app.draft.models import ChapterDraft, SceneDraft
    from app.schemas.agent_outputs import ChapterPlan
    from tests.draft_helpers import build_valid_plan_dict

    plan = ChapterPlan.model_validate(build_valid_plan_dict(world))
    draft = ChapterDraft(
        novel_id=str(world["result"].novel_id),
        chapter_number=5,
        scenes=[SceneDraft(scene_id="s1", scene_index=1, text="正常正文。")],
        full_text="正常正文。",
        chapter_plan_hash="0" * 64,
        story_context_hash="0" * 64,
        created_at=datetime.now(UTC),
    )
    return plan, draft


async def test_memory_validate_endpoint(memory_app, session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    plan, draft = _build_payload(world)
    from app.memory.models import (
        CharacterStateChange,
        EvidenceSpan,
        MemoryUpdateBatch,
        MemoryUpdateOperation,
        MemoryUpdateType,
    )

    batch = MemoryUpdateBatch(
        operations=[
            MemoryUpdateOperation(
                operation_id="op1",
                update_type=MemoryUpdateType.character_state_update,
                source_scene_id="s1",
                evidence=[EvidenceSpan(scene_id="s1", exact_quote="正常正文。")],
                character_state=CharacterStateChange(
                    character_id=str(world["characters"]["林凡"].id),
                    field="mental_condition", proposed_new_value="平静",
                ),
            )
        ]
    )
    response = await memory_app.post(
        f"/api/v1/novels/{novel.id}/memory-update/validate",
        json={
            "draft": draft.model_dump(mode="json"),
            "chapter_plan": plan.model_dump(mode="json"),
            "batch": batch.model_dump(mode="json"),
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["validation_report"]["valid"] is True
    assert body["dry_run"] is None


async def test_memory_dry_run_endpoint(memory_app, session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    plan, draft = _build_payload(world)
    from app.memory.models import (
        CharacterStateChange,
        EvidenceSpan,
        MemoryUpdateBatch,
        MemoryUpdateOperation,
        MemoryUpdateType,
    )

    batch = MemoryUpdateBatch(
        operations=[
            MemoryUpdateOperation(
                operation_id="op1",
                update_type=MemoryUpdateType.character_state_update,
                source_scene_id="s1",
                evidence=[EvidenceSpan(scene_id="s1", exact_quote="正常正文。")],
                character_state=CharacterStateChange(
                    character_id=str(world["characters"]["林凡"].id),
                    field="mental_condition", proposed_new_value="警觉",
                ),
            )
        ]
    )
    response = await memory_app.post(
        f"/api/v1/novels/{novel.id}/memory-update/validate",
        json={
            "draft": draft.model_dump(mode="json"),
            "chapter_plan": plan.model_dump(mode="json"),
            "batch": batch.model_dump(mode="json"),
            "dry_run": True,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["dry_run"] is not None
    assert body["dry_run"]["before_hash"] != body["dry_run"]["proposed_after_hash"]


async def test_memory_preview_rejects_blocking_report(memory_app, session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    plan, draft = _build_payload(world)
    report = {
        "valid": False,
        "issues": [],
        "blocking_issue_count": 1,
        "deterministic_issue_count": 1,
        "llm_issue_count": 0,
        "checked_scene_ids": ["s1"],
        "draft_hash": hashlib.sha256(draft.full_text.encode()).hexdigest(),
    }
    response = await memory_app.post(
        f"/api/v1/novels/{novel.id}/chapter-draft/memory-preview",
        json={
            "draft": draft.model_dump(mode="json"),
            "chapter_plan": plan.model_dump(mode="json"),
            "continuity_report": report,
        },
    )
    assert response.status_code == 422

