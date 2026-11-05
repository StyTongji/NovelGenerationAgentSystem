import pytest

from app.commit.models import CommitError
from app.services.canonical_commit import CanonicalCommitService
from app.services.chapter_generation import ChapterGenerationService
from tests.test_real_closure_hardening import valid_package, approval
from tests.e2e.test_preview_pipeline import db_fingerprint


async def test_review_context_includes_real_mental_state_and_fact_catalog(session):
    from app.services.author_review import build_review_before_state
    package = await valid_package(session)
    before = await db_fingerprint(session)
    context = await build_review_before_state(session, package)
    assert all("mental_condition" in row["state"] for row in context["characters"])
    assert context["fact_catalog"]
    assert all(row["fact_id"] and row["statement"] for row in context["fact_catalog"])
    assert await db_fingerprint(session) == before


async def test_unknown_realm_field_cannot_silently_pass_validation(session):
    import uuid
    from app.memory.catalog import EvidenceCatalogBuilder
    from app.memory.models import MemoryUpdateOperation
    from app.services.memory_extraction import MemoryExtractionService
    package = await valid_package(session)
    before = await db_fingerprint(session)
    entry = EvidenceCatalogBuilder.build(package.draft).entries[0]
    package.memory_result.batch.operations.append(MemoryUpdateOperation.model_validate({
        "operation_id": "bad-realm", "update_type": "character_state_update",
        "source_scene_id": entry.scene_id, "evidence_refs": [{"evidence_id": entry.evidence_id}],
        "character_state": {"character_id": "a5448bd9-6a04-4b0d-a22b-8156c393cc9a",
            "field": "power_realm", "proposed_new_value": "练气(1)"}}))
    result = await MemoryExtractionService(session, require_evidence_refs=True).validate_batch(
        uuid.UUID(package.novel_id), package.draft, package.plan_result.plan,
        package.memory_result.batch, dry_run=True)
    assert not result.validation_report.valid and result.dry_run is None
    assert any(issue.code == "unsupported_character_state_field" for issue in result.validation_report.issues)
    assert await db_fingerprint(session) == before


async def test_location_action_is_not_a_canonical_location(session):
    import uuid
    from app.memory.catalog import EvidenceCatalogBuilder
    from app.memory.models import MemoryUpdateOperation
    from app.services.memory_extraction import MemoryExtractionService
    package = await valid_package(session)
    entry = EvidenceCatalogBuilder.build(package.draft).entries[0]
    package.memory_result.batch.operations.append(MemoryUpdateOperation.model_validate({
        "operation_id": "bad-location", "update_type": "character_state_update",
        "source_scene_id": entry.scene_id, "evidence_refs": [{"evidence_id": entry.evidence_id}],
        "character_state": {"character_id": "a5448bd9-6a04-4b0d-a22b-8156c393cc9a",
            "field": "location", "proposed_new_value": "前往未登记的新摊位"}}))
    result = await MemoryExtractionService(session, require_evidence_refs=True).validate_batch(
        uuid.UUID(package.novel_id), package.draft, package.plan_result.plan,
        package.memory_result.batch, dry_run=True)
    assert not result.validation_report.valid
    assert any(issue.code == "unknown_location_name" for issue in result.validation_report.issues)


async def test_new_character_keeps_canonical_realm(session):
    import uuid
    from sqlalchemy import select
    from app.models import Character, Realm, PowerSystem, CharacterState
    from app.memory.catalog import EvidenceCatalogBuilder
    from app.memory.models import MemoryUpdateOperation
    from app.draft.models import TemporaryEntityInfo
    from app.services.memory_extraction import MemoryExtractionService
    from tests.test_real_closure_hardening import rehash
    package = await valid_package(session)
    realm = await session.scalar(select(Realm).join(PowerSystem).where(PowerSystem.novel_id == uuid.UUID(package.novel_id)))
    entry = EvidenceCatalogBuilder.build(package.draft).entries[0]
    package.draft.temporary_entities.append(TemporaryEntityInfo(temporary_ref="new_character_realm",
        entity_type="character", name_hint="测试筑基修士", introduced_in_scene=entry.scene_id))
    package.memory_result.batch.operations.append(MemoryUpdateOperation.model_validate({
        "operation_id": "new-npc", "update_type": "propose_entity", "source_scene_id": entry.scene_id,
        "evidence_refs": [{"evidence_id": entry.evidence_id}], "proposed_entity": {
            "temporary_ref": "new_character_realm", "entity_type": "character", "proposed_name": "测试筑基修士",
            "attributes": {"realm": realm.name, "role": "测试人物"}}}))
    package.memory_result = await MemoryExtractionService(session, require_evidence_refs=True).validate_batch(
        uuid.UUID(package.novel_id), package.draft, package.plan_result.plan,
        package.memory_result.batch, dry_run=True)
    assert package.memory_result.validation_report.valid, package.memory_result.validation_report
    rehash(package)
    service = ChapterGenerationService(session, None, None, None, None, CanonicalCommitService(session))
    result = await service.finalize(package, attempt_id="test-only", approval=approval(package))
    identifier = uuid.UUID(result.temporary_entity_mapping["new_character_realm"])
    state = await session.scalar(select(CharacterState).where(CharacterState.character_id == identifier))
    assert state.realm_id == realm.id


async def test_automated_receipt_requires_explicit_test_opt_in(session):
    package = await valid_package(session)
    receipt = approval(package).model_copy(update={"review_kind": "automated_test"})
    before = await db_fingerprint(session)
    normal = ChapterGenerationService(session, None, None, None, None, CanonicalCommitService(session))
    with pytest.raises(CommitError, match="Automated test approval"):
        await normal.finalize(package, attempt_id="test-only", approval=receipt)
    assert await db_fingerprint(session) == before
    isolated = ChapterGenerationService(session, None, None, None, None,
        CanonicalCommitService(session, allow_automated_test_review=True))
    result = await isolated.finalize(package, attempt_id="test-only", approval=receipt)
    after = await db_fingerprint(session)
    duplicate = await isolated.finalize(package, attempt_id="test-only", approval=receipt)
    assert not result.duplicate and duplicate.duplicate
    assert await db_fingerprint(session) == after


async def test_event_resolves_new_location_reference_at_commit(session):
    import uuid
    from sqlalchemy import select
    from app.draft.models import TemporaryEntityInfo
    from app.memory.catalog import EvidenceCatalogBuilder
    from app.memory.models import MemoryUpdateOperation
    from app.models import Event
    from app.services.memory_extraction import MemoryExtractionService
    from tests.test_real_closure_hardening import rehash
    package = await valid_package(session)
    entry = EvidenceCatalogBuilder.build(package.draft).entries[0]
    package.draft.temporary_entities.append(TemporaryEntityInfo(
        temporary_ref="new_location_test", entity_type="location", name_hint="临时城门",
        introduced_in_scene=entry.scene_id, role="测试场景地点"))
    common = {"source_scene_id": entry.scene_id, "evidence_refs": [{"evidence_id": entry.evidence_id}]}
    package.memory_result.batch.operations.extend([
        MemoryUpdateOperation.model_validate({**common, "operation_id": "place", "update_type": "propose_entity",
            "proposed_entity": {"temporary_ref": "new_location_test", "entity_type": "location", "proposed_name": "临时城门"}}),
        MemoryUpdateOperation.model_validate({**common, "operation_id": "arrival", "update_type": "create_event",
            "event": {"event_type": "arrival", "title": "抵达城门", "author_summary": "抵达城门",
                "reader_safe_summary": "抵达城门", "location_id": "new_location_test"}}),
    ])
    package.memory_result = await MemoryExtractionService(session, require_evidence_refs=True).validate_batch(
        uuid.UUID(package.novel_id), package.draft, package.plan_result.plan,
        package.memory_result.batch, dry_run=True)
    assert package.memory_result.validation_report.valid
    rehash(package)
    service = ChapterGenerationService(session, None, None, None, None, CanonicalCommitService(session))
    result = await service.finalize(package, attempt_id="test-only", approval=approval(package))
    event = (await session.execute(select(Event))).scalar_one()
    assert str(event.location_id) == result.temporary_entity_mapping["new_location_test"]
