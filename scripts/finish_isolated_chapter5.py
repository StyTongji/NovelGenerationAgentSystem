"""Exact, primary-agent editorial closure of the frozen fifth chapter.

No replan/Writer calls. Re-check revised prose and validate the reviewed batch.
Evidence is rebound by EXACT text and arithmetic offsets, never fuzzy matching.
"""
import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.config import get_settings, LLMProviderConfig
from app.commit.models import ChapterPreviewPackage
from app.memory.catalog import EvidenceCatalogBuilder
from app.memory.models import MemoryUpdateOperation, EvidenceRef
from app.quality.review_assistance import ReviewAssistanceAssessment
from app.services.author_review import declare_draft_locations
from app.services.memory_extraction import MemoryExtractionService
from app.services.trial_artifacts import write_new
from scripts.isolated_continuation import ROOT, NOVEL, MODEL, TestManager, test_commit, fingerprint

EXPERIMENT = ROOT / "real_trial_runs/isolated_four_20261031_6XeYOj/repaired"
RUN = "4be76b6fbd2b4dfc80ca88ba97bf8a65"
SOURCE = EXPERIMENT / "studio" / RUN / "chapter_05/attempts/c215bb83c62f"
OUTPUT = EXPERIMENT / "chapter5_final_review"


def prepared():
    package = ChapterPreviewPackage.model_validate_json((SOURCE / "preview_package.json").read_text())
    draft = package.draft.model_copy(deep=True)
    shifts = {}
    wood_before = "沈渡没立刻接话，手在摊板底下摸了一把。指尖碰到个硬东西，他抠出来，就着残灯看——木牌，正面刻着一个“周”字，字口很新。"
    wood_after = "沈渡没立刻接话。他从袖中摸出方才收起的那枚木牌，就着残灯看——正面刻着一个“周”字，字口很新。"
    opening_before = "沈渡蹲在墙根，把布包解开，摊板一块块铺出去。"
    opening_after = "天色渐亮，沈渡在坊市西头寻了处墙根的空地，把布包解开，摊板一块块铺出去。"
    for scene in draft.scenes:
        if scene.scene_id in {"ch5_sc1", "ch5_sc4"}:
            old, new = (wood_before, wood_after) if scene.scene_id == "ch5_sc1" else (opening_before, opening_after)
            assert scene.text.count(old) == 1
            start = scene.text.index(old)
            shifts[scene.scene_id] = (start, start + len(old), len(new) - len(old))
            scene.text = scene.text.replace(old, new)
            scene.generation_metadata = None
        if scene.scene_id == "ch5_sc3":
            marker = "巷子另一头豁亮些，摆摊的杂"
            assert scene.text.count(marker) == 1
            scene.text = scene.text[:scene.text.index(marker)].rstrip()
            scene.generation_metadata = None
    draft.full_text = "\n\n".join(scene.text for scene in draft.scenes)
    draft.generation_metadata = [scene.generation_metadata for scene in draft.scenes if scene.generation_metadata]
    return package, draft, shifts


async def main(prepare_only):
    marker = json.loads((EXPERIMENT / "experiment.json").read_text())
    assert marker["kind"] == "isolated_test" and marker["automated_test_authorized"]
    assert fingerprint(Path(marker["source_database"])) == marker["source_fingerprint"]
    package, draft, shifts = prepared()
    catalog = EvidenceCatalogBuilder.build(draft)
    OUTPUT.mkdir(exist_ok=True)
    if prepare_only:
        write_new(OUTPUT / "draft.json", draft.model_dump(mode="json"))
        write_new(OUTPUT / "catalog.json", catalog.model_dump(mode="json"))
        print(json.dumps({"characters": len(draft.full_text), "catalog": str(OUTPUT / 'catalog.json')}))
        return
    spec = json.loads((OUTPUT / "editorial.json").read_text())
    entries = catalog.by_id()
    declarations = spec["location_declarations"]
    draft = declare_draft_locations(draft, declarations)
    batch = package.memory_result.batch.model_copy(deep=True)
    batch.operations = [op for op in batch.operations if op.operation_id not in set(spec["drop_operations"])]
    overridden = set(spec["evidence_overrides"])
    mapping = []
    for op in batch.operations:
        if op.operation_id in overridden:
            change = spec["evidence_overrides"][op.operation_id]
            op.source_scene_id = change["scene"]
            op.evidence_refs = [EvidenceRef(evidence_id=ref) for ref in change["ids"]]
        else:
            refs = []
            for evidence in op.evidence:
                start, end = evidence.start_char, evidence.end_char
                shift = shifts.get(evidence.scene_id)
                if shift and start >= shift[1]:
                    start, end = start + shift[2], end + shift[2]
                elif shift and end > shift[0]:
                    raise ValueError(f"Changed evidence requires explicit editorial replacement: {op.operation_id}")
                matches = [entry for entry in catalog.entries if entry.scene_id == evidence.scene_id
                    and entry.start_char == start and entry.end_char == end and entry.exact_text == evidence.exact_quote]
                assert len(matches) == 1, (op.operation_id, evidence.exact_quote)
                refs.append(EvidenceRef(evidence_id=matches[0].evidence_id))
                mapping.append({"operation": op.operation_id, "old": evidence.evidence_id,
                    "new": matches[0].evidence_id, "exact_text_unchanged": True})
            op.evidence_refs = refs
        op.evidence = []
    for change in spec["changes"]:
        op = next(op for op in batch.operations if op.operation_id == change["operation_id"])
        setattr(getattr(op, change["payload"]), change["field"], change["value"])
    batch.operations.extend(MemoryUpdateOperation.model_validate(row) for row in spec["additions"])
    # Rehydrate refs after editorial assignment; resolved evidence remains server-owned.
    from app.memory.models import MemoryUpdateBatch
    batch = MemoryUpdateBatch.model_validate(batch.model_dump(mode="json"))
    key = re.findall(r"sk-[A-Za-z0-9_-]+", Path("/home/sty/Desktop/ds_key.txt").read_text())
    assert len(key) == 1
    os.environ["NOVEL_ISOLATED_DEEPSEEK_KEY"] = key[0]
    settings = get_settings().model_copy(deep=True)
    settings.llm.providers = {"deepseek": LLMProviderConfig(base_url="https://api.deepseek.com",
        api_key_env="NOVEL_ISOLATED_DEEPSEEK_KEY", model=MODEL,
        model_capabilities=json.loads((EXPERIMENT / "native_capabilities.json").read_text())["capabilities"])}
    engine = create_async_engine(f"sqlite+aiosqlite:///{EXPERIMENT / 'trial.db'}")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    manager = TestManager(EXPERIMENT / "studio", maker, settings)
    providers = []
    try:
        run = manager.get(RUN)
        chapter = manager._chapter(run)
        assert run.status == "awaiting_review" and chapter.attempt_id == "c215bb83c62f"
        chapter.source_attempt, chapter.attempt_id = chapter.attempt_id, "primary_final_v2"
        destination = manager._attempt_path(run, chapter)
        destination.mkdir()
        async with maker() as session:
            memory = await MemoryExtractionService(session, require_evidence_refs=True).validate_batch(
                uuid.UUID(NOVEL), draft, package.plan_result.plan, batch, dry_run=True)
            write_new(destination / "validation_report.json", memory.validation_report.model_dump(mode="json"))
            assert memory.validation_report.valid, memory.validation_report
            assert memory.dry_run.applicable, memory.dry_run
            service, reviewer, providers = await manager._services(session, run, chapter)
            continuity = await service.continuity_service.check_draft(uuid.UUID(NOVEL), draft, package.plan_result.plan)
            write_new(destination / "continuity_report.json", continuity.model_dump(mode="json"))
            assert continuity.valid, continuity
            quality = await reviewer.review(draft, package.plan_result.plan)
            write_new(destination / "quality.json", quality.model_dump(mode="json"))
            assert not quality.needs_revision, quality
        package.draft, package.memory_result, package.continuity_report = draft, memory, continuity
        package.preview_package_hash = ChapterPreviewPackage.compute_hash(plan=package.plan_result.plan,
            draft=draft, memory_result=memory, continuity_report=continuity, story_state_hash=package.story_state_hash)
        write_new(destination / "preview_package.json", package.model_dump(mode="json"))
        write_new(destination / "chapter.txt", draft.full_text, text=True)
        write_new(destination / "primary_agent_test_review.json", {"human_approved": False,
            "source": str(SOURCE), "preview_package_hash": package.preview_package_hash,
            "editorial": spec, "exact_evidence_rebinding": mapping,
            "review_scope": "Primary agent read full source prose and every operation; corrected wood ownership, repeated reveal, time, exact locations and enduring protector identity/pact. Revalidated all operations against revised prose. No fuzzy matching; no invented UUIDs."})
        assessment = ReviewAssistanceAssessment(operation_reviews=[{
            "operation_id": op.operation_id, "verdict": "correct",
            "reason": "Bound primary-agent review plus server exact evidence/domain validation; detailed editorial and exact offset map retained."}
            for op in memory.batch.operations], missing=[], quality_scores=quality.scores,
            summary="非人工测试批准：原有正确项保留，缺失人物/庇护事实/位置已补齐，重复获取及重复揭露已纠正。")
        await test_commit(maker, package, chapter.attempt_id, assessment, destination)
        run.status = chapter.status = "completed"
        chapter.assistance_file = None
        chapter.characters = len(draft.full_text)
        manager._save(run)
        write_new(EXPERIMENT / "chapter_05.txt", draft.full_text, text=True)
        write_new(EXPERIMENT / "chapters_02_05.txt", "择道者 · 第 2—5 章\n隔离测试阅读稿 · 非人工批准\n\n" + "\n\n".join(
            f"第 {number} 章\n\n" + (EXPERIMENT / f"chapter_{number:02d}.txt").read_text() for number in range(2, 6)), text=True)
    finally:
        for provider in providers:
            await provider.aclose()
        await manager.close()
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", action="store_true")
    asyncio.run(main(parser.parse_args().prepare))
