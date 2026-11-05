"""Phase 8.7 regression and catalog contract tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.memory.catalog import EvidenceCatalogBuilder
from app.memory.models import (
    EvidenceRef,
    FactRef,
    KnowledgeChange,
    MemoryUpdateBatch,
    MemoryUpdateOperation,
    MemoryUpdateType,
    StoryFactCandidate,
)


FIXTURE = Path(__file__).parent / "fixtures" / "real_memory_closure" / "chapter_01"


def _fixture(name: str):
    return json.loads((FIXTURE / name).read_text(encoding="utf-8"))


async def test_real_failure_fixture_reproduces_eight_plus_three(session):
    import uuid
    from sqlalchemy import text
    from app.context.builder import ContextBuilder
    from app.draft.models import ChapterDraft
    from app.memory.validator import MemoryUpdateValidator
    from app.schemas.agent_outputs import ChapterPlan

    # Restore the real before-state into an isolated test database; NEVER
    # read the unversioned trial database or count a stored report as a test.
    for line in (FIXTURE / "before_state.sql").read_text().splitlines():
        if line.strip():
            await session.execute(text(line))
    draft = ChapterDraft.model_validate(_fixture("draft.json"))
    batch = MemoryUpdateBatch.model_validate(_fixture("memory_batch.json"))
    context = await ContextBuilder(session).build_extraction_context(
        uuid.UUID(draft.novel_id), chapter_number=1, chapter_text=draft.full_text,
        chapter_plan=ChapterPlan.model_validate(_fixture("chapter_plan.json")),
        scene_plans=draft.scene_plans, draft=draft,
        draft_hash=EvidenceCatalogBuilder.build(draft).draft_hash,
    )
    result = await MemoryUpdateValidator(session).validate(batch, context, draft)
    codes = [issue.code for issue in result.issues]
    assert codes.count("evidence_not_in_draft") == 8
    assert codes.count("knowledge_unknown_fact") == 3
    assert result.valid is False


def test_evidence_catalog_is_draft_bound_and_never_fuzzy():
    from app.draft.models import ChapterDraft

    draft = ChapterDraft.model_validate(_fixture("draft.json"))
    catalog = EvidenceCatalogBuilder.build(draft)
    assert catalog.draft_hash
    assert len(catalog.entries) >= 3
    assert all(entry.exact_text == draft.scenes[0].text[entry.start_char:entry.end_char]
               for entry in catalog.entries if entry.scene_id == draft.scenes[0].scene_id)
    assert "not-a-real-evidence-id" not in catalog.by_id()


@pytest.mark.asyncio
async def test_fact_ref_batch_dependency_is_validated(session):
    from tests.test_memory_validator import _setup, _validator

    _novel, _world, draft, context = await _setup(session, {"s1": "林凡发现一枚新符印。"})
    context.evidence_catalog = EvidenceCatalogBuilder.build(draft)
    batch = MemoryUpdateBatch(operations=[
        MemoryUpdateOperation(
            operation_id="fact",
            update_type=MemoryUpdateType.story_fact_create,
            source_scene_id="s1",
            evidence_refs=[EvidenceRef(evidence_id="ev_0001")],
            story_fact=StoryFactCandidate(
                statement="林凡发现一枚新符印", temporary_fact_ref="new_fact_1",
            ),
        ),
        MemoryUpdateOperation(
            operation_id="knowledge",
            update_type=MemoryUpdateType.character_knowledge_update,
            source_scene_id="s1",
            evidence_refs=[EvidenceRef(evidence_id="ev_0001")],
            knowledge=KnowledgeChange(
                character_id=str(_world["characters"]["林凡"].id),
                fact_ref=FactRef(temporary_fact_ref="new_fact_1"),
                status="known",
            ),
        ),
    ])
    report = await _validator(session).validate(batch, context, draft)
    assert report.valid, [issue.code for issue in report.errors()]
    assert batch.operations[1].knowledge.fact_ref.temporary_fact_ref == "new_fact_1"
    assert batch.operations[1].knowledge.temporary_fact_ref is None  # no caller mutation
    assert context.evidence_catalog.by_id()["ev_0001"].exact_text == "林凡发现一枚新符印。"


@pytest.mark.asyncio
async def test_unknown_temporary_fact_ref_remains_critical(session):
    from tests.test_memory_validator import _setup, _validator

    _novel, world, draft, context = await _setup(session, {"s1": "林凡发现一枚新符印。"})
    context.evidence_catalog = EvidenceCatalogBuilder.build(draft)
    batch = MemoryUpdateBatch(operations=[
        MemoryUpdateOperation(
            operation_id="knowledge",
            update_type=MemoryUpdateType.character_knowledge_update,
            source_scene_id="s1",
            evidence_refs=[EvidenceRef(evidence_id="ev_0001")],
            knowledge=KnowledgeChange(
                character_id=str(world["characters"]["林凡"].id),
                fact_ref=FactRef(temporary_fact_ref="missing_fact"),
                status="known",
            ),
        ),
    ])
    report = await _validator(session).validate(batch, context, draft)
    assert not report.valid
    assert any(issue.code == "knowledge_unknown_fact" for issue in report.issues)


@pytest.mark.asyncio
async def test_v3_strict_mode_rejects_legacy_free_text_evidence(session):
    from app.memory.models import EvidenceSpan
    from tests.test_memory_validator import _setup, _validator

    _novel, world, draft, context = await _setup(session, {"s1": "林凡点头。"})
    batch = MemoryUpdateBatch(operations=[
        MemoryUpdateOperation(
            operation_id="legacy",
            update_type=MemoryUpdateType.character_knowledge_update,
            source_scene_id="s1",
            evidence=[EvidenceSpan(scene_id="s1", exact_quote="林凡点头。")],
            knowledge=KnowledgeChange(
                character_id=str(world["characters"]["林凡"].id),
                fact_id=str(context.existing_facts[0].fact_id),
                status="known",
            ),
        ),
    ])
    report = await _validator(session).validate(
        batch, context, draft, require_evidence_refs=True,
    )
    assert not report.valid
    assert any(issue.code == "missing_evidence_ref" for issue in report.issues)


@pytest.mark.asyncio
async def test_v3_rejects_catalog_hash_and_canonical_fact_tampering(session):
    from tests.test_memory_validator import _setup, _validator

    _novel, world, draft, context = await _setup(session, {"s1": "林凡点头。"})
    catalog = EvidenceCatalogBuilder.build(draft).model_copy(update={"draft_hash": "f" * 64})
    context.evidence_catalog = catalog
    batch = MemoryUpdateBatch(operations=[
        MemoryUpdateOperation(
            operation_id="tampered",
            update_type=MemoryUpdateType.character_knowledge_update,
            source_scene_id="s1",
            evidence_refs=[EvidenceRef(evidence_id="ev_0001")],
            knowledge=KnowledgeChange(
                character_id=str(world["characters"]["林凡"].id),
                fact_ref=FactRef(fact_id="00000000-0000-0000-0000-000000000000"),
                status="known",
            ),
        ),
    ])
    report = await _validator(session).validate(batch, context, draft)
    codes = {issue.code for issue in report.issues}
    assert {"evidence_catalog_draft_hash_mismatch", "unknown_entity_reference"} <= codes
