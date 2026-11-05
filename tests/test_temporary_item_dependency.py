import uuid

import pytest
from sqlalchemy import select

from app.memory.catalog import EvidenceCatalogBuilder
from app.memory.models import MemoryUpdateOperation
from app.models import Item, ChapterCommit
from app.services.memory_extraction import MemoryExtractionService
from tests.test_real_closure_hardening import valid_package, generation, approval, rehash


async def item_package(session, mutation=None):
    package = await valid_package(session)
    catalog = EvidenceCatalogBuilder.build(package.draft)
    entry = next(e for e in catalog.entries if e.scene_id == "ch1_s2")
    common = {"source_scene_id": entry.scene_id, "evidence_refs": [{"evidence_id": entry.evidence_id}]}
    proposal = MemoryUpdateOperation.model_validate({**common, "operation_id": "new-item", "update_type": "propose_entity",
        "proposed_entity": {"temporary_ref": "new_item_1", "entity_type": "item", "proposed_name": "测试碎片"}})
    transfer = MemoryUpdateOperation.model_validate({**common, "operation_id": "take-item", "update_type": "item_update",
        "item": {"temporary_item_ref": "new_item_1", "change": "owner_transfer",
                 "new_owner_id": "a5448bd9-6a04-4b0d-a22b-8156c393cc9a"}})
    # Deliberately put dependent update first: dependency is batch-level.
    batch = package.memory_result.batch.model_copy(deep=True)
    batch.operations = [transfer, proposal]
    if mutation == "missing":
        batch.operations = [transfer]
    elif mutation == "unknown":
        transfer.item.temporary_item_ref = "new_item_999"
    elif mutation == "type":
        proposal.proposed_entity.entity_type = "character"
    elif mutation == "duplicate":
        extra = proposal.model_copy(deep=True)
        extra.operation_id = "duplicate"
        batch.operations.append(extra)
    elif mutation == "alias":
        proposal.proposed_entity.proposed_name = "轮回古镜"
    elif mutation == "old_owner":
        transfer.item.expected_old_owner_id = transfer.item.new_owner_id
    elif mutation == "double":
        transfer.item.item_id = "new_item_1"
    elif mutation == "implicit":
        transfer.item.item_id = "new_item_1"
        transfer.item.temporary_item_ref = None
    package.memory_result = await MemoryExtractionService(session, require_evidence_refs=True).validate_batch(
        uuid.UUID(package.novel_id), package.draft, package.plan_result.plan, batch, dry_run=True)
    rehash(package)
    return package


async def test_explicit_item_dependency_dryrun_commit_and_provenance(session):
    package = await item_package(session)
    result = package.memory_result
    assert result.validation_report.valid, result.validation_report
    assert result.dry_run.applicable
    preview_item = next(i for i in result.dry_run.proposed_after_state["items"] if i["id"] == "new_item_1")
    committed = await generation(session).finalize(package, attempt_id="test-only", approval=approval(package))
    item = await session.get(Item, uuid.UUID(committed.temporary_entity_mapping["new_item_1"]))
    assert str(item.current_owner_character_id) == preview_item["owner"]
    record = (await session.execute(select(ChapterCommit))).scalar_one()
    op = next(op for op in record.operations_json if op["operation_id"] == "take-item")
    assert op["payload"]["temporary_item_ref"] == "new_item_1"
    assert op["resolved_item_id"] == str(item.id)


@pytest.mark.parametrize("mutation", ["missing", "unknown", "type", "duplicate", "alias", "old_owner", "double", "implicit"])
async def test_temporary_item_dependencies_fail_closed(session, mutation):
    package = await item_package(session, mutation)
    assert not package.memory_result.validation_report.valid
    assert package.memory_result.dry_run is None
