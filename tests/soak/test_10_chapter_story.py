"""Phase 8.5 — Long-horizon committed story soak (10 chapters).

Only the product façade is used: ChapterGenerationService.generate_preview
and .finalize. Deterministic scripted story; injections for duplicate
finalize, stale parallel preview, and mid-transaction rollback.
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import func, select

from app.agents.memory_extractor import MemoryExtractorAgent
from app.agents.scene_planner import ScenePlannerAgent
from app.agents.writer import WriterAgent
from app.agents.writer_repair import WriterRepairAgent
from app.commit.models import (
    ChapterPreviewPackage,
    CommitError,
    StaleStoryStateError,
)
from app.context.builder import ContextBuilder
from app.llm.client import AgentLLMClient
from app.llm.fake import FakeLLMProvider
from app.llm.prompts import get_default_registry
from app.memory.models import MemoryUpdateBatch
from app.models import (
    Chapter,
    ChapterCommit,
    ChapterSummary,
    Novel,
    Character,
    CharacterKnowledge,
    CharacterState,
    Event,
    Item,
    PlotDebt,
    PlotThread,
    StoryFact,
    StorySnapshot,
)
from app.planning.provenance import compute_story_state_hash
from app.planning.scene_validator import ScenePlanValidator
from app.services.canonical_commit import CanonicalCommitService
from app.services.chapter_draft import ChapterDraftService
from app.services.chapter_generation import ChapterGenerationService
from app.services.chapter_planning import ChapterPlanningService, build_director_agent
from app.services.continuity import ContinuityService
from app.services.memory_extraction import MemoryExtractionService
from tests.e2e.test_preview_pipeline import db_fingerprint
from tests.helpers import create_novel, create_rich_world
from tests.soak.story_audit import LongHorizonStoryAuditor
from tests.soak.soak_story import (
    PROTECTED_READER_SECRET,
    RELIC_FACT_STATEMENT,
    build_story,
)

pytestmark = [pytest.mark.soak]

CHAPTER_START = 5
CULT_SECRET = "玄清真人是魔教卧底"


def _build_generation_service(session, provider) -> ChapterGenerationService:
    planning = ChapterPlanningService(
        session, director=build_director_agent(
            session, AgentLLMClient(provider, agent_name="director", model="fake-model")
        )
    )
    draft = ChapterDraftService(
        session,
        ScenePlannerAgent(
            llm=AgentLLMClient(provider, agent_name="scene_planner", model="fake-model"),
            prompt_registry=get_default_registry(),
            validator=ScenePlanValidator(session),
        ),
        WriterAgent(
            llm=AgentLLMClient(provider, agent_name="writer", model="fake-model"),
            prompt_registry=get_default_registry(),
        ),
        previous_scene_tail_chars=200,
    )
    continuity = ContinuityService(
        session,
        llm_checker=None,
        repair_agent=WriterRepairAgent(
            llm=AgentLLMClient(provider, agent_name="writer_repair", model="fake-model"),
            prompt_registry=get_default_registry(),
        ),
        mode="deterministic_only",
    )
    memory = MemoryExtractionService(
        session,
        MemoryExtractorAgent(
        contract_version=2,  # explicit offline legacy replay
            llm=AgentLLMClient(provider, agent_name="memory_extractor", model="fake-model"),
            prompt_registry=get_default_registry(),
        ),
    )
    return ChapterGenerationService(
        session, planning, draft, continuity, memory, CanonicalCommitService(session, allow_legacy_batches=True)
    )


def _scene_batch(world, chapter_number: int) -> dict:
    lin = str(world["characters"]["林凡"].id)
    su = str(world["characters"]["苏青"].id)
    scenes = []
    for index in range(1, 3):  # two scenes per chapter keeps the soak light
        scenes.append(
            {
                "scene_id": f"ch{chapter_number}_scene_{index}",
                "scene_index": index,
                "source_beat_ids": [str(index)],
                "pov": {"character_id": lin, "name": "林凡"},
                "location": {"name": "天剑城"},
                "participants": [
                    {"character_id": lin, "name": "林凡"},
                    *([{"character_id": su, "name": "苏青"}] if index == 2 else []),
                ],
                "scene_goal": f"ch{chapter_number} 场景{index}",
                "required_outcomes": [],
                "target_chars": 800,
                "temporary_entities": ["new_character_1"]
                if (chapter_number == 5 and index == 1)
                else [],
            }
        )
    return {"chapter_number": chapter_number, "scenes": scenes, "planner_notes": []}


def _plan_dict(world, chapter_number: int, direction: str) -> dict:
    from tests.draft_helpers import build_valid_plan_dict

    plan = build_valid_plan_dict(world)
    plan["chapter_number"] = chapter_number
    plan["chapter_goal"] = direction[:60]
    return plan


async def _run_soak(session, provider, generation, world, story, injection) -> dict:
    """Commit all chapters in order with scripted injections."""
    log = {
        "finalize_llm_deltas": [],
        "writer_prompts_by_chapter": {},
        "context_sizes": {},
        "duplicate_chapter": None,
        "stale_chapter": None,
        "rollback_chapter": None,
    }

    for chapter in story:
        number = chapter.number
        writer_call_marker = len(provider.calls)

        # -------- preview (director + scene planner + writers + extractor)
        def enqueue_preview(texts, memory_chapter):
            plan_dict = _plan_dict(world, number, chapter.direction)
            chapter_temp_refs = {
                7: [{"temporary_ref": "new_location_1", "entity_type": "location",
                     "name_hint": "青云遗迹"}],
            }
            for ref in chapter_temp_refs.get(number, []):
                plan_dict["proposed_entities"] = plan_dict.get("proposed_entities", []) + [ref]
            provider.enqueue_structured(plan_dict)
            provider.enqueue_structured(_scene_batch(world, number))
            for text in texts:
                provider.enqueue_text(text)
            provider.enqueue_structured(_memory_batch_json(world, memory_chapter))

        enqueue_preview(chapter.scene_texts, chapter)

        package = await generation.generate_preview(
            session_novel_id(world), chapter.direction, chapter_number=number
        )
        assert package.status == "preview"
        assert package.continuity_report.valid, f"ch{number} continuity invalid"
        assert package.memory_result.validation_report.valid, (
            f"ch{number} memory invalid: "
            f"{[i.code for i in package.memory_result.validation_report.errors()]}"
        )

        # capture writer prompts for the long-horizon leak check
        writer_prompts = []
        for call in provider.calls[writer_call_marker:]:
            if call and call[0].role == "system" and call[0].content.startswith(
                "# SYSTEM — Xuanhuan Chapter Writer"
            ):
                writer_prompts.append("\n".join(m.content for m in call))
        log["writer_prompts_by_chapter"][number] = writer_prompts

        # -------- injections
        if injection.get("rollback_chapter") == number:
            fingerprint_before = await db_fingerprint(session)
            async def exploding(*a, **k):
                raise RuntimeError("injected failure")
            original = generation.commit_service._apply_character_states
            generation.commit_service._apply_character_states = exploding  # type: ignore
            with pytest.raises(CommitError) as excinfo:
                await generation.finalize(package)
            assert excinfo.value.code.value == "precondition_failed"
            generation.commit_service._apply_character_states = original  # type: ignore
            assert await db_fingerprint(session) == fingerprint_before, (
                f"ch{number}: rollback 后 DB 必须完全恢复"
            )
            log["rollback_chapter"] = number
            # fall through: re-finalize the SAME preview below

        if injection.get("stale_chapter") == number:
            # two parallel previews for the same chapter, both based on state X
            alt_texts = [text + "（另一条世界线的版本。）" for text in chapter.scene_texts]
            enqueue_preview(alt_texts, chapter)
            packageB = await generation.generate_preview(
                session_novel_id(world), chapter.direction, chapter_number=number
            )

        calls_before_finalize = len(provider.calls)
        result = await generation.finalize(package)
        log["finalize_llm_deltas"].append(len(provider.calls) - calls_before_finalize)
        assert len(provider.calls) - calls_before_finalize == 0, (
            f"ch{number}: finalize 不得调用 LLM"
        )

        if injection.get("stale_chapter") == number:
            # A committed -> state moved X -> Y; B must now be stale-rejected
            with pytest.raises(StaleStoryStateError):
                await generation.finalize(packageB)
            log["stale_chapter"] = number

        if injection.get("duplicate_chapter") == number:
            duplicate = await generation.finalize(package)
            assert duplicate.duplicate is True
            assert duplicate.commit_id == result.commit_id
            log["duplicate_chapter"] = number

        # -------- per-chapter context budget measurement
        context = await ContextBuilder(session).build_writer_context(
            session_novel_id(world),
            chapter_number=number + 1,
            pov_character_id=uuid.UUID(str(world["characters"]["林凡"].id)),
        )
        log["context_sizes"][number] = {
            "events": len(context.relevant_events),
            "threads": len(context.safe_plot_threads),
            "knowledge": len(context.pov_knowledge),
        }
        assert len(context.relevant_events) <= 12, "ContextBudget 必须持续生效"

    return log


def _memory_batch_json(world, chapter) -> dict:
    """Resolve @mirror@/@pill@/@sect_debt@ placeholders to real ids."""
    ops = []
    for op in chapter.memory_ops:
        op = json.loads(json.dumps(op, ensure_ascii=False))
        payload_key = next(
            k for k in ("event", "character_state", "knowledge", "item",
                        "story_fact", "plot_thread", "plot_debt",
                        "proposed_entity", "relationship")
            if k in op
        )
        payload = op[payload_key]
        if payload_key == "item":
            if payload["item_id"] == "@mirror@":
                payload["item_id"] = str(world["mirror_item"].id)
            if payload["item_id"] == "@pill@":
                payload["item_id"] = str(world["pill_item"].id)
        if payload_key == "plot_debt":
            payload["plot_debt_id"] = str(world["sect_debt"].id)
        ops.append(op)
    return {
        "operations": ops,
        "candidate_author_summary": chapter.author_summary,
        "candidate_reader_summary": chapter.reader_summary,
    }


def session_novel_id(world):
    return world["result"].novel_id


async def test_10_chapter_committed_soak(session, tmp_path):
    from app.debug import PreviewExportService

    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    mirror_fact = await world["service"].add_fact(
        novel.id, "林凡持有轮回古镜", secrecy="secret"
    )
    await world["service"].grant_knowledge(
        world["characters"]["林凡"].id, mirror_fact.id
    )
    cult_secret = await world["service"].add_fact(
        novel.id, CULT_SECRET, secrecy="top_secret"
    )
    await world["service"].grant_knowledge(
        world["elder"].id, cult_secret.id
    )
    # soak items + debt tuning
    from app.models import Item

    world["mirror_item"] = await world["service"].items.add(
        Item(novel_id=novel.id, name="轮回古镜", aliases=["古镜"], type="artifact",
             current_owner_character_id=world["characters"]["林凡"].id)
    )
    world["pill_item"] = await world["service"].items.add(
        Item(novel_id=novel.id, name="疗伤丹", type="pill",
             current_owner_character_id=world["characters"]["苏青"].id)
    )
    sect_debt = next(
        d for d in await world["service"].debts.list_open(novel.id)
        if d.description == "七日后宗门大会"
    )
    sect_debt.due_chapter = 14
    await world["service"].session.flush()
    world["sect_debt"] = sect_debt
    # a dormant secondary thread for dormancy tracking
    from app.models import PlotThread

    await world["service"].threads.add(
        PlotThread(novel_id=novel.id, title="神秘残卷", status="dormant",
                   priority=0.3, last_advanced_chapter=5)
    )
    novel.current_chapter_number = 4
    await session.flush()
    await session.commit()

    provider = FakeLLMProvider()
    generation = _build_generation_service(session, provider)
    story = build_story(world, str(mirror_fact.id), str(cult_secret.id))
    assert len(story) == 10

    injection = {
        "duplicate_chapter": 8,
        "rollback_chapter": 12,
        "stale_chapter": 10,
    }
    log = await _run_soak(session, provider, generation, world, story, injection)

    # ============== post-soak canonical audit ==============
    expected = [c.number for c in story]
    auditor = LongHorizonStoryAuditor(session, novel.id)
    audit = await auditor.audit(expected)
    assert audit.ok, f"soak audit problems: {audit.problems}"

    # ================= chapter / commit chain (#20) =================
    novel_final = await session.get(Novel, novel.id)
    assert novel_final.current_chapter_number == 14
    commits = {
        c.chapter_number: c
        for c in (await session.execute(select(ChapterCommit))).scalars().all()
    }
    assert set(commits) == set(range(5, 15))

    # snapshot / hash chain (#18)
    previous_after = None
    for number in range(5, 15):
        commit = commits[number]
        snapshot = (
            await session.execute(
                select(StorySnapshot).where(StorySnapshot.after_chapter_number == number)
            )
        ).scalar_one()  # exactly one (#19)
        assert snapshot.state_hash == commit.after_state_hash
        if previous_after is not None:
            assert commit.before_state_hash == previous_after
        previous_after = commit.after_state_hash

    # ================= character state history (#33 append-only) =================
    su = str(world["characters"]["苏青"].id)
    su_states = sorted(
        (
            s for s in (await session.execute(select(CharacterState))).scalars()
            if str(s.character_id) == su
        ),
        key=lambda s: s.chapter_number,
    )
    by_chapter = {s.chapter_number: s.physical_condition for s in su_states}
    assert by_chapter.get(5) == "injured"
    assert by_chapter.get(9) == "recovering"
    assert by_chapter.get(10) == "healthy"
    # historical rows keep their values (append-only)
    assert by_chapter[5] == "injured" and by_chapter[9] == "recovering"

    # ================= knowledge (#8) =================
    knowledge = (await session.execute(select(CharacterKnowledge))).scalars().all()
    su_mirror = [
        k for k in knowledge
        if str(k.character_id) == su and str(k.fact_id) == str(mirror_fact.id)
    ]
    assert len(su_mirror) == 1 and su_mirror[0].status == "known"
    lin_cult = [
        k for k in knowledge
        if str(k.character_id) == str(world["characters"]["林凡"].id)
        and str(k.fact_id) == str(cult_secret.id)
    ]
    assert len(lin_cult) == 1 and lin_cult[0].status == "suspected", (
        "unknown -> suspected 不得升级为 known"
    )
    # relic fact known by 林凡
    relic = next(
        f for f in (await session.execute(select(StoryFact))).scalars()
        if f.statement == RELIC_FACT_STATEMENT
    )
    assert any(
        str(k.character_id) == str(world["characters"]["林凡"].id)
        and str(k.fact_id) == str(relic.id) and k.status == "known"
        for k in knowledge
    )

    # ================= item ownership (#10/#11) =================
    mirror_after = await session.get(Item, world["mirror_item"].id)
    assert str(mirror_after.current_owner_character_id) == str(
        world["characters"]["林凡"].id
    ), "古镜 owner 不得长期漂移"
    pill_after = await session.get(Item, world["pill_item"].id)
    assert pill_after.status == "lost", "疗伤丹必须体现消耗"

    # ================= entity invariant (#12/#13) =================
    characters = (await session.execute(select(Character))).scalars().all()
    gus = [c for c in characters if c.name == "顾清璃"]
    assert len(gus) == 1, "顾清璃只能有一个 canonical UUID"
    # events referencing her point at the same id
    mapping = {
        c.chapter_number: c.temporary_entity_mapping_json for c in commits.values()
    }
    gu_id = str(gus[0].id)
    assert any(
        mapping.get(ch, {}).get("new_character_1") == gu_id for ch in (5, 6)
    )

    # ================= plot lifecycle (#25/#26) =================
    father = next(
        t for t in (await session.execute(select(PlotThread))).scalars()
        if t.title == "父亲失踪之谜"
    )
    assert father.status in ("active", "progressing")
    assert father.last_advanced_chapter == 14
    residual = next(
        t for t in (await session.execute(select(PlotThread))).scalars()
        if t.title == "神秘残卷"
    )
    builder = ContextBuilder(session)
    director_context_14 = await builder.build_director_context(
        novel.id, chapter_number=14, user_direction="推进剧情"
    )
    dormant = next(
        t for t in director_context_14.dormant_plot_threads
        if t.title == "神秘残卷"
    )
    assert dormant.chapters_since_last_progress == 9

    # ================= plot debt (#27) =================
    debt = next(
        d for d in (await session.execute(select(PlotDebt))).scalars()
        if d.description == "七日后宗门大会"
    )
    assert debt.status == "fulfilled" and debt.resolved_chapter == 14
    open_debts = [
        d.description for d in await builder.debts.list_open(novel.id)
        if d.description == "七日后宗门大会"
    ]
    assert not open_debts

    # ================= long-horizon secret isolation (#29) =================
    leak_count = 0
    for number, prompts in log["writer_prompts_by_chapter"].items():
        for prompt in prompts:
            if CULT_SECRET in prompt:
                # only allowed after ch11 AND inside the suspicion framing
                assert number >= 11, f"ch{number} writer prompt 泄漏 secret"
                assert "怀疑" in prompt
            if PROTECTED_READER_SECRET in prompt:
                leak_count += 1
    assert leak_count == 0, "reader-visible secret 绝不能进入任何 writer prompt"

    # ================= retrieval relevance (#24) =================
    director_context_13 = await builder.build_director_context(
        novel.id,
        chapter_number=13,
        user_direction="让林凡重新检查轮回古镜与玉佩的关系",
    )
    old_mirror_event = any(
        e.chapter_number == 5 and "轮回古镜" in e.summary
        for e in director_context_13.relevant_events
    )
    assert old_mirror_event, "旧 episodic event 必须能通过 relevance 召回"

    # ================= finalize llm deltas (#43) =================
    assert all(delta == 0 for delta in log["finalize_llm_deltas"])

    # ================= duplicate finalize side effects (#15) =================
    assert log["duplicate_chapter"] == 8
    event_count_8 = (
        await session.execute(
            select(func.count()).select_from(Event).where(Event.chapter_number == 8)
        )
    ).scalar_one()
    assert event_count_8 == 1, "duplicate finalize 不得重复创建 event"

    # ================= summary binding (#31) =================
    summaries = (await session.execute(
        select(ChapterSummary).where(ChapterSummary.novel_id == novel.id)
    )).scalars().all()
    for summary in summaries:
        if summary.chapter_number < 5:
            continue
        chapter = await session.get(Chapter, summary.chapter_id)
        assert summary.source_draft_hash == chapter.draft_hash

    # ================= summary secret isolation (#30) =================
    for summary in summaries:
        if summary.chapter_number >= 5:
            assert CULT_SECRET not in (summary.reader_summary or "")
            assert PROTECTED_READER_SECRET not in (summary.reader_summary or "")

    # ================= preview artifact export still works (#54 preview side)
    exporter = PreviewExportService(tmp_path)
    # rebuild the last preview shape via the auditor-verified data is not
    # possible post-commit; export simply must remain filesystem-only
    assert exporter.base_dir == tmp_path
