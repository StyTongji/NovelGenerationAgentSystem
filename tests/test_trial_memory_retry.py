"""CLI contract: memory-retry touches only the memory stage."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_memory_retry_does_not_call_planner_or_writer(tmp_path, monkeypatch):
    import scripts.real_llm_trial as trial

    source = Path(__file__).parent / "fixtures/real_memory_closure/chapter_01"
    run_dir = tmp_path / "retry-run"
    chapter = run_dir / "chapter_01"
    chapter.mkdir(parents=True)
    original = (source / "preview_package.json").read_bytes()
    (chapter / "preview_package.json").write_bytes(original)
    raw = json.loads(original)
    state = {"provider": "fake", "model": "fake", "novel_id": raw["novel_id"],
             "story_state_hash": raw["story_state_hash"], "next_chapter": 1}
    (run_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setattr(trial, "RUNS_ROOT", tmp_path)
    async def current_hash(_session, _novel_id):
        return state["story_state_hash"]
    monkeypatch.setattr(
        "app.planning.provenance.compute_story_state_hash", current_hash,
    )

    class StubMemoryService:
        calls = 0

        async def extract_preview(self, *args, **kwargs):
            self.calls += 1
            from app.commit.models import ChapterPreviewPackage

            package = ChapterPreviewPackage.model_validate_json(
                (run_dir / "chapter_01" / "preview_package.json").read_text(encoding="utf-8")
            )
            return package.memory_result

    memory = StubMemoryService()

    class StubSession:
        async def close(self):
            pass

    class StubEngine:
        async def dispose(self):
            pass

    class StubGeneration:
        memory_service = memory

    monkeypatch.setattr(
        trial, "_build_services",
        lambda *args, **kwargs: (StubSession(), StubGeneration(), None, StubEngine()),
    )
    args = SimpleNamespace(run=run_dir.name, chapter=1, attempt="retry1")
    await trial.cmd_memory_retry(args)
    assert memory.calls == 1
    assert (chapter / "preview_package.json").read_bytes() == original
    assert (chapter / "attempts/retry1/memory_preview.json").exists()
    assert (chapter / "attempts/retry1/validation_report.json").exists()
    assert json.loads((chapter / "attempts/retry1/dry_run.json").read_text()) is None
    args.resume = True
    await trial.cmd_memory_retry(args)
    assert memory.calls == 1
    state["story_state_hash"] = "changed"
    args.resume = False
    args.attempt = "stale"
    with pytest.raises(ValueError):
        await trial.cmd_memory_retry(args)
    assert memory.calls == 1
