"""Author edit -> fresh v3 evidence/memory -> new, unapproved candidate."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agents.memory_extractor import MemoryExtractorAgent
from app.commit.models import CommitError, StaleStoryStateError
from app.continuity.models import ContinuityReport
from app.llm.client import AgentLLMClient
from app.llm.fake import FakeLLMProvider
from app.llm.prompts import get_default_registry
from app.services.author_review import export_author_review
from app.services.canonical_commit import CanonicalCommitService
from app.services.chapter_generation import ChapterGenerationService, ContinuityRepairExhaustedError
from app.services.continuity import ContinuityService
from app.services.memory_extraction import MemoryExtractionService
from app.services.trial_artifacts import TrialAttempt
from tests.e2e.test_preview_pipeline import db_fingerprint
from tests.test_real_closure_hardening import valid_package, approval


class Forbidden:
    def __getattr__(self, name):
        raise AssertionError(f"Revision must not call {name}")


def revision_service(session, package):
    provider = FakeLLMProvider()
    wire = package.memory_result.batch.model_dump(mode="json", exclude_none=True)
    for op in wire["operations"]:
        op.pop("evidence", None)
    provider.enqueue_structured(wire)
    service = ChapterGenerationService(
        session, Forbidden(), Forbidden(),
        ContinuityService(session, mode="deterministic_only", repair_agent=Forbidden()),
        MemoryExtractionService(session, MemoryExtractorAgent(
            AgentLLMClient(provider, agent_name="memory_extractor"), get_default_registry(),
        ), require_evidence_refs=True),
        CanonicalCommitService(session),
    )
    return service, provider


async def test_revision_rebuilds_catalog_and_memory_without_rewriting_other_scenes(session, tmp_path):
    package = await valid_package(session)
    original = package.model_dump_json()
    before = await db_fingerprint(session)
    service, provider = revision_service(session, package)
    scene = package.draft.scenes[-1]
    attempt = TrialAttempt(tmp_path / "revision", {"source": package.preview_package_hash})
    revised = await service.revise_preview(
        package, {scene.scene_id: scene.text + "\n夜风掠过窗纸，屋里安静下来。"}, checkpoints=attempt,
    )
    assert package.model_dump_json() == original
    assert revised.draft.scenes[:-1] == package.draft.scenes[:-1]
    assert revised.plan_result == package.plan_result
    assert revised.draft.scenes[-1].generation_metadata is None
    assert revised.draft.full_text == "\n\n".join(s.text for s in revised.draft.scenes)
    assert revised.memory_result.validation_report.valid
    assert revised.memory_result.dry_run.applicable
    assert revised.memory_result.draft_hash != package.memory_result.draft_hash
    assert revised.continuity_report.draft_hash == revised.memory_result.evidence_catalog.draft_hash
    assert revised.preview_package_hash != package.preview_package_hash
    assert len(provider.calls) == 1
    assert await db_fingerprint(session) == before
    attempt.save_package(revised)
    assert not json.loads((attempt.path / "review.json").read_text())["approved"]
    assert (attempt.path / "reading/review.md").exists()
    resumed = TrialAttempt(attempt.path, attempt.signature, resume=True)
    again = await service.revise_preview(
        package, {scene.scene_id: scene.text + "\n夜风掠过窗纸，屋里安静下来。"}, checkpoints=resumed,
    )
    assert again.memory_result == revised.memory_result
    assert len(provider.calls) == 1
    with pytest.raises(CommitError):
        await service.finalize(revised, attempt_id="test-only", approval=approval(package))
    assert await db_fingerprint(session) == before


async def test_blocked_revision_stops_before_extraction_and_keeps_report(session, tmp_path):
    package = await valid_package(session)
    service, provider = revision_service(session, package)
    service.continuity_service.check_draft = AsyncMock(return_value=ContinuityReport(valid=False))
    attempt = TrialAttempt(tmp_path / "blocked", {"source": package.preview_package_hash})
    with pytest.raises(ContinuityRepairExhaustedError, match="No automatic rewrite"):
        await service.revise_preview(package, {package.draft.scenes[0].scene_id: "作者修订。"}, checkpoints=attempt)
    assert provider.calls == []
    assert (attempt.path / "stage_draft.json").exists()
    assert (attempt.path / "stage_continuity.json").exists()
    assert not (attempt.path / "stage_memory.json").exists()


async def test_revision_rejects_unknown_scene_and_stale_source_before_calls(session, monkeypatch):
    package = await valid_package(session)
    service, provider = revision_service(session, package)
    with pytest.raises(ValueError, match="existing scene"):
        await service.revise_preview(package, {"missing": "正文。"})
    monkeypatch.setattr("app.planning.provenance.compute_story_state_hash", AsyncMock(return_value="new-state"))
    with pytest.raises(StaleStoryStateError):
        await service.revise_preview(package, {package.draft.scenes[0].scene_id: "正文。"})
    assert not provider.calls


async def test_reading_export_keeps_author_edits_and_shows_operations(session, tmp_path):
    package = await valid_package(session)
    report = export_author_review(package, tmp_path)
    content = report.read_text()
    assert "story_fact_create" in content and "林凡已经抵达天剑城" in content
    assert package.memory_result.batch.operations[0].evidence[0].exact_quote in content
    index = json.loads((tmp_path / "scenes/index.json").read_text())
    assert index[0]["scene_id"] == package.draft.scenes[0].scene_id
    copy = tmp_path / "scenes" / index[0]["file"]
    copy.write_text("作者正在编辑")
    export_author_review(package, tmp_path)
    assert copy.read_text() == "作者正在编辑"


async def test_revision_cli_and_resume_keep_source_and_freeze_edit_input(session, tmp_path, monkeypatch):
    from scripts import real_llm_trial as trial
    package = await valid_package(session)
    run = tmp_path / "book"
    source = TrialAttempt(run / "chapter_01/attempts/original", {"source": "fixture"})
    source.save_package(package)
    original_bytes = (source.path / "preview_package.json").read_bytes()
    (run / "state.json").write_text(json.dumps({
        "novel_id": package.novel_id, "provider": "fake", "model": "fake", "next_chapter": 1,
        "story_state_hash": package.story_state_hash,
    }))
    scene = package.draft.scenes[-1]
    edit = tmp_path / "edit.txt"
    edit.write_text(scene.text + "\n夜风掠过窗纸。")
    service, provider = revision_service(session, package)
    monkeypatch.setattr(trial, "RUNS_ROOT", tmp_path)
    monkeypatch.setattr(trial, "_configure", lambda *a, **kw: None)
    monkeypatch.setattr(trial, "_close_services", AsyncMock())
    monkeypatch.setattr(trial, "_build_services", lambda *a, **kw: (session, service, None, None))
    args = SimpleNamespace(run="book", chapter=1, source_attempt="original", attempt="edited",
                           scene_id=scene.scene_id, text_file=str(edit))
    await trial.cmd_revise(args)
    edit.write_text("继续改的文字不应影响已保存的尝试")
    await trial.cmd_resume(SimpleNamespace(run="book", chapter=1, attempt="edited"))
    assert len(provider.calls) == 1
    assert (source.path / "preview_package.json").read_bytes() == original_bytes
    saved = json.loads((run / "chapter_01/attempts/edited/manifest.json").read_text())
    assert saved["signature"]["replacements"][scene.scene_id] == scene.text + "\n夜风掠过窗纸。"


@pytest.mark.parametrize("unknown_evidence", [False, True])
async def test_memory_edit_cli_is_offline_and_preserves_draft_and_database(session, tmp_path, monkeypatch, unknown_evidence):
    import sqlite3
    from scripts import real_llm_trial as trial
    package = await valid_package(session)
    run = tmp_path / "book"
    source = TrialAttempt(run / "chapter_01/attempts/source", {"source": "fixture"})
    source.save_package(package)
    (run / "state.json").write_text(json.dumps({
        "novel_id": package.novel_id, "provider": "real", "model": "not-called", "next_chapter": 1,
        "story_state_hash": package.story_state_hash,
    }))
    # Copy the fixture schema from the isolated test engine, never the real trial DB.
    from sqlalchemy.ext.asyncio import create_async_engine
    from app.models import Base
    engine = create_async_engine(f"sqlite+aiosqlite:///{run / 'trial.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await engine.dispose()
    sql = (Path(__file__).parent / "fixtures/real_memory_closure/chapter_01/before_state.sql").read_text()
    with sqlite3.connect(run / "trial.db") as db:
        db.executescript(sql)
        before = list(db.iterdump())
    batch_path = source.path / "reading/memory_batch.json"
    wire = json.loads(batch_path.read_text())
    if unknown_evidence:
        wire["operations"][0]["evidence_refs"] = [{"evidence_id": "unknown"}]
        batch_path.write_text(json.dumps(wire))
    monkeypatch.setattr(trial, "RUNS_ROOT", tmp_path)
    monkeypatch.delenv("NOVEL_LLM_API_KEY", raising=False)
    monkeypatch.setattr(trial, "_build_services", lambda *a, **kw: pytest.fail("Must not construct LLM services"))
    args = SimpleNamespace(run="book", chapter=1, source_attempt="source", attempt="corrected", batch_file=str(batch_path))
    await trial.cmd_memory_edit(args)
    edited_path = run / "chapter_01/attempts/corrected"
    result = json.loads((edited_path / "preview_package.json").read_text())
    assert result["draft"] == package.draft.model_dump(mode="json")
    assert result["memory_result"]["validation_report"]["valid"] is (not unknown_evidence)
    assert not json.loads((edited_path / "review.json").read_text())["approved"]
    await trial.cmd_resume(SimpleNamespace(run="book", chapter=1, attempt="corrected"))
    with sqlite3.connect(run / "trial.db") as db:
        assert list(db.iterdump()) == before
