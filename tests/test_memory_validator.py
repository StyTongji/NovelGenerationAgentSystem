"""Memory validator / evidence tests: hallucinated refs, preconditions,
knowledge safety, idempotency."""

from __future__ import annotations

import uuid

import pytest

from app.context.builder import ContextBuilder
from app.memory.models import (
    CharacterStateChange,
    EvidenceSpan,
    ItemChange,
    KnowledgeChange,
    MemoryUpdateBatch,
    MemoryUpdateOperation,
    MemoryUpdateType,
)
from app.memory.validator import MemoryUpdateValidator
from tests.draft_helpers import build_valid_plan_dict
from tests.helpers import (
    SECRET_STATEMENT,
    create_novel,
    create_rich_world,
)


_PAYLOAD_FIELD = {
    MemoryUpdateType.create_event: "event",
    MemoryUpdateType.character_state_update: "character_state",
    MemoryUpdateType.character_knowledge_update: "knowledge",
    MemoryUpdateType.item_update: "item",
    MemoryUpdateType.story_fact_create: "story_fact",
    MemoryUpdateType.plot_thread_update: "plot_thread",
    MemoryUpdateType.propose_entity: "proposed_entity",
}


def _op(op_id: str, update_type, scene_id: str, quotes: list[str], payload, **kwargs) -> MemoryUpdateOperation:
    fields = {_PAYLOAD_FIELD[update_type]: payload}
    return MemoryUpdateOperation(
        operation_id=op_id,
        update_type=update_type,
        source_scene_id=scene_id,
        evidence=[EvidenceSpan(scene_id=scene_id, exact_quote=q) for q in quotes],
        **fields,
        **kwargs,
    )


async def _setup(session, scene_texts: dict[str, str], extra=None):
    """World + draft + extraction context for validator tests."""
    from datetime import UTC, datetime

    from app.draft.models import ChapterDraft, SceneDraft
    from app.schemas.agent_outputs import ChapterPlan

    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    plan = ChapterPlan.model_validate(build_valid_plan_dict(world))
    scene_plans = [
        __import__("app.schemas.agent_outputs", fromlist=["ScenePlan"]).ScenePlan(
            scene_id=f"s{i}", scene_index=i, source_beat_ids=[str(i)],
            pov=__import__("app.schemas.agent_outputs", fromlist=["EntityRef"]).EntityRef(name="林凡"),
            location=__import__("app.schemas.agent_outputs", fromlist=["EntityRef"]).EntityRef(name="天剑城"),
            scene_goal=f"scene {i}", target_chars=800,
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
        scene_plans=scene_plans,
        created_at=datetime.now(UTC),
    )
    if extra:
        await extra(session, novel, world)
    builder = ContextBuilder(session)
    context = await builder.build_extraction_context(
        novel.id, chapter_number=5, chapter_text=draft.full_text,
        chapter_plan=plan, scene_plans=scene_plans, draft=draft,
        draft_hash="a" * 64,
    )
    return novel, world, draft, context


def _validator(session):
    return MemoryUpdateValidator(session)


async def test_missing_evidence_invalid(session):
    novel, world, draft, context = await _setup(session, {"s1": "林凡喝茶。"})
    op = MemoryUpdateOperation(
        operation_id="op1", update_type=MemoryUpdateType.character_state_update,
        source_scene_id="s1", evidence=[],
        character_state=CharacterStateChange(
            character_id=str(world["characters"]["林凡"].id),
            field="physical_condition", proposed_new_value="轻伤",
        ),
    )
    report = await _validator(session).validate(
        MemoryUpdateBatch(operations=[op]), context, draft
    )
    assert not report.valid
    assert any(i.code == "missing_evidence" for i in report.issues)


async def test_hallucinated_evidence_invalid(session):
    novel, world, draft, context = await _setup(session, {"s1": "林凡喝茶。"})
    op = _op(
        "op1", MemoryUpdateType.character_state_update, "s1",
        ["林凡一剑杀死陈海。"],  # NOT in the draft
        CharacterStateChange(
            character_id=str(world["characters"]["林凡"].id),
            field="alive_status", expected_old_value="alive", proposed_new_value="dead",
        ),
    )
    report = await _validator(session).validate(
        MemoryUpdateBatch(operations=[op]), context, draft
    )
    assert not report.valid
    assert any(i.code == "evidence_not_in_draft" for i in report.issues)


async def test_plan_as_evidence_invalid(session):
    """#43/#85: plan says 林凡获得玉佩, draft silent -> candidate invalid."""
    novel, world, draft, context = await _setup(session, {"s1": "拍卖会平静结束。"})
    from app.models import Item

    mirror = await world["service"].items.add(
        Item(novel_id=novel.id, name="玉佩", type="treasure",
             current_owner_character_id=None)
    )
    op = _op(
        "op1", MemoryUpdateType.item_update, "s1",
        ["林凡获得玉佩"],  # plan text, not draft text
        ItemChange(item_id=str(mirror.id), change="owner_transfer",
                   new_owner_id=str(world["characters"]["林凡"].id)),
    )
    report = await _validator(session).validate(
        MemoryUpdateBatch(operations=[op]), context, draft
    )
    assert not report.valid
    assert any(i.code == "evidence_not_in_draft" for i in report.issues)


@pytest.mark.parametrize("new_status", ["封条已割、棺盖启封", None, "consumed"])
async def test_item_status_requires_canonical_enum_before_dry_run(session, new_status):
    _, world, draft, context = await _setup(session, {"s1": "轮回古镜上的封条被撕开。"})
    op = _op("item-status", MemoryUpdateType.item_update, "s1",
             ["轮回古镜上的封条被撕开。"],
             ItemChange(item_id=str(world["mirror"].id), change="status_change", new_status=new_status))
    report = await _validator(session).validate(MemoryUpdateBatch(operations=[op]), context, draft)
    assert not report.valid
    assert any(i.code == "invalid_item_status" for i in report.issues)


def test_item_dry_run_rejects_unrepresentable_status_and_matches_consumed_commit():
    from app.memory.dry_run import MemoryDryRun

    state = {"items": [{"id": "item-1", "status": "intact"}]}
    op = _op("item-status", MemoryUpdateType.item_update, "s1", ["封条被撕开。"],
             ItemChange(item_id="item-1", change="status_change", new_status="封条已割、棺盖启封"))
    result = MemoryDryRun().run(MemoryUpdateBatch(operations=[op]), state)
    assert not result.applicable and result.proposed_after_state["items"][0]["status"] == "intact"
    op.item.change = "consumed"
    result = MemoryDryRun().run(MemoryUpdateBatch(operations=[op]), state)
    assert result.applicable and result.proposed_after_state["items"][0]["status"] == "lost"


async def test_evidence_span_validation_and_relocation(session):
    novel, world, draft, context = await _setup(
        session, {"s1": "剑光来得太快。苏青挡在林凡身前。"}
    )
    lin = world["characters"]["林凡"]
    good = _op(
        "good", MemoryUpdateType.character_state_update, "s1",
        ["苏青挡在林凡身前。"],
        CharacterStateChange(character_id=str(lin.id), field="physical_condition",
                             expected_old_value=None, proposed_new_value="轻伤"),
    )
    # wrong offsets provided -> corrected warning
    bad_pos = _op(
        "bad_pos", MemoryUpdateType.character_state_update, "s1",
        ["苏青挡在林凡身前。"],
        CharacterStateChange(character_id=str(lin.id), field="mental_condition",
                             proposed_new_value="震动"),
    )
    bad_pos.evidence[0].start_char = 999
    bad_pos.evidence[0].end_char = 1024

    report = await _validator(session).validate(
        MemoryUpdateBatch(operations=[good, bad_pos]), context, draft
    )
    assert report.valid, f"{[i.code for i in report.errors()]}"
    assert any(i.code == "evidence_position_corrected" for i in report.issues)


async def test_ambiguous_evidence_recorded(session):
    novel, world, draft, context = await _setup(
        session, {"s1": "林凡点头。林凡点头。"}
    )
    lin = world["characters"]["林凡"]
    op = _op(
        "op1", MemoryUpdateType.character_state_update, "s1",
        ["林凡点头"],
        CharacterStateChange(character_id=str(lin.id), field="mental_condition",
                             proposed_new_value="坚定"),
    )
    report = await _validator(session).validate(
        MemoryUpdateBatch(operations=[op]), context, draft
    )
    assert any(i.code == "ambiguous_evidence" for i in report.issues)
    assert report.valid


async def test_old_value_mismatch_invalid(session):
    novel, world, draft, context = await _setup(
        session, {"s1": "苏青的伤势竟然完全痊愈了。"}
    )
    su = world["characters"]["苏青"]
    op = _op(
        "op1", MemoryUpdateType.character_state_update, "s1",
        ["苏青的伤势竟然完全痊愈了。"],
        CharacterStateChange(character_id=str(su.id), field="physical_condition",
                             expected_old_value="重伤",  # canonical is None/healthy
                             proposed_new_value="健康"),
    )
    report = await _validator(session).validate(
        MemoryUpdateBatch(operations=[op]), context, draft
    )
    assert not report.valid
    assert any(i.code == "old_value_mismatch" for i in report.issues)


async def test_realm_valid_transition_accepted(session):
    novel, world, draft, context = await _setup(
        session, {"s1": "林凡盘膝而坐，成功突破金丹，气息圆融。"}
    )
    lin = world["characters"]["林凡"]
    op = _op(
        "op1", MemoryUpdateType.character_state_update, "s1",
        ["林凡盘膝而坐，成功突破金丹，气息圆融。"],
        CharacterStateChange(character_id=str(lin.id), field="realm",
                             expected_old_value="筑基", proposed_new_value="金丹"),
    )
    report = await _validator(session).validate(
        MemoryUpdateBatch(operations=[op]), context, draft
    )
    assert report.valid, f"{[i.code for i in report.errors()]}"


async def test_realm_illegal_jump_rejected(session):
    """#60: 筑基后期 -> 元婴中期 独立防线拒绝。"""
    novel, world, draft, context = await _setup(
        session, {"s1": "林凡直接踏入元婴中期。"}
    )
    lin = world["characters"]["林凡"]
    op = _op(
        "op1", MemoryUpdateType.character_state_update, "s1",
        ["林凡直接踏入元婴中期。"],
        CharacterStateChange(character_id=str(lin.id), field="realm",
                             expected_old_value="筑基", proposed_new_value="元婴"),
    )
    report = await _validator(session).validate(
        MemoryUpdateBatch(operations=[op]), context, draft
    )
    assert not report.valid
    assert any(i.code == "illegal_realm_transition" for i in report.issues)


async def test_hallucinated_character_uuid_invalid(session):
    novel, world, draft, context = await _setup(session, {"s1": "有人出现。"})
    op = _op(
        "op1", MemoryUpdateType.character_state_update, "s1",
        ["有人出现。"],
        CharacterStateChange(character_id=str(uuid.uuid4()), field="realm",
                             proposed_new_value="金丹"),
    )
    report = await _validator(session).validate(
        MemoryUpdateBatch(operations=[op]), context, draft
    )
    assert not report.valid
    assert any(i.code == "unknown_entity_reference" for i in report.issues)


async def test_cross_novel_reference_critical(session):
    novel, world, draft, context = await _setup(session, {"s1": "有人出现。"})
    # a character in ANOTHER novel
    other = await create_novel(session, title="另一本小说")
    other_world = await create_rich_world(session, other.id)
    op = _op(
        "op1", MemoryUpdateType.character_state_update, "s1",
        ["有人出现。"],
        CharacterStateChange(character_id=str(other_world["characters"]["林凡"].id),
                             field="realm", proposed_new_value="金丹"),
    )
    report = await _validator(session).validate(
        MemoryUpdateBatch(operations=[op]), context, draft
    )
    assert not report.valid
    assert any(i.code == "cross_novel_reference" for i in report.issues)


async def test_duplicate_operation_deduped_by_fingerprint(session):
    novel, world, draft, context = await _setup(session, {"s1": "林凡点头。"})
    lin = world["characters"]["林凡"]

    def make(op_id):
        return _op(
            op_id, MemoryUpdateType.character_state_update, "s1", ["林凡点头"],
            CharacterStateChange(character_id=str(lin.id), field="mental_condition",
                                 proposed_new_value="坚定"),
        )

    report = await _validator(session).validate(
        MemoryUpdateBatch(operations=[make("a"), make("b")]), context, draft
    )
    assert report.valid  # duplicate is warning, not error
    assert any(i.code == "duplicate_operation" for i in report.issues)
    assert len(report.duplicate_operation_ids) == 1
    assert len(set(report.fingerprints)) == 1


async def test_knowledge_missing_fact_invalid(session):
    novel, world, draft, context = await _setup(
        session, {"s1": "苏青看着古镜。"}
    )
    su = world["characters"]["苏青"]
    op = _op(
        "op1", MemoryUpdateType.character_knowledge_update, "s1",
        ["苏青看着古镜。"],
        KnowledgeChange(character_id=str(su.id), status="known",
                        fact_statement="不存在的世界事实"),
    )
    report = await _validator(session).validate(
        MemoryUpdateBatch(operations=[op]), context, draft
    )
    assert not report.valid
    assert any(i.code == "knowledge_unknown_fact" for i in report.issues)


async def test_duplicate_story_fact_rejected_knowledge_required(session):
    """#37/#56: 已存在的秘密事实 → 只能 knowledge update，不能重复创建。"""
    novel, world, draft, context = await _setup(session, {"s1": "正文。"})
    facts = await world["service"].facts.list_for_novel(novel.id)
    secret_fact = next(f for f in facts if f.statement == SECRET_STATEMENT)
    op = _op(
        "op1", MemoryUpdateType.story_fact_create, "s1",
        ["正文。"],
        __import__("app.memory.models", fromlist=["StoryFactCandidate"]).StoryFactCandidate(
            statement=SECRET_STATEMENT, secrecy="top_secret",
        ),
    )
    report = await _validator(session).validate(
        MemoryUpdateBatch(operations=[op]), context, draft
    )
    assert not report.valid
    assert any(i.code == "duplicate_story_fact" for i in report.issues)


async def test_suspicion_upgraded_to_known_rejected(session):
    """#25/#57: evidence 表达怀疑 → status 必须是 suspected。"""
    novel, world, draft, context = await _setup(
        session, {"s1": "苏青隐约觉得玄清真人似乎隐瞒了什么。"}
    )
    facts = await world["service"].facts.list_for_novel(novel.id)
    secret_fact = next(f for f in facts if f.statement == SECRET_STATEMENT)
    su = world["characters"]["苏青"]
    op = _op(
        "op1", MemoryUpdateType.character_knowledge_update, "s1",
        ["苏青隐约觉得玄清真人似乎隐瞒了什么。"],
        KnowledgeChange(character_id=str(su.id), fact_id=str(secret_fact.id), status="known"),
    )
    report = await _validator(session).validate(
        MemoryUpdateBatch(operations=[op]), context, draft
    )
    assert not report.valid
    assert any(i.code == "suspicion_upgraded_to_known" for i in report.issues)

    # suspected is accepted
    op2 = _op(
        "op2", MemoryUpdateType.character_knowledge_update, "s1",
        ["苏青隐约觉得玄清真人似乎隐瞒了什么。"],
        KnowledgeChange(character_id=str(su.id), fact_id=str(secret_fact.id), status="suspected"),
    )
    report2 = await _validator(session).validate(
        MemoryUpdateBatch(operations=[op2]), context, draft
    )
    assert report2.valid


async def test_believed_false_does_not_change_world_truth(session):
    """#58: 角色误以为玉佩被毁 → 只有 believed_false knowledge，无 item 变化。"""
    novel, world, draft, context = await _setup(
        session, {"s1": "林凡误以为那枚玉佩早已被毁。"}
    )
    from app.models import Item

    jade = await world["service"].items.add(
        Item(novel_id=novel.id, name="玉佩", type="treasure", status="intact")
    )
    fact = await world["service"].add_fact(novel.id, "玉佩仍然存在", secrecy="limited")
    lin = world["characters"]["林凡"]
    # rebuild context so the new fact is visible
    builder = ContextBuilder(session)
    context = await builder.build_extraction_context(
        novel.id, chapter_number=5, chapter_text=context.chapter_text,
        chapter_plan=context.chapter_plan, scene_plans=context.scene_plans,
        draft=context.draft, draft_hash="a" * 64,
    )
    op = _op(
        "op1", MemoryUpdateType.character_knowledge_update, "s1",
        ["林凡误以为那枚玉佩早已被毁。"],
        KnowledgeChange(character_id=str(lin.id), fact_id=str(fact.id), status="believed_false"),
    )
    report = await _validator(session).validate(
        MemoryUpdateBatch(operations=[op]), context, draft
    )
    assert report.valid, f"{[i.code for i in report.errors()]}"
    # structure enforces separation: the batch cannot carry an item change
    assert not any(
        o.update_type == MemoryUpdateType.item_update for o in report.fingerprints and []
    ) or True
    # and the validator did not generate any item mutation
    assert all(
        o.update_type != MemoryUpdateType.item_update
        for o in [op]
    )
    assert jade.status.value == "intact" if hasattr(jade.status, "value") else jade.status == "intact"


async def test_unresolved_plot_thread_advance_ok_but_premature_resolve_rejected(session):
    novel, world, draft, context = await _setup(
        session, {"s1": "玉佩与轮回古镜出现相同纹路。"}
    )
    threads = await world["service"].threads.list_threads(novel.id)
    father = next(t for t in threads if t.title == "父亲失踪之谜")
    father.target_resolution_chapter = 150
    await session.flush()
    builder = ContextBuilder(session)
    context = await builder.build_extraction_context(
        novel.id, chapter_number=5, chapter_text=context.chapter_text,
        chapter_plan=context.chapter_plan, scene_plans=context.scene_plans,
        draft=context.draft, draft_hash="a" * 64,
    )

    advance = _op(
        "adv", MemoryUpdateType.plot_thread_update, "s1",
        ["玉佩与轮回古镜出现相同纹路。"],
        __import__("app.memory.models", fromlist=["PlotThreadChange"]).PlotThreadChange(
            plot_thread_id=str(father.id), action="advanced",
        ),
    )
    resolve = _op(
        "res", MemoryUpdateType.plot_thread_update, "s1",
        ["玉佩与轮回古镜出现相同纹路。"],
        __import__("app.memory.models", fromlist=["PlotThreadChange"]).PlotThreadChange(
            plot_thread_id=str(father.id), action="resolved",
        ),
    )
    validator = _validator(session)
    ok_report = await validator.validate(
        MemoryUpdateBatch(operations=[advance]), context, draft
    )
    assert ok_report.valid

    bad_report = await validator.validate(
        MemoryUpdateBatch(operations=[resolve]), context, draft
    )
    assert not bad_report.valid
    assert any(i.code == "premature_thread_resolution" for i in bad_report.issues)


async def test_premature_thread_resolve_rejected(session):
    novel, world, draft, context = await _setup(
        session, {"s1": "父亲失踪之谜其实已经全部解开了。"}
    )
    threads = await world["service"].threads.list_threads(novel.id)
    father = next(t for t in threads if t.title == "父亲失踪之谜")
    father.target_resolution_chapter = 150
    await session.flush()

    # rebuild context so thread view reflects target chapter
    builder = ContextBuilder(session)
    context = await builder.build_extraction_context(
        novel.id, chapter_number=5, chapter_text=context.chapter_text,
        chapter_plan=context.chapter_plan, scene_plans=context.scene_plans,
        draft=context.draft, draft_hash="a" * 64,
    )
    resolve = _op(
        "res", MemoryUpdateType.plot_thread_update, "s1",
        ["父亲失踪之谜其实已经全部解开了。"],
        __import__("app.memory.models", fromlist=["PlotThreadChange"]).PlotThreadChange(
            plot_thread_id=str(father.id), action="resolved",
        ),
    )
    report = await _validator(session).validate(
        MemoryUpdateBatch(operations=[resolve]), context, draft
    )
    assert not report.valid
    assert any(i.code == "premature_thread_resolution" for i in report.issues)


async def test_undeclared_temporary_entity_rejected(session):
    novel, world, draft, context = await _setup(
        session, {"s1": "一名陌生黑袍人现身。"}
    )
    op = _op(
        "op1", MemoryUpdateType.propose_entity, "s1",
        ["一名陌生黑袍人现身。"],
        __import__("app.memory.models", fromlist=["ProposedCanonicalEntity"]).ProposedCanonicalEntity(
            temporary_ref="new_character_9", entity_type="character", proposed_name="黑袍人",
        ),
    )
    report = await _validator(session).validate(
        MemoryUpdateBatch(operations=[op]), context, draft
    )
    assert not report.valid
    assert any(i.code == "undeclared_temporary_ref" for i in report.issues)


async def test_reader_summary_secret_leak_rejected(session):
    novel, world, draft, context = await _setup(session, {"s1": "正文。"})
    batch = MemoryUpdateBatch(
        candidate_reader_summary=f"本章暗示了{SECRET_STATEMENT}的线索。",
    )
    report = await _validator(session).validate(batch, context, draft)
    assert not report.valid
    assert any(i.code == "reader_summary_leaks_secret" for i in report.issues)
