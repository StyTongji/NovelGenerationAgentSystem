import asyncio
import json
from pathlib import Path
import sys

import pytest
import yaml
from pydantic import BaseModel

from app.services.trial_artifacts import TrialAttempt, safe_name


class Result(BaseModel):
    value: int


async def test_resume_reuses_complete_stages_and_retries_failed_stage(tmp_path):
    attempt = TrialAttempt(tmp_path / "a", {"state": "before", "prompt": "v3"})
    calls = []
    async def first():
        calls.append("first")
        return Result(value=1)
    async def fail():
        calls.append("failed")
        raise RuntimeError("failure")
    await attempt.stage("first", Result, first)
    with pytest.raises(RuntimeError):
        await attempt.stage("second", Result, fail)
    resumed = TrialAttempt(attempt.path, attempt.signature, resume=True)
    assert (await resumed.stage("first", Result, first)).value == 1
    assert calls == ["first", "failed"]
    async def second():
        calls.append("second")
        return Result(value=2)
    await resumed.stage("second", Result, second)
    assert calls == ["first", "failed", "second"]
    with pytest.raises(ValueError):
        TrialAttempt(attempt.path, {"state": "after", "prompt": "v3"}, resume=True)
    path = attempt.path / "stage_first.json"
    saved = json.loads(path.read_text())
    saved["value"]["value"] = 123
    path.write_text(json.dumps(saved))
    with pytest.raises(ValueError):
        await resumed.stage("first", Result, first)


def test_names_cannot_escape_trial_root():
    for name in ("../x", "/tmp/x", "a/b", "..", ""):
        with pytest.raises(ValueError):
            safe_name(name)


def test_package_publication_recovers_interrupted_auxiliary_save(tmp_path, monkeypatch):
    from app.commit.models import ChapterPreviewPackage
    import app.services.trial_artifacts as artifacts
    package = ChapterPreviewPackage.model_validate_json(
        (Path(__file__).parent / "fixtures/real_memory_closure/chapter_01/preview_package.json").read_text())
    attempt = TrialAttempt(tmp_path / "a", {"state": "before"})
    original = artifacts.write_new
    def interrupted(path, value, **kwargs):
        if path.name == "dry_run.json":
            raise OSError("simulated interrupted save")
        original(path, value, **kwargs)
    monkeypatch.setattr(artifacts, "write_new", interrupted)
    with pytest.raises(OSError):
        attempt.save_package(package)
    assert not (attempt.path / "preview_package.json").exists()
    monkeypatch.setattr(artifacts, "write_new", original)
    attempt.save_package(package)
    assert (attempt.path / "chapter.txt").read_text() == package.draft.full_text
    assert (attempt.path / "review.json").exists()
    attempt.save_package(package)


def test_example_config_loads_and_environment_takes_precedence(monkeypatch):
    from app import config
    overlay = yaml.safe_load((Path(__file__).parent.parent / "config.example.yaml").read_text())
    assert set(overlay) <= set(config.Settings.model_fields)
    monkeypatch.setattr(config, "_load_yaml_overlay", lambda: overlay)
    monkeypatch.setenv("NOVEL_GENERATION_CHAPTER_TARGET_CHARS", "4200")
    monkeypatch.setenv("NOVEL_LLM__PROVIDERS__SCNET__DEADLINE_SECONDS", "90")
    config.get_settings.cache_clear()
    try:
        settings = config.get_settings()
        assert settings.generation_chapter_target_chars == 4200
        assert settings.llm.providers["scnet"].deadline_seconds == 90
        assert settings.llm.providers["scnet"].connect_timeout_seconds == 10
    finally:
        config.get_settings.cache_clear()


async def test_real_sse_disconnect_kills_and_reaps_stubborn_child(monkeypatch):
    from app.api.routes import trial_ui
    original = asyncio.create_subprocess_exec
    children = []
    async def spawn(*args, **kwargs):
        child = await original(sys.executable, "-u", "-c",
            "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print('ready',flush=True); time.sleep(60)",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        children.append(child)
        return child
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(trial_ui, "CHILD_STOP_TIMEOUT", .05)
    response = await trial_ui.start_trial_preview(trial_ui.TrialPreviewRequest(
        base_url="https://example.test", api_key="fake", model="fake"))
    disconnect = asyncio.Event()
    async def send(message):
        if b"ready" in message.get("body", b""):
            disconnect.set()
    async def receive():
        await disconnect.wait()
        return {"type": "http.disconnect"}
    try:
        await asyncio.wait_for(response({"type": "http", "asgi": {"spec_version": "2.3"}}, receive, send), 3)
        assert len(children) == 1  # preview command never starts after cancelled init
        assert children[0].returncode == -9
    finally:
        for child in children:
            if child.returncode is None:
                child.kill()
            await child.wait()


def test_comparison_includes_failed_attempts_and_unknown_usage(tmp_path):
    from scripts.trial_comparison import compare
    attempt = TrialAttempt(tmp_path / "chapter_01/attempts/auto_1", {
        "kind": "memory", "memory_model": "Auto", "source_hash": "same", "state_hash": "before"})
    from app.services.trial_artifacts import write_new
    write_new(attempt.path / "failure_1.json", {"error_type": "ProviderTimeoutError"})
    write_new(tmp_path / "usage_ledger.json", {"reservations": {
        "a": {"chapter": "1", "usage_known": False, "charged_tokens": 1000}}})
    result = compare(tmp_path, 1, ["Auto", "Flash"])
    assert result["attempts"][0]["status"] == "failed"
    assert result["samples"]["Auto"]["completed"] == 0
    assert result["samples"]["Flash"]["missing_planned_attempts"] == 3
    assert result["budget"]["known_tokens"] == 0
    assert result["budget"]["reserved_unknown_tokens"] == 1000
