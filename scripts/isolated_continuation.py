"""User-authorized automated TEST continuation; never writes the source database.

Artifacts and approvals explicitly say non-human. Normal finalize rejects them.
Run again with the same --experiment to resume saved stages and committed chapters.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from app.commit.models import ChapterPreviewPackage, ReviewApproval
from app.config import get_settings, LLMProviderConfig
from app.llm.budget import TrialBudget
from app.llm.client import AgentLLMClient
from app.llm.openai_compatible import OpenAICompatibleProvider
from app.memory.catalog import EvidenceCatalogBuilder
from app.memory.models import MemoryUpdateBatchV3, MemoryUpdateBatch, MemoryUpdateOperation
from app.planning.provenance import build_story_state_projection
from app.schemas.studio import RunInput, ItemDeclarationInput, StudioRun, ChapterProgress
from app.services.author_review import build_review_before_state
from app.services.canonical_commit import CanonicalCommitService
from app.services.chapter_generation import ChapterGenerationService
from app.services.studio import StudioManager
from app.services.trial_artifacts import write_new, digest

ROOT = Path(__file__).resolve().parents[1]
SOURCE_RUN = "4336af8fb4ac4862833b0c66878917ed"
SOURCE_ATTEMPT = "54d8010f3fa3"
NOVEL = "4ca78726-e108-4745-a33e-728650a8b8e3"
MODEL = "deepseek-flash"


def log(**value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


def fingerprint(path):
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        return digest(list(db.iterdump()))


class MemoryRepair(BaseModel):
    batch: MemoryUpdateBatchV3
    item_declarations: list[ItemDeclarationInput] = Field(default_factory=list)
    change_log: list[str]


class TestManager(StudioManager):
    def _runtime(self, run, chapter):
        registry, settings, providers = super()._runtime(run, chapter)
        for provider in providers:
            provider.budget = self.shared_budget(chapter.number)
            provider.trace_path = self.root.parent / "llm_calls.jsonl"
        return registry, settings, providers

    def shared_budget(self, chapter):
        return TrialBudget(self.root.parent / "usage_ledger.json", chapter,
            max_calls=50, chapter_tokens=450_000, run_tokens=1_700_000)


async def wait(manager, run):
    task = manager.tasks[run.id]
    while not task.done():
        await asyncio.wait({task}, timeout=20)
        log(chapter=run.current_chapter, status=run.status, stage=run.active_stage,
            error=run.error, budget=manager.shared_budget(run.current_chapter).summary())
    await task


async def test_commit(maker, package, attempt, assessment, destination):
    receipt = ReviewApproval(attempt_id=attempt, reviewed_by="TEST_AGENT_NON_HUMAN",
        review_kind="automated_test", approved=True, semantic_review_complete=True,
        draft_hash=hashlib.sha256(package.draft.full_text.encode()).hexdigest(),
        memory_hash=ReviewApproval.hash_memory(package.memory_result),
        preview_package_hash=package.preview_package_hash,
        operation_reviews={r.operation_id: r.verdict for r in assessment.operation_reviews},
        missing=assessment.missing, quality_scores=assessment.quality_scores.model_dump())
    receipt_path = destination / f"automated_test_receipt_{digest(receipt.model_dump(mode='json'))[:12]}.json"
    if not receipt_path.exists():
        write_new(receipt_path, receipt.model_dump(mode="json"))
    async with maker() as session:
        service = ChapterGenerationService(session, None, None, None, None,
            CanonicalCommitService(session, allow_automated_test_review=True))
        result = await service.finalize(package, attempt_id=attempt, approval=receipt)
        duplicate = await service.finalize(package, attempt_id=attempt, approval=receipt)
        assert duplicate.duplicate and duplicate.after_story_state_hash == result.after_story_state_hash
    write_new(destination / "test_commit.json", {"human_approved": False,
        "production_commit": False, "result": result.model_dump(mode="json"),
        "duplicate": duplicate.model_dump(mode="json")})
    log(chapter=package.chapter_number, test_committed=True, human_approved=False,
        characters=len(package.draft.full_text), operations=len(package.memory_result.batch.operations))


async def review(manager, run):
    await manager.assist_review(run.id, run.current_chapter)
    await wait(manager, run)
    chapter = manager._chapter(run)
    if not chapter.assistance_file:
        raise RuntimeError(run.error or "Review produced no assessment")
    from app.quality.review_assistance import ReviewAssistanceAssessment
    return ReviewAssistanceAssessment.model_validate(json.loads(
        (manager._attempt_path(run, chapter) / chapter.assistance_file).read_text()))


def new_declarations(package, inputs):
    """A repair may echo an existing item declaration; never overwrite it."""
    existing = {entity.temporary_ref: entity for entity in package.draft.temporary_entities}
    inputs = dict(inputs)
    declarations = []
    for row in inputs.get("item_declarations", []):
        old = existing.get(row["temporary_ref"])
        if old is None:
            declarations.append(row)
        elif old.entity_type != "item" or old.name_hint != row["name"]:
            raise ValueError("Repair attempted to replace an existing temporary identity")
    inputs["item_declarations"] = declarations
    return inputs


async def repair(manager, run, issues):
    chapter = manager._chapter(run)
    package = manager._package(run, chapter)
    registry, settings, providers = manager._runtime(run, chapter)
    try:
        async with manager.session_factory() as session:
            before = await build_review_before_state(session, package)
        client = registry.client_for_agent("memory_extractor", settings, prompt_version="isolated_memory_repair_v1")
        client.max_tokens = 16384
        batch = package.memory_result.batch.model_dump(mode="json", exclude_none=True)
        for operation in batch["operations"]:
            operation.pop("evidence", None)  # Server-owned; do not send duplicated exact quotes.
        catalog = EvidenceCatalogBuilder.build(package.draft)
        result = await client.generate_structured(
            system=(ROOT / "prompts/isolated_memory_repair_v1.md").read_text(),
            user=json.dumps({"temporary_entities": [e.model_dump(mode="json") for e in package.draft.temporary_entities],
                "draft_hash": catalog.draft_hash,
                "catalog": [{"id": e.evidence_id, "scene": e.scene_id, "text": e.exact_text} for e in catalog.entries],
                "before_state": before, "batch": batch,
                "issues": issues}, ensure_ascii=False, separators=(",", ":")), response_model=MemoryRepair)
        write_new(manager._attempt_path(run, chapter) / f"repair_proposal_{uuid.uuid4().hex[:8]}.json",
            result.data.model_dump(mode="json"))
        await manager.new_candidate(run.id, chapter.number, "memory",
            new_declarations(package, result.data.model_dump(mode="json")))
        await wait(manager, run)
    finally:
        for provider in providers:
            await provider.aclose()


async def main(args):
    experiment = args.experiment.resolve()
    experiment.mkdir(parents=True, exist_ok=True)
    source_db = ROOT / "novel_engine.db"
    target_db = experiment / "trial.db"
    if source_db.resolve() == target_db.resolve() or not experiment.is_relative_to(ROOT / "real_trial_runs"):
        raise ValueError("Experiment must be an isolated real_trial_runs directory")
    marker = experiment / "experiment.json"
    if not marker.exists():
        if target_db.exists():
            raise ValueError("Refuse an unmarked database")
        with sqlite3.connect(f"file:{source_db}?mode=ro", uri=True) as src, sqlite3.connect(target_db) as dst:
            src.backup(dst)
        write_new(marker, {"kind": "isolated_test", "automated_test_authorized": True,
            "human_approved": False, "production_commit": False, "source_database": str(source_db),
            "source_fingerprint": fingerprint(source_db), "source_run": SOURCE_RUN,
            "source_attempt": SOURCE_ATTEMPT, "model": MODEL, "run_budget": 1_700_000})
        source = ROOT / "studio_runs" / SOURCE_RUN / "chapter_01/attempts" / SOURCE_ATTEMPT
        shutil.copytree(source, experiment / "seed_chapter_01")
    metadata = json.loads(marker.read_text())
    if metadata.get("kind") != "isolated_test" or not metadata.get("automated_test_authorized"):
        raise ValueError("Missing isolation authorization marker")
    # Never echo, serialize, or copy the credential into experiment artifacts.
    secret_text = args.key_file.read_text().strip()
    matches = re.findall(r"sk-[A-Za-z0-9_-]+", secret_text)
    if len(matches) != 1:
        raise ValueError("Key file must contain exactly one DeepSeek key")
    os.environ["NOVEL_ISOLATED_DEEPSEEK_KEY"] = matches[0]
    settings = get_settings().model_copy(deep=True)
    settings.llm.providers = {"deepseek": LLMProviderConfig(base_url="https://api.deepseek.com",
        api_key_env="NOVEL_ISOLATED_DEEPSEEK_KEY", model=MODEL)}
    native_profile = experiment / "native_capabilities.json"
    if native_profile.exists():
        profile = json.loads(native_profile.read_text())
        if profile.get("ok"):
            settings.llm.providers["deepseek"].model_capabilities = profile["capabilities"]
    engine = create_async_engine(f"sqlite+aiosqlite:///{target_db}")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    manager = TestManager(experiment / "studio", maker, settings)
    try:
        capabilities_file = experiment / "capabilities.json"
        if not capabilities_file.exists():
            provider = OpenAICompatibleProvider(base_url="https://api.deepseek.com",
                api_key_env="NOVEL_ISOLATED_DEEPSEEK_KEY", model=MODEL,
                budget=manager.shared_budget(0), trace_path=experiment / "llm_calls.jsonl")
            try:
                client = AgentLLMClient(provider, agent_name="capability_probe", model=MODEL, max_tokens=256)
                answer = await client.generate_text(system="Return only the JSON object {\"ok\":true}.", user="JSON compatibility probe")
                if not json.loads(answer.text).get("ok"):
                    raise ValueError("Capability probe failed")
                write_new(capabilities_file, {"base_url": provider.base_url, "model": MODEL,
                    "capabilities": {}, "metadata": answer.metadata.model_dump(mode="json")})
                log(probe="passed", model=MODEL)
            finally:
                await provider.aclose()
        from app.models import Novel
        async with maker() as session:
            novel = await session.get(Novel, uuid.UUID(NOVEL))
            current = novel.current_chapter_number
        source_run = json.loads((ROOT / "studio_runs" / SOURCE_RUN / "run.json").read_text())
        config = source_run["config"]
        if args.editorial:
            edit = json.loads(args.editorial.read_text())
            marker_path = experiment / f"editorial_applied_{digest(edit)[:12]}.json"
            if not marker_path.exists():
                run = manager.get(edit["run_id"])
                chapter = manager._chapter(run)
                source = manager._package(run, chapter, edit["source_attempt"])
                if current >= chapter.number:
                    raise ValueError("Cannot revise an already committed test chapter")
                replacements = {}
                for scene_id, changes in edit.get("scenes", {}).items():
                    text = next(scene.text for scene in source.draft.scenes if scene.scene_id == scene_id)
                    for old, new in changes:
                        if text.count(old) != 1:
                            raise ValueError("Editorial edit must match one exact occurrence")
                        text = text.replace(old, new)
                    replacements[scene_id] = text
                manager.emit(run, type="test_editorial_selection", previous_attempt=chapter.attempt_id,
                    selected_attempt=edit["source_attempt"], reasons=edit["reasons"])
                chapter.attempt_id = edit["source_attempt"]
                run.status = "blocked"
                if edit.get("memory_changes") or edit.get("memory_additions"):
                    if edit.get("use_saved_proposal_attempt"):
                        failed = manager._attempt_path(run, chapter, edit["use_saved_proposal_attempt"])
                        saved_inputs = json.loads((failed / "manifest.json").read_text())["signature"]["inputs"]
                        batch = MemoryUpdateBatch.model_validate(saved_inputs["batch"])
                    else:
                        batch = source.memory_result.batch.model_copy(deep=True)
                    for change in edit.get("memory_changes", []):
                        operation = next(op for op in batch.operations if op.operation_id == change["operation_id"])
                        payload = getattr(operation, change["payload"])
                        setattr(payload, change["field"], change["value"])
                    batch.operations.extend(MemoryUpdateOperation.model_validate(row) for row in edit.get("memory_additions", []))
                    await manager.new_candidate(run.id, chapter.number, "memory", {
                        "batch": batch.model_dump(mode="json"), "item_declarations": edit.get("item_declarations", [])})
                else:
                    await manager.new_candidate(run.id, chapter.number, "revise", {"replacements": replacements})
                await wait(manager, run)
                write_new(marker_path, {**edit, "new_attempt": chapter.attempt_id, "human_approved": False})
        if current == 0 and "test_seed" not in manager.runs:
            seed_config = RunInput(novel_id=uuid.UUID(NOVEL), mode="live", provider="deepseek",
                model=MODEL, chapter_count=1, max_output_tokens=8192)
            seed_run = StudioRun(id="test_seed", novel_id=NOVEL, title=source_run["title"],
                mode="live", status="awaiting_review", current_chapter=1, total_chapters=1,
                start_chapter=1, config=seed_config,
                chapters=[ChapterProgress(number=1, status="awaiting_review", attempt_id=SOURCE_ATTEMPT)])
            manager._path(seed_run).mkdir()
            manager.runs[seed_run.id] = seed_run
            manager._save(seed_run)
            destination = manager._attempt_path(seed_run, manager._chapter(seed_run))
            shutil.copytree(experiment / "seed_chapter_01", destination)
        first = 1 if current == 0 else current + 1
        for number in range(first, 6):
            # The original Studio seed has no structured cultivation catalog.
            # Add only realm names already specified by the user's chapter goals;
            # never promote a character or fabricate an achieved breakthrough.
            if number == 2 and not (experiment / "cultivation_test_setup.json").exists():
                from app.schemas.story import BootstrapWorldRequest, PowerSystemIn, RealmIn
                from app.services.story_state import StoryStateService
                setup = BootstrapWorldRequest(power_system=PowerSystemIn(name="三命洲纳音修行",
                    description="隔离测试补齐已有背景的结构化境界名，不修改角色境界。",
                    realms=[RealmIn(name="练气", rank=1, sublevels=["初期", "中期", "后期"]),
                            RealmIn(name="筑基", rank=2), RealmIn(name="金丹", rank=3)]))
                async with maker() as session:
                    await StoryStateService(session).bootstrap_world(uuid.UUID(NOVEL), setup)
                    await session.commit()
                write_new(experiment / "cultivation_test_setup.json", {"human_approved": False,
                    "test_setup_only": True, "source": "existing user premise and chapter 4/5 directions",
                    "setup": setup.model_dump(mode="json")})
            saved = [run for run in manager.runs.values() if run.current_chapter == number]
            if saved:
                run = saved[-1]
                chapter = manager._chapter(run)
                if not (manager._attempt_path(run, chapter) / "preview_package.json").exists() and chapter.action == "memory" and chapter.source_attempt:
                    source = manager._package(run, chapter, chapter.source_attempt)
                    inputs = new_declarations(source, chapter.inputs)
                    manager.emit(run, type="test_recovery", failed_attempt=chapter.attempt_id,
                        source_attempt=chapter.source_attempt, message="Reuse saved repair proposal without an API call")
                    chapter.attempt_id = chapter.source_attempt
                    run.status = "blocked"
                    await manager.new_candidate(run.id, number, "memory", inputs)
                    await wait(manager, run)
                if run.status in {"interrupted", "cancelled"} or (
                    run.status == "blocked" and not (manager._attempt_path(run, manager._chapter(run)) / "preview_package.json").exists()):
                    await manager.resume(run.id)
                    await wait(manager, run)
            else:
                run = await manager.create(RunInput(novel_id=uuid.UUID(NOVEL), mode="live",
                    provider="deepseek", model=MODEL, chapter_count=1, target_chars=3000,
                    max_output_tokens=16384, max_calls=50, chapter_tokens=450_000, run_tokens=1_700_000,
                    direction=config["direction"] + "\n可按情节需要引入必要新配角，先在 proposed_entities 声明；不要强迫全部既有配角登场。不得提前完成后续章节目标。所有新增人物使用稳定姓名/称谓和对应临时 EntityRef，不以‘那位筑基’等未登记指代代替场景参与者引用。"
                        + ("\n本章需要的新筑基庇护人可命名陶问山，在本章计划中声明后引入；不得令他无证据获知沈渡私密果位谱。" if number == 5 else ""),
                    chapter_directions=[config["chapter_directions"][number - 1]]))
                await wait(manager, run)
            for cycle in range(3):
                if not (manager._attempt_path(run, manager._chapter(run)) / "preview_package.json").exists():
                    raise RuntimeError(f"Chapter {number} stopped at {run.active_stage}: {run.error}; completed stages retained")
                package = manager._package(run, manager._chapter(run))
                issues = package.memory_result.validation_report.model_dump(mode="json")
                if run.status == "awaiting_review":
                    assessment = await review(manager, run)
                    issues = assessment.model_dump(mode="json")
                    if not assessment.missing and all(r.verdict == "correct" for r in assessment.operation_reviews) and min(assessment.quality_scores.model_dump().values()) >= 3:
                        destination = manager._attempt_path(run, manager._chapter(run))
                        await test_commit(maker, package, manager._chapter(run).attempt_id, assessment, destination)
                        run.status = manager._chapter(run).status = "completed"
                        manager._save(run)
                        write_new(experiment / f"chapter_{number:02d}.txt", package.draft.full_text, text=True)
                        break
                if cycle == 2:
                    raise RuntimeError(f"Chapter {number}: bounded semantic repair exhausted; candidate preserved")
                await repair(manager, run, issues)
        texts = [f"第 {n} 章\n\n" + (experiment / f"chapter_{n:02d}.txt").read_text() for n in range(2, 6)]
        if not (experiment / "chapters_02_05.txt").exists():
            write_new(experiment / "chapters_02_05.txt", "\n\n".join(texts), text=True)
        write_new(experiment / "completion.json", {"human_approved": False, "production_commit": False,
            "chapters": [2, 3, 4, 5], "budget": manager.shared_budget(5).summary(),
            "source_unchanged": fingerprint(source_db) == metadata["source_fingerprint"]})
        log(completed=True, directory=str(experiment))
    finally:
        await manager.close()
        await engine.dispose()
        log(source_unchanged=fingerprint(source_db) == metadata["source_fingerprint"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", type=Path, required=True)
    parser.add_argument("--key-file", type=Path, default=Path("/home/sty/Desktop/ds_key.txt"))
    parser.add_argument("--editorial", type=Path, help="Exact, reviewed scene edits in this isolated experiment")
    asyncio.run(main(parser.parse_args()))
