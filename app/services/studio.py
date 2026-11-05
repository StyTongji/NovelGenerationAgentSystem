"""Local authoring jobs: durable artifacts, one chapter at a time, explicit review.

The task owns its DB session; HTTP/SSE connections never own that session.
LangGraph owns chapter routing, TrialAttempt owns stage persistence, and this
service owns the human review boundary between consecutive chapters.
"""
from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

from pydantic import SecretStr

from sqlalchemy import select

from app.commit.models import ChapterPreviewPackage, ReviewApproval
from app.config import AgentModelConfig, LLMProviderConfig, Settings
from app.memory.models import MemoryUpdateBatch
from app.llm.credentials import read_api_key
from app.models import Novel
from app.planning.provenance import compute_story_state_hash
from app.schemas.studio import ApprovalInput, BudgetLimits, ChapterProgress, RunInput, StudioRun
from app.services.trial_artifacts import TrialAttempt, digest, safe_name, write_new


def _atomic_json(path: Path, value):
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


class ObservedAttempt:
    def __init__(self, attempt, emit):
        self.attempt, self.emit = attempt, emit

    async def stage(self, name, model, produce):
        cached = (self.attempt.path / f"stage_{name}.json").exists()
        self.emit(type="stage", stage=name, status="cached" if cached else "running",
                  message=f"{'恢复已完成阶段' if cached else '开始'} · {name}")
        result = await self.attempt.stage(name, model, produce)
        value = result.model_dump(mode="json")
        # Structured output and prose are reviewable; no private chain of thought.
        self.emit(type="stage", stage=name, status="completed", message=f"完成 · {name}",
                  output=value)
        return result


class StudioManager:
    def __init__(self, root: Path, session_factory, settings: Settings, service_factory=None):
        self.root, self.session_factory, self.settings = root, session_factory, settings
        self.service_factory = service_factory
        self.review_agent_factory = None
        self.root.mkdir(parents=True, exist_ok=True)
        self._owner = (root / ".owner.lock").open("a")
        try:
            fcntl.flock(self._owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._owner.close()
            raise RuntimeError("Studio uses one worker. Start uvicorn with --workers 1.") from None
        self.tasks: dict[str, asyncio.Task] = {}
        self._custom_keys: dict[str, SecretStr] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.subscribers: dict[str, int] = {}
        self._closing = False
        self.runs: dict[str, StudioRun] = {}
        for path in sorted(root.glob("*/run.json")):
            run = StudioRun.model_validate_json(path.read_text())
            if run.config.provider == "custom_input":
                run.requires_api_key = True
            if run.status == "running":
                run.status = "interrupted"
                run.error = "服务已重启，可从已保存阶段恢复。"
                _atomic_json(path, run.model_dump(mode="json"))
            self.runs[run.id] = run

    def get(self, run_id: str) -> StudioRun:
        return self.runs[safe_name(run_id)]

    def _path(self, run):
        return self.root / run.id

    def _save(self, run):
        run.updated_at = datetime.now(UTC)
        _atomic_json(self._path(run) / "run.json", run.model_dump(mode="json"))

    def budget_summary(self, run_id):
        from app.llm.budget import TrialBudget
        run = self.get(run_id)
        return TrialBudget(self._path(run) / "usage_ledger.json", run.current_chapter,
                           **BudgetLimits.model_validate(run.config.model_dump()).model_dump()).summary()

    async def update_budget(self, run_id, limits: BudgetLimits):
        run = self.get(run_id)
        async with self.locks.setdefault(run.novel_id, asyncio.Lock()):
            if run.status in {"running", "completed", "archived"}:
                raise ValueError("请先暂停正在运行的任务；已结束的记录无需调整预算。")
            summary = self.budget_summary(run_id)
            if (limits.max_calls < summary["chapter"]["calls"]
                or limits.chapter_tokens < summary["chapter"]["charged_tokens"]
                or limits.run_tokens < summary["run"]["charged_tokens"]):
                raise ValueError("新上限不能低于已经计入的请求次数或 token 用量。")
            before = BudgetLimits.model_validate(run.config.model_dump()).model_dump()
            run.config = RunInput.model_validate({**run.config.model_dump(), **limits.model_dump()})
            self._save(run)
            self.emit(run, type="budget", status="updated", message="预算上限已更新；历史用量与已完成阶段保留，点击恢复后继续。",
                      before=before, limits=limits.model_dump())
            return run

    def emit(self, run, **event):
        path = self._path(run) / "events.jsonl"
        # The local single-worker owner serializes event writes in the event loop.
        sequence = self._sequences.get(run.id, 0) + 1
        self._sequences[run.id] = sequence
        event = {"sequence": sequence, "at": datetime.now(UTC).isoformat(),
                 "chapter_number": run.current_chapter, **event}
        with path.open("a", encoding="utf-8") as output:
            output.write(json.dumps(event, ensure_ascii=False) + "\n")
        if event.get("stage") and event.get("status") == "running":
            run.active_stage = event["stage"]
            self._save(run)

    @property
    def _sequences(self):
        if not hasattr(self, "_event_sequences"):
            self._event_sequences = {}
            for run in self.runs.values():
                events = self.events(run.id)
                self._event_sequences[run.id] = events[-1]["sequence"] if events else 0
        return self._event_sequences

    def events(self, run_id, after=0):
        path = self._path(self.get(run_id)) / "events.jsonl"
        if not path.exists():
            return []
        return [event for line in path.read_text().splitlines()
                if (event := json.loads(line))["sequence"] > after]

    def providers(self):
        return [{"id": name, "label": cfg.display_name or name, "configured": bool(read_api_key(cfg.api_key_env)),
                 "base_url": cfg.base_url,
                 "is_default": name == self.settings.llm.default_provider,
                 "default_model": cfg.model, "key_env": cfg.api_key_env,
                 "models": list(dict.fromkeys(filter(None, [cfg.model, *cfg.models])))}
                for name, cfg in self.settings.llm.providers.items()] + [{
                    "id": "custom_input", "label": "自定义（API 地址 / Key / 模型）",
                    "configured": True, "input_required": True, "is_default": False,
                    "base_url": "", "default_model": "", "key_env": "", "models": [],
                }]

    def _assert_available(self, novel_id, except_run=None):
        if any(r.novel_id == str(novel_id) and r.id != except_run
               and r.status not in {"completed", "cancelled", "archived"} for r in self.runs.values()):
            raise ValueError("这本小说已有未结束的记录。可点击“结束此记录”，或在新建时选择“结束旧记录并新建”。")

    def _validate_provider(self, config, api_key: SecretStr | None = None):
        if config.mode == "demo":
            return
        if config.provider == "custom_input":
            if not config.custom_base_url or not config.model.strip():
                raise ValueError("请填写自定义 API 地址和模型名称。")
            if api_key is None or not api_key.get_secret_value().strip():
                raise ValueError("请重新输入此任务的 API Key；自定义密钥仅在服务运行期间保留。")
            return
        cfg = self.settings.llm.providers.get(config.provider)
        if cfg is None or not config.model.strip():
            raise ValueError("请选择已配置的 API 服务和模型。")
        if not read_api_key(cfg.api_key_env):
            raise ValueError(f"服务端环境变量 {cfg.api_key_env} 尚未设置。")

    async def create(self, config: RunInput, *, api_key: SecretStr | None = None, replace_existing: bool = False):
        self._validate_provider(config, api_key)
        # Serialized start also prevents two HTTP requests creating jobs for one novel.
        async with self.locks.setdefault(str(config.novel_id), asyncio.Lock()):
            async with self.session_factory() as session:
                novel = await session.get(Novel, config.novel_id)
                if novel is None:
                    raise ValueError("小说不存在，请先创建世界与角色。")
                start = novel.current_chapter_number + 1
                run = StudioRun(id=uuid.uuid4().hex, novel_id=str(novel.id), title=novel.title,
                    mode=config.mode, status="running", current_chapter=start,
                    start_chapter=start, total_chapters=config.chapter_count, config=config,
                    chapters=[ChapterProgress(number=n) for n in range(start, start + config.chapter_count)])
            if replace_existing:
                for previous in list(self.runs.values()):
                    if previous.novel_id == str(config.novel_id) and previous.status not in {"completed", "cancelled", "archived"}:
                        await self._finish_locked(previous)
            self._assert_available(config.novel_id)
            self._path(run).mkdir()
            self.runs[run.id] = run
            if config.mode == "live" and config.provider == "custom_input":
                self._custom_keys[run.id] = api_key
            self._save(run)
            self.emit(run, type="status", status="running", message="任务已创建；每章审阅提交后继续下一章。")
            self._launch(run)
            return run

    def _launch(self, run):
        if self._closing:
            raise ValueError("服务正在关闭。")
        if run.id in self.tasks and not self.tasks[run.id].done():
            raise ValueError("任务正在运行。")
        run.status, run.error = "running", None
        self._save(run)
        self.tasks[run.id] = asyncio.create_task(self._execute(run), name=f"studio-{run.id}")

    def _chapter(self, run, number=None):
        chapter = next((c for c in run.chapters if c.number == (number or run.current_chapter)), None)
        if chapter is None:
            raise KeyError("Chapter is not in this run")
        return chapter

    def _attempt_path(self, run, chapter, attempt_id=None):
        return self._path(run) / f"chapter_{chapter.number:02d}" / "attempts" / safe_name(attempt_id or chapter.attempt_id)

    def chapter_detail(self, run_id, number):
        run = self.get(run_id)
        chapter = self._chapter(run, number)
        result = {"attempt_id": chapter.attempt_id, "package": None, "quality": None,
                  "approval": None, "commit": None, "assistance": None, "partial": {}}
        if not chapter.attempt_id:
            return result
        path = self._attempt_path(run, chapter)
        for key, file in {"package": "preview_package.json", "quality": "quality.json",
                          "approval": "approval.json", "commit": "commit.json"}.items():
            if (path / file).exists():
                result[key] = json.loads((path / file).read_text())
        if result["package"] is None:
            for stage in path.glob("stage_*.json"):
                result["partial"][stage.stem.removeprefix("stage_")] = json.loads(stage.read_text())["value"]
        if chapter.assistance_file and (path / chapter.assistance_file).exists():
            result["assistance"] = json.loads((path / chapter.assistance_file).read_text())
        return result

    def _package(self, run, chapter, attempt_id=None):
        return ChapterPreviewPackage.model_validate_json(
            (self._attempt_path(run, chapter, attempt_id) / "preview_package.json").read_text())

    def _partial_source(self, run, chapter):
        """Recover a failed continuity candidate for author-directed scene edits."""
        from app.draft.models import ChapterDraft
        from app.planning.models import PlanGenerationResult
        path = self._attempt_path(run, chapter, chapter.source_attempt)
        manifest = json.loads((path / "manifest.json").read_text())
        signature = manifest["signature"]
        if digest(signature) != manifest["signature_hash"]:
            raise ValueError("Source manifest integrity failure")
        def read(name):
            stored = json.loads((path / f"stage_{name}.json").read_text())
            if stored["signature_hash"] != manifest["signature_hash"] or digest(stored["value"]) != stored["hash"]:
                raise ValueError("Source checkpoint integrity failure")
            return stored["value"]
        draft_value = read("repair")["draft"] if (path / "stage_repair.json").exists() else read("draft")
        # A revision attempt may have inherited its plan from an earlier attempt.
        planning_path = path / "stage_director.json"
        if planning_path.exists():
            planning = PlanGenerationResult.model_validate(read("director"))
        else:
            planning = PlanGenerationResult.model_validate(read("revision_plan"))
        return planning, ChapterDraft.model_validate(draft_value), signature["story_state_hash"]

    def _effective_settings(self, run):
        settings = self.settings.model_copy(deep=True)
        settings.continuity_mode = "hybrid"
        settings.generation_chapter_target_chars = run.config.target_chars
        settings.llm.default_provider = run.config.provider
        if run.config.provider == "custom_input":
            settings.llm.providers["custom_input"] = LLMProviderConfig(
                base_url=run.config.custom_base_url, model=run.config.model,
                api_key_env="NOVEL_STUDIO_CUSTOM_KEY", display_name="自定义服务",
            )
        for role in ("director", "scene_planner", "writer", "writer_repair", "continuity_checker",
                     "memory_extractor", "literary_reviewer"):
            old = settings.llm.agents.get(role)
            settings.llm.agents[role] = AgentModelConfig(
                provider=run.config.provider, model=run.config.role_models.get(role) or run.config.model,
                temperature=old.temperature if old else (0.85 if role in {"writer", "writer_repair"} else 0.2),
                max_tokens=run.config.max_output_tokens or (old.max_tokens if old else (8192 if role in {"memory_extractor", "director"} else 4096)),
            )
        return settings

    async def _services(self, session, run, chapter):
        if self.service_factory:
            return await self.service_factory(session, run, chapter), None, []
        if run.mode == "demo":
            from app.services.studio_demo import build_demo_services
            return await build_demo_services(session, uuid.UUID(run.novel_id), chapter.number,
                                             target_chars=run.config.target_chars), None, []
        from app.agents.literary_reviewer import LiteraryReviewerAgent
        from app.api.deps import build_chapter_generation_service
        from app.llm.prompts import get_default_registry
        registry, settings, providers = self._runtime(run, chapter)
        reviewer = LiteraryReviewerAgent(llm=registry.client_for_agent(
            "literary_reviewer", settings, prompt_version="literary_reviewer_v1"),
            prompt_registry=get_default_registry())
        return build_chapter_generation_service(session, registry, settings), reviewer, providers

    def _runtime(self, run, chapter):
        from app.llm.budget import TrialBudget
        from app.llm.registry import build_registry
        key = self._custom_keys.get(run.id)
        self._validate_provider(run.config, key)
        settings = self._effective_settings(run)
        registry = build_registry(settings, api_keys={"custom_input": key} if key is not None else None)
        providers = [registry.get(name) for name in registry.names()]
        for name in registry.names():
            provider = registry.get(name)
            provider.budget = TrialBudget(self._path(run) / "usage_ledger.json", chapter.number,
                max_calls=run.config.max_calls, chapter_tokens=run.config.chapter_tokens,
                run_tokens=run.config.run_tokens)
            provider.trace_path = self._path(run) / "llm_calls.jsonl"
            provider.model_capabilities = settings.llm.providers[name].model_capabilities
            if name == run.config.provider and run.config.runtime_capabilities:
                provider.model_capabilities = {**provider.model_capabilities, **run.config.runtime_capabilities}
            provider.event_callback = lambda event: self.emit(run, type="llm", **event)
        return registry, settings, providers

    def _error_text(self, run, exc):
        from app.llm.errors import scrub_secrets
        message = str(exc)
        for cfg in self.settings.llm.providers.values():
            message = scrub_secrets(message, read_api_key(cfg.api_key_env))
        key = self._custom_keys.get(run.id)
        return scrub_secrets(message, key.get_secret_value() if key is not None else None)

    async def _execute(self, run):
        chapter = self._chapter(run)
        providers = []
        try:
            async with self.session_factory() as session:
                novel_id = uuid.UUID(run.novel_id)
                state_hash = await compute_story_state_hash(session, novel_id)
                novel = await session.get(Novel, novel_id)
                prompt_root = Path(__file__).resolve().parents[2] / "prompts"
                signature = {"version": 1, "story_state_hash": state_hash,
                    "story_profile": {"title": novel.title, "premise": novel.premise, "genre": novel.genre,
                                      "style_profile": novel.style_profile},
                    "config": run.config.model_dump(mode="json", exclude_none=True), "chapter": chapter.number,
                    "action": chapter.action, "source": chapter.source_attempt, "inputs": chapter.inputs,
                    "prompts": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(prompt_root.glob("*.md")) if not p.name.startswith(("author_review_", "market_research_", "story_architect_"))},
                    "llm_config": self._effective_settings(run).llm.model_dump(mode="json") if run.mode == "live" else {}}
                if not chapter.attempt_id:
                    chapter.attempt_id = uuid.uuid4().hex[:12]
                path = self._attempt_path(run, chapter)
                if (path / "manifest.json").exists():
                    # Spending limits control future requests, not generated content.
                    # Keep the original signed limits for checkpoint compatibility;
                    # the runtime always charges against the current run limits.
                    original = json.loads((path / "manifest.json").read_text())["signature"]["config"]
                    for key in BudgetLimits.model_fields:
                        if key in original:
                            signature["config"][key] = original[key]
                        else:
                            signature["config"].pop(key, None)
                attempt = TrialAttempt(path, signature, resume=(path / "manifest.json").exists())
                stages = ObservedAttempt(attempt, lambda **e: self.emit(run, **e))
                chapter.status, chapter.error = "running", None
                self._save(run)
                if (path / "preview_package.json").exists():
                    package = self._package(run, chapter)
                    service = reviewer = None
                    # Resume a failed literary review without rerunning generation.
                    if run.mode == "live" and not (path / "quality.json").exists():
                        service, reviewer, providers = await self._services(session, run, chapter)
                elif chapter.action == "memory":
                    from app.services.memory_extraction import MemoryExtractionService, MemoryExtractionResult
                    source = self._package(run, chapter, chapter.source_attempt)
                    self._check_source(source, state_hash)
                    if chapter.inputs.get("item_declarations"):
                        from app.services.author_review import declare_draft_items
                        source = source.model_copy(deep=True)
                        source.draft = declare_draft_items(source.draft, chapter.inputs["item_declarations"])
                        async def declared_draft():
                            return source.draft
                        await stages.stage("draft", type(source.draft), declared_draft)
                    memory = MemoryExtractionService(session, require_evidence_refs=True)
                    result = await stages.stage("memory", MemoryExtractionResult, lambda: memory.validate_batch(
                        novel_id, source.draft, source.plan_result.plan,
                        MemoryUpdateBatch.model_validate(chapter.inputs["batch"]), dry_run=True))
                    package = self._with_memory(source, result)
                    service = reviewer = None
                else:
                    service, reviewer, providers = await self._services(session, run, chapter)
                    if chapter.action == "recheck":
                        source_path = self._attempt_path(run, chapter, chapter.source_attempt) / "preview_package.json"
                        if source_path.exists():
                            source = self._package(run, chapter, chapter.source_attempt)
                            self._check_source(source, state_hash)
                            planning, draft, before_hash = source.plan_result, source.draft, source.story_state_hash
                        else:
                            planning, draft, before_hash = self._partial_source(run, chapter)
                        if before_hash != state_hash:
                            raise ValueError("世界状态已变化，不能复用该候选。")
                        async def keep_plan():
                            return planning
                        async def keep_draft():
                            return draft
                        await stages.stage("revision_plan", type(planning), keep_plan)
                        draft = await stages.stage("draft", type(draft), keep_draft)
                        offset = chapter.number - run.start_chapter
                        specific = run.config.chapter_directions[offset] if offset < len(run.config.chapter_directions) else ""
                        direction = "\n".join(filter(None, [run.config.direction, specific])) or None
                        package = await service._finish_preview(novel_id, direction, planning, draft,
                                                               checkpoints=stages, auto_repair=True)
                    elif chapter.action == "revise":
                        source_path = self._attempt_path(run, chapter, chapter.source_attempt) / "preview_package.json"
                        if source_path.exists():
                            source = self._package(run, chapter, chapter.source_attempt)
                            self._check_source(source, state_hash)
                            planning = source.plan_result
                            async def keep_plan():
                                return planning
                            await stages.stage("revision_plan", type(planning), keep_plan)
                            package = await service.revise_preview(source, chapter.inputs["replacements"], checkpoints=stages)
                        else:
                            planning, draft, before_hash = self._partial_source(run, chapter)
                            if before_hash != state_hash:
                                raise ValueError("世界状态已变化，不能复用该候选。")
                            replacements = chapter.inputs["replacements"]
                            if not replacements or replacements.keys() - {s.scene_id for s in draft.scenes} or any(not t.strip() for t in replacements.values()):
                                raise ValueError("请选择已有场景并提供非空正文。")
                            for scene in draft.scenes:
                                if scene.scene_id in replacements:
                                    scene.text, scene.generation_metadata = replacements[scene.scene_id], None
                            draft.full_text = "\n\n".join(s.text for s in draft.scenes)
                            draft.generation_metadata = [s.generation_metadata for s in draft.scenes if s.generation_metadata]
                            async def keep_plan():
                                return planning
                            async def keep_draft():
                                return draft
                            await stages.stage("revision_plan", type(planning), keep_plan)
                            draft = await stages.stage("draft", type(draft), keep_draft)
                            package = await service._finish_preview(novel_id, run.config.direction, planning, draft,
                                                                   checkpoints=stages, auto_repair=False)
                    elif chapter.action in {"memory_retry", "memory_assisted_repair"}:
                        from app.services.memory_extraction import MemoryExtractionResult
                        source = self._package(run, chapter, chapter.source_attempt)
                        self._check_source(source, state_hash)
                        feedback = chapter.inputs.get("review_feedback")
                        kwargs = {"review_feedback": feedback} if feedback else {}
                        result = await stages.stage("memory", MemoryExtractionResult, lambda: service.memory_service.extract_preview(
                            novel_id, source.draft, source.plan_result.plan, source.continuity_report, **kwargs))
                        package = self._with_memory(source, result)
                    else:
                        offset = chapter.number - run.start_chapter
                        specific = run.config.chapter_directions[offset] if offset < len(run.config.chapter_directions) else ""
                        direction = "\n".join(filter(None, [run.config.direction, specific])) or None
                        package = await service.generate_preview(novel_id, direction,
                            chapter_number=chapter.number, checkpoints=stages)
                attempt.save_package(package)
                if chapter.action in {"memory", "memory_retry", "memory_assisted_repair"} and chapter.source_attempt:
                    previous_quality = self._attempt_path(run, chapter, chapter.source_attempt) / "quality.json"
                    if previous_quality.exists() and not (path / "quality.json").exists():
                        write_new(path / "quality.json", json.loads(previous_quality.read_text()))
                if reviewer and not (path / "quality.json").exists():
                    from app.quality.models import LiteraryQualityReport
                    quality = await stages.stage("literary_review", LiteraryQualityReport,
                        lambda: reviewer.review(package.draft, package.plan_result.plan))
                    write_new(path / "quality.json", quality.model_dump(mode="json"))
                chapter.characters = len(package.draft.full_text)
                chapter.title = package.draft.title or f"第 {chapter.number} 章"
                valid = package.continuity_report.valid and package.memory_result.validation_report.valid
                applicable = package.memory_result.dry_run is not None and package.memory_result.dry_run.applicable
                run.status = chapter.status = "awaiting_review" if valid and applicable else "blocked"
                run.active_stage = "approval"
                run.error = None if valid and applicable else "记忆校验未通过；请查看报告并修改记忆或重试抽取。"
                self._save(run)
                self.emit(run, type="status", status=run.status, message=run.error or "候选已保存，等待你审阅正文和记忆变化。")
        except asyncio.CancelledError:
            run.status = "interrupted" if self._closing else "cancelled"
            chapter.status = run.status
            self._save(run)
            self.emit(run, type="status", status=run.status, message="任务已停止，已完成阶段保留，可恢复。")
            raise
        except Exception as exc:
            from app.llm.errors import StructuredOutputError
            if isinstance(exc, StructuredOutputError) and "path" in locals() and path.exists():
                # Public completion content only, never provider reasoning or credentials.
                write_new(path / f"structured_failure_{uuid.uuid4().hex[:12]}.json", {
                    "stage": run.active_stage,
                    "errors": [self._error_text(run, value) for value in exc.errors],
                    "raw_outputs": [self._error_text(run, value) for value in exc.raw_outputs],
                })
            message = self._error_text(run, exc)
            run.status = chapter.status = "blocked"
            run.error = chapter.error = message
            self._save(run)
            self.emit(run, type="error", status="blocked", message=message)
        finally:
            for provider in providers:
                if hasattr(provider, "aclose"):
                    await provider.aclose()
        if chapter.action == "memory_assisted_repair" and run.status == "awaiting_review":
            run.status, run.active_stage = "running", "review_assistant"
            self._save(run)
            await self._execute_assistance(run, chapter, "awaiting_review")

    @staticmethod
    def _check_source(package, current_hash):
        expected = ChapterPreviewPackage.compute_hash(plan=package.plan_result.plan,
            draft=package.draft, continuity_report=package.continuity_report,
            memory_result=package.memory_result, story_state_hash=package.story_state_hash)
        if expected != package.preview_package_hash:
            raise ValueError("来源候选内容与 hash 不符，不能复用。")
        if package.story_state_hash != current_hash:
            raise ValueError("世界状态已变化，不能复用该候选。")

    @staticmethod
    def _with_memory(source, memory):
        package = source.model_copy(deep=True)
        package.memory_result = memory
        package.generated_at = datetime.now(UTC)
        package.preview_package_hash = ChapterPreviewPackage.compute_hash(plan=package.plan_result.plan,
            draft=package.draft, continuity_report=package.continuity_report,
            memory_result=memory, story_state_hash=package.story_state_hash)
        return package

    async def resume(self, run_id):
        run = self.get(run_id)
        chapter = self._chapter(run)
        if chapter.attempt_id:
            pending = self._attempt_path(run, chapter) / "pending_approval.json"
            if pending.exists() and run.status != "running":
                # A crash after DB commit but before artifact publication is
                # recovered through the canonical service's duplicate path.
                return await self.approve(run_id, chapter.number,
                    ApprovalInput.model_validate_json(pending.read_text()))
        async with self.locks.setdefault(run.novel_id, asyncio.Lock()):
            self._assert_available(run.novel_id, run.id)
            if run.status in {"completed", "running", "awaiting_review", "archived"}:
                raise ValueError("该任务无需恢复；等待审阅的章节需要先批准或修改。")
            self._launch(run)
        return run

    def provide_credentials(self, run_id: str, api_key: SecretStr):
        run = self.get(run_id)
        if run.config.provider != "custom_input":
            raise ValueError("该任务使用服务端配置的密钥。")
        self._validate_provider(run.config, api_key)
        self._custom_keys[run_id] = api_key
        run.requires_api_key = False
        self._save(run)
        return run

    async def cancel(self, run_id, *, expected_task=None):
        run = self.get(run_id)
        async with self.locks.setdefault(run.novel_id, asyncio.Lock()):
            if expected_task is not None and self.tasks.get(run_id) is not expected_task:
                return run
            return await self._cancel(run)

    async def _cancel(self, run):
        if run.status in {"completed", "archived"}:
            return run
        run_id = run.id
        task = self.tasks.get(run_id)
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        elif run.status != "completed":
            run.status = "cancelled"
            self._save(run)
            self.emit(run, type="status", status="cancelled", message="记录已暂停，可以新建记录；草稿和已提交章节保留。")
        current = self._chapter(run)
        if current.status != "committed":
            current.status = "cancelled"
        run.error = None
        self._save(run)
        return run

    async def finish(self, run_id):
        run = self.get(run_id)
        async with self.locks.setdefault(run.novel_id, asyncio.Lock()):
            return await self._finish_locked(run)

    async def _finish_locked(self, run):
        if run.status == "archived":
            return run
        await self._cancel(run)
        run.status, run.active_stage, run.error = "archived", "archived", None
        for chapter in run.chapters:
            if chapter.status != "committed":
                chapter.status = "archived"
        self._custom_keys.pop(run.id, None)
        self._save(run)
        self.emit(run, type="status", status="archived", message="此创作记录已结束，正文和提交记录保留。可以开始新的创作记录。")
        return run

    async def new_candidate(self, run_id, number, action, inputs, *, runtime=None):
        run = self.get(run_id)
        async with self.locks.setdefault(run.novel_id, asyncio.Lock()):
            if run.status not in {"awaiting_review", "blocked"} or number != run.current_chapter:
                raise ValueError("只能修改当前等待审阅的章节。")
            chapter = self._chapter(run, number)
            if action == "memory_assisted_repair" and chapter.attempt_id != inputs.get("source_attempt"):
                raise ValueError("候选已变化，请重新生成审阅建议。")
            if action not in {"revise", "recheck"}:
                self._package(run, chapter)
            else:
                path = self._attempt_path(run, chapter)
                if not ((path / "preview_package.json").exists() or (path / "stage_draft.json").exists()):
                    raise ValueError("尚无完整场景正文，请恢复生成。")
            if runtime is not None:
                if action != "recheck":
                    raise ValueError("运行参数变化需要另存重新检查候选。")
                config = RunInput.model_validate({**run.config.model_dump(), **runtime.model_dump(exclude_none=True)})
                self._validate_provider(config, self._custom_keys.get(run.id))
                run.config = config
            chapter.source_attempt, chapter.attempt_id = chapter.attempt_id, ""
            chapter.assistance_file = None
            chapter.action, chapter.inputs = action, inputs
            self._launch(run)
        return run

    async def assist_review(self, run_id, number, *, model="", max_output_tokens=None):
        run = self.get(run_id)
        async with self.locks.setdefault(run.novel_id, asyncio.Lock()):
            if run.status not in {"awaiting_review", "blocked"} or number != run.current_chapter:
                raise ValueError("请先等待本章草稿完成，再让 Agent 辅助填写。")
            chapter = self._chapter(run, number)
            self._package(run, chapter)
            if run.mode == "live":
                self._validate_provider(run.config, self._custom_keys.get(run.id))
            previous_status = run.status
            run.status, run.active_stage, run.error = "running", "review_assistant", None
            self._save(run)
            self.tasks[run.id] = asyncio.create_task(self._execute_assistance(run, chapter, previous_status,
                                                     model=model.strip(), max_output_tokens=max_output_tokens),
                                                    name=f"review-assist-{run.id}")
        return run

    async def repair_from_review(self, run_id, number):
        run = self.get(run_id)
        chapter = self._chapter(run, number)
        detail = self.chapter_detail(run_id, number)
        report = detail.get("assistance")
        package = self._package(run, chapter)
        if (not report or report.get("attempt_id") != chapter.attempt_id
            or report.get("preview_package_hash") != package.preview_package_hash
            or report.get("memory_hash") != ReviewApproval.hash_memory(package.memory_result)
            or digest({k: v for k, v in report.items() if k != "report_hash"}) != report.get("report_hash")):
            raise ValueError("当前候选没有有效的 Agent 审阅建议，请先生成审阅建议。")
        if report.get("source") != "model":
            raise ValueError("离线演示建议不能用于真实记忆修订。")
        feedback = {"previous_batch": package.memory_result.batch.model_dump(mode="json", exclude_none=True),
                    "operation_reviews": report["operation_reviews"], "missing": report["missing"]}
        return await self.new_candidate(run_id, number, "memory_assisted_repair", {
            "source_attempt": report["attempt_id"], "review_feedback": feedback})

    async def _execute_assistance(self, run, chapter, previous_status, *, model="", max_output_tokens=None):
        from app.agents.literary_reviewer import LiteraryReviewerAgent
        from app.llm.client import AgentLLMClient
        from app.llm.fake import FakeLLMProvider
        from app.llm.prompts import get_default_registry
        from app.planning.provenance import build_story_state_projection
        from app.quality.review_assistance import ReviewAssistanceReport
        providers = []
        try:
            package = self._package(run, chapter)
            async with self.session_factory() as session:
                self._check_source(package, await compute_story_state_hash(session, uuid.UUID(run.novel_id)))
                from app.services.author_review import build_review_before_state
                before = await build_review_before_state(session, package)
                if self.review_agent_factory:
                    reviewer, providers = await self.review_agent_factory(session, run, chapter, package)
                elif run.mode == "demo":
                    provider = FakeLLMProvider(simulate_latency_ms=100)
                    provider.enqueue_structured({
                        "operation_reviews": [{"operation_id": op.operation_id, "verdict": "correct",
                            "reason": "离线演示的预设判断，仅用于展示表单辅助填写，不代表模型语义审查。"}
                            for op in package.memory_result.batch.operations],
                        "missing": [], "quality_scores": {key: 3 for key in
                            ("readability", "character", "dialogue", "continuity", "progression")},
                        "summary": "离线预设建议，非真实模型审阅。正式使用请自行核对正文与每项记忆。",
                    })
                    reviewer = LiteraryReviewerAgent(AgentLLMClient(provider,
                        agent_name="literary_reviewer", model="scripted-demo-not-a-real-llm"), get_default_registry())
                else:
                    registry, settings, providers = self._runtime(run, chapter)
                    reviewer = LiteraryReviewerAgent(registry.client_for_agent("literary_reviewer", settings,
                        prompt_version="author_review_v1"), get_default_registry())
                if run.mode == "live" and hasattr(reviewer, "llm"):
                    if model:
                        reviewer.llm.model = model
                    if max_output_tokens is not None:
                        reviewer.llm.max_tokens = max_output_tokens
                self.emit(run, type="stage", stage="review_assistant", status="running",
                          message="Agent 正在核对记忆并建议评分；不会自动批准提交。")
                assessment, metadata = await reviewer.assist_review(package, before)
                self._check_source(package, await compute_story_state_hash(session, uuid.UUID(run.novel_id)))
            report = ReviewAssistanceReport(**assessment.model_dump(), attempt_id=chapter.attempt_id,
                preview_package_hash=package.preview_package_hash,
                draft_hash=hashlib.sha256(package.draft.full_text.encode()).hexdigest(),
                memory_hash=ReviewApproval.hash_memory(package.memory_result), generated_at=datetime.now(UTC),
                source="offline_demo" if run.mode == "demo" else "model", generation_metadata=metadata)
            report.report_hash = digest(report.model_dump(mode="json", exclude={"report_hash"}))
            filename = f"review_assistance_{uuid.uuid4().hex[:12]}.json"
            write_new(self._attempt_path(run, chapter) / filename, report.model_dump(mode="json"))
            chapter.assistance_file = filename
            run.status, run.active_stage = previous_status, "approval"
            self._save(run)
            self.emit(run, type="stage", stage="review_assistant", status="completed",
                      message="Agent 建议已保存，可采用或修改后再确认提交。", output=report.model_dump(mode="json"))
        except asyncio.CancelledError:
            run.status = "interrupted" if self._closing else "cancelled"
            self._save(run)
            self.emit(run, type="status", status=run.status, message="辅助填写已停止，正文和已保存建议保留。")
            raise
        except Exception as exc:
            run.status, run.active_stage, run.error = previous_status, "approval", self._error_text(run, exc)
            self._save(run)
            self.emit(run, type="error", status=run.status, message=run.error)
        finally:
            for provider in providers:
                if hasattr(provider, "aclose"):
                    await provider.aclose()

    async def approve(self, run_id, number, payload: ApprovalInput):
        from app.commit.models import ChapterCommitRequest
        from app.services.canonical_commit import CanonicalCommitService

        run = self.get(run_id)
        async with self.locks.setdefault(run.novel_id, asyncio.Lock()):
            chapter = self._chapter(run, number)
            if chapter.attempt_id != payload.attempt_id:
                raise ValueError("候选已变化，请重新审阅当前版本。")
            path = self._attempt_path(run, chapter)
            if not (path / "commit.json").exists() and (number != run.current_chapter or run.status not in {"awaiting_review", "blocked"}):
                if not ((path / "pending_approval.json").exists() and run.status in {"interrupted", "cancelled"}):
                    raise ValueError("当前章节不能提交。")
            package = self._package(run, chapter)
            if payload.assisted_review_hash:
                detail = self.chapter_detail(run_id, number)
                suggestion = detail["assistance"]
                if (not suggestion or suggestion["report_hash"] != payload.assisted_review_hash
                    or digest({k: v for k, v in suggestion.items() if k != "report_hash"}) != suggestion["report_hash"]
                    or suggestion["attempt_id"] != chapter.attempt_id
                    or suggestion["memory_hash"] != ReviewApproval.hash_memory(package.memory_result)
                    or suggestion["preview_package_hash"] != package.preview_package_hash):
                    raise ValueError("Agent 建议已过期，请重新生成建议或手动审阅。")
            approval = ReviewApproval(**payload.model_dump(),
                draft_hash=hashlib.sha256(package.draft.full_text.encode()).hexdigest(),
                memory_hash=ReviewApproval.hash_memory(package.memory_result))
            request = ChapterCommitRequest(
                    novel_id=run.novel_id, chapter_plan=package.plan_result.plan, chapter_draft=package.draft,
                    continuity_report=package.continuity_report, memory_result=package.memory_result,
                    expected_story_state_hash=package.story_state_hash,
                    preview_package_hash=payload.preview_package_hash,
                    attempt_id=chapter.attempt_id, approval=approval)
            approval.verify(request)
            self._check_source(package, package.story_state_hash)
            _atomic_json(path / "pending_approval.json", payload.model_dump(mode="json"))
            async with self.session_factory() as session:
                result = await CanonicalCommitService(session).commit_chapter(request)
            if not (path / "approval.json").exists():
                write_new(path / "approval.json", approval.model_dump(mode="json"))
            if not (path / "commit.json").exists():
                write_new(path / "commit.json", result.model_dump(mode="json"))
            was_committed = chapter.status == "committed"
            chapter.status = "committed"
            if not was_committed:
                self.emit(run, type="stage", stage="finalize", status="completed", message=f"第 {number} 章已提交。",
                          commit=result.model_dump(mode="json"))
                if number == run.start_chapter + run.total_chapters - 1:
                    run.status, run.active_stage = "completed", "completed"
                    self._save(run)
                    self.emit(run, type="status", status="completed", message="所有章节均已审阅并提交。")
                else:
                    run.current_chapter = number + 1
                    run.status = "interrupted"
                    self._save(run)
                    self._launch(run)
            return run

    async def close(self):
        self._closing = True
        for task in self.tasks.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        self._custom_keys.clear()
        self._owner.close()
