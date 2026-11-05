import pytest

from app.memory.catalog import EvidenceCatalogBuilder
from app.services.author_review import declare_draft_items
from tests.test_studio import studio, new_story, completed_task, counts


async def test_explicit_item_declaration_is_new_candidate_and_keeps_canonical_unchanged(studio):
    manager, client = studio
    novel = await new_story(client)
    run = (await client.post("/api/v1/studio/runs", json={"novel_id": novel, "chapter_count": 1})).json()
    rid = run["id"]
    await completed_task(manager, rid)
    old = manager.chapter_detail(rid, 1)
    package = manager._package(manager.get(rid), manager._chapter(manager.get(rid)))
    entry = next(e for e in EvidenceCatalogBuilder.build(package.draft).entries if "旧记录" in e.exact_text)
    payload = {"temporary_ref": "new_item_review", "name": "旧记录", "source_scene_id": entry.scene_id,
               "evidence_ids": [entry.evidence_id]}
    batch = old["package"]["memory_result"]["batch"]
    batch["operations"].extend([
        {"operation_id": "review-new-item", "update_type": "propose_entity", "source_scene_id": entry.scene_id,
         "evidence_refs": [{"evidence_id": entry.evidence_id}], "proposed_entity": {
             "temporary_ref": "new_item_review", "entity_type": "item", "proposed_name": "旧记录", "attributes": {}}},
    ])
    response = await client.post(f"/api/v1/studio/runs/{rid}/chapters/1/memory",
                                json={"batch": batch, "item_declarations": [payload]})
    assert response.status_code == 200, response.text
    await completed_task(manager, rid)
    current = manager.chapter_detail(rid, 1)
    assert current["package"]["draft"]["full_text"] == package.draft.full_text
    assert current["package"]["memory_result"]["validation_report"]["valid"]
    assert current["package"]["memory_result"]["dry_run"]["applicable"]
    before_characters = current["package"]["memory_result"]["dry_run"]["before_state"]["characters"]
    hero = next(character for character in before_characters if character["is_protagonist"])
    assert hero["state"]["mental_condition"] == "谨慎"
    assert current["package"]["preview_package_hash"] != old["package"]["preview_package_hash"]
    assert package.draft.temporary_entities == []
    assert await counts(manager) == [0] * 4
    for invalid in [
        {**payload, "evidence_ids": ["nonexistent"]},
        {**payload, "name": "原文不存在的古镜"},
        {**payload, "source_scene_id": "other_scene"},
    ]:
        with pytest.raises(ValueError):
            declare_draft_items(package.draft, [invalid])
    declared = declare_draft_items(package.draft, [payload])
    with pytest.raises(ValueError, match="不允许覆盖"):
        declare_draft_items(declared, [payload])
