"""Durable author-side launch workflow, followed by the existing chapter engine.

Source data, plans and approvals are files. Only approve() bootstraps a NEW
canonical novel, in one transaction. Chapter commits retain Studio's human gate.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from datetime import datetime, UTC
from pathlib import Path

from sqlalchemy import select

from app.agents.book_launch import MarketResearchAgent, StoryArchitectAgent, PROMPTS
from app.config import AgentModelConfig, LLMProviderConfig
from app.llm.budget import TrialBudget
from app.llm.client import AgentLLMClient
from app.llm.credentials import read_api_key
from app.llm.errors import scrub_secrets
from app.llm.prompts import get_default_registry
from app.llm.registry import build_registry
from app.models import Arc, Novel
from app.schemas.book_launch import (
    LaunchConfig, LaunchSession, LaunchStart, TopicSelection, BlueprintApproval,
    MarketResearch, BookBlueprint, LaunchBudget, ResearchForkInput, BlueprintRevision,
)
from app.schemas.studio import RunInput
from app.services.story_state import StoryStateService
from app.services.studio import _atomic_json
from app.services.trial_artifacts import TrialAttempt, digest, safe_name, write_new


class BookLaunchManager:
    def __init__(self, studio):
        self.studio = studio
        self.root = studio.root / "book_launch"
        self.root.mkdir(exist_ok=True)
        self.sessions = {}
        self.tasks = {}
        self.keys = {}
        self.locks = {}
        for path in self.root.glob("*/session.json"):
            item = LaunchSession.model_validate_json(path.read_text())
            if item.status in {"researching", "planning"}:
                item.status, item.error = "interrupted", "服务重启；可恢复未完成阶段。"
                self.save(item)
            self.sessions[item.id] = item

    def get(self, id):
        return self.sessions[safe_name(id)]

    def path(self, item):
        return self.root / item.id

    def save(self, item):
        item.updated_at = datetime.now(UTC)
        _atomic_json(self.path(item) / "session.json", item.model_dump(mode="json"))

    def emit(self, item, **event):
        with (self.path(item) / "events.jsonl").open("a") as f:
            f.write(json.dumps({"at": datetime.now(UTC).isoformat(), **event}, ensure_ascii=False) + "\n")

    def events(self, id):
        path = self.path(self.get(id)) / "events.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def budget(self, item):
        cfg = item.config
        return TrialBudget(self.path(item) / "usage_ledger.json", 0,
            max_calls=cfg.max_calls, chapter_tokens=cfg.token_budget, run_tokens=cfg.token_budget)

    def signature(self, item):
        prompts = get_default_registry()
        return digest({"config": item.config.model_dump(mode="json", exclude={"max_calls", "token_budget"}),
                       "prompts": {role: prompts.get(role).content for role in PROMPTS}})

    def validate_provider(self, item):
        cfg = item.config
        if cfg.mode == "demo":
            return
        if not cfg.model.strip():
            raise ValueError("模型名称不能为空")
        key = self.keys.get(item.id)
        if cfg.provider == "custom_input":
            if not key or not key.get_secret_value().strip():
                raise ValueError("自定义服务需重新提供 API Key；不会写入磁盘")
        elif cfg.provider not in self.studio.settings.llm.providers:
            raise ValueError("未知 provider")
        elif not read_api_key(self.studio.settings.llm.providers[cfg.provider].api_key_env):
            raise ValueError("服务端尚未配置该 provider 的 API Key")

    async def create(self, request: LaunchStart):
        item = LaunchSession(id=uuid.uuid4().hex, novel_id=uuid.uuid4(),
            config=LaunchConfig.model_validate(request.model_dump()), status="researching")
        if request.api_key:
            self.keys[item.id] = request.api_key
        try:
            self.validate_provider(item)
        except Exception:
            self.keys.pop(item.id, None)
            raise
        self.path(item).mkdir()
        write_new(self.path(item) / "manifest.json", {"signature": self.signature(item)})
        self.sessions[item.id] = item
        self.save(item)
        self.tasks[item.id] = asyncio.create_task(self.execute(item))
        return item

    async def select(self, id, selection: TopicSelection):
        item = self.get(id)
        async with self.locks.setdefault(id, asyncio.Lock()):
            if item.status != "awaiting_topic" or selection.research_hash != item.research_hash:
                raise ValueError("选题确认已过期或当前阶段不允许选择")
            if selection.topic_id not in {t.id for t in item.research.topics}:
                raise ValueError("未知选题")
            self.validate_provider(item)
            item.selected_topic, item.status = selection.topic_id, "planning"
            self.save(item)
            self.tasks[id] = asyncio.create_task(self.execute(item))
        return item

    async def fork_research(self, id, request: ResearchForkInput):
        source = self.get(id)
        async with self.locks.setdefault(id, asyncio.Lock()):
            research = source.research
            if not research or digest(research.model_dump(mode="json")) != source.research_hash:
                raise ValueError("原任务没有完整且可校验的研究结果")
            cfg = LaunchConfig.model_validate({**source.config.model_dump(mode="json"),
                **request.model_dump(mode="json")})
            item = LaunchSession(id=uuid.uuid4().hex, novel_id=uuid.uuid4(), config=cfg,
                status="awaiting_topic", source_session_id=source.id,
                research=research.model_copy(deep=True), research_hash=source.research_hash)
            if request.api_key:
                self.keys[item.id] = request.api_key
            try:
                self.validate_provider(item)
            except Exception:
                self.keys.pop(item.id, None)
                raise
            self.path(item).mkdir()
            write_new(self.path(item) / "manifest.json", {"signature": self.signature(item),
                "source_session_id": source.id, "source_research_hash": source.research_hash})
            write_new(self.path(item) / "research.json", research.model_dump(mode="json"))
            self.sessions[item.id] = item
            self.save(item)
            self.emit(item, type="stage", message=f"复用规划 {source.id} 的已完成市场研究；请选择方向")
            return item

    async def resume(self, id, key=None):
        item = self.get(id)
        async with self.locks.setdefault(id, asyncio.Lock()):
            if item.status not in {"failed", "cancelled", "interrupted"}:
                raise ValueError("只有失败、中断或取消的规划可以恢复")
            manifest = json.loads((self.path(item) / "manifest.json").read_text())
            if manifest["signature"] != self.signature(item):
                raise ValueError("输入或 prompt 已变化，请创建新的规划，不复用旧阶段")
            if key:
                self.keys[id] = key
            if not item.blueprint and (item.selected_topic or not item.research):
                self.validate_provider(item)
            item.error = None
            item.status = ("awaiting_approval" if item.blueprint else "planning" if item.selected_topic
                           else "awaiting_topic" if item.research else "researching")
            self.save(item)
            if item.status in {"researching", "planning"}:
                self.tasks[id] = asyncio.create_task(self.execute(item))
        return item

    async def cancel(self, id):
        item = self.get(id)
        async with self.locks.setdefault(id, asyncio.Lock()):
            if item.status == "created":
                raise ValueError("已创建的小说请在工坊管理；取消规划不会删除小说")
            task = self.tasks.get(id)
            if task and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            item.status = "cancelled"
            self.save(item)
        return item

    async def update_budget(self, id, limits: LaunchBudget):
        item = self.get(id)
        async with self.locks.setdefault(id, asyncio.Lock()):
            if item.status in {"researching", "planning", "created"}:
                raise ValueError("请先暂停；已开书无需调整规划预算")
            usage = self.budget(item).summary()["run"]
            if limits.max_calls < usage["calls"] or limits.token_budget < usage["charged_tokens"]:
                raise ValueError("预算不能低于已计入用量")
            item.config = LaunchConfig.model_validate({**item.config.model_dump(), **limits.model_dump()})
            self.save(item)
        return item

    def runtime(self, item):
        if item.config.mode == "demo":
            from app.llm.fake import FakeLLMProvider
            from app.services.book_launch_demo import demo_research, demo_blueprint
            provider = FakeLLMProvider()
            if item.selected_topic:
                topic = next(t for t in item.research.topics if t.id == item.selected_topic)
                provider.enqueue_structured(demo_blueprint(topic))
            else:
                provider.enqueue_structured(demo_research(item.config.sample.books[0].id))
            return {role: AgentLLMClient(provider, agent_name=role, model="offline-demo",
                        prompt_version=version) for role, version in PROMPTS.items()}, [provider]
        cfg = item.config
        settings = self.studio.settings.model_copy(deep=True)
        if cfg.provider == "custom_input":
            settings.llm.providers[cfg.provider] = LLMProviderConfig(base_url=cfg.custom_base_url, model=cfg.model)
        for role in PROMPTS:
            settings.llm.agents[role] = AgentModelConfig(provider=cfg.provider, model=cfg.model,
                max_tokens=cfg.max_output_tokens, temperature=0.3 if role == "market_research" else 0.6)
        registry = build_registry(settings, api_keys={cfg.provider: self.keys[item.id]} if item.id in self.keys else None)
        providers = [registry.get(n) for n in registry.names()]
        for name in registry.names():
            p = registry.get(name)
            p.budget, p.trace_path = self.budget(item), self.path(item) / "llm_calls.jsonl"
            p.model_capabilities = {**settings.llm.providers[name].model_capabilities,
                                    **(cfg.runtime_capabilities or {})}
            p.event_callback = lambda event: self.emit(item, type="llm", **event)
        return {role: registry.client_for_agent(role, settings, prompt_version=version)
                for role, version in PROMPTS.items()}, providers

    async def execute(self, item):
        providers = []
        try:
            clients, providers = self.runtime(item)
            stage = "blueprint" if item.selected_topic else "research"
            checkpoint = self.path(item) / f"checkpoint_{stage}"
            attempt = TrialAttempt(checkpoint, {"input_signature": self.signature(item),
                "selected_topic": item.selected_topic, "research_hash": item.research_hash if item.selected_topic else ""},
                resume=checkpoint.exists())
            if not item.selected_topic:
                self.emit(item, type="stage", message="市场研究开始")
                research = await attempt.stage("research", MarketResearch,
                    lambda: MarketResearchAgent(clients["market_research"]).research(item.config))
                if not (self.path(item) / "research.json").exists():
                    write_new(self.path(item) / "research.json", research.model_dump(mode="json"))
                item.research, item.research_hash = research, digest(research.model_dump(mode="json"))
                item.status = "awaiting_topic"
            else:
                self.emit(item, type="stage", message="选题已确认，构建世界与十章计划")
                topic = next(t for t in item.research.topics if t.id == item.selected_topic)
                blueprint = await attempt.stage("blueprint", BookBlueprint,
                    lambda: StoryArchitectAgent(clients["story_architect"]).design(item.config.brief, topic))
                if not (self.path(item) / "blueprint.json").exists():
                    write_new(self.path(item) / "blueprint.json", blueprint.model_dump(mode="json"))
                item.blueprint, item.blueprint_hash = blueprint, digest(blueprint.model_dump(mode="json"))
                item.status = "awaiting_approval"
            self.save(item)
            self.emit(item, type="stage", message=item.status)
        except asyncio.CancelledError:
            item.status = "interrupted"
            self.save(item)
            raise
        except Exception as exc:
            secrets = [read_api_key(p.api_key_env) for p in self.studio.settings.llm.providers.values()]
            if item.id in self.keys:
                secrets.append(self.keys[item.id].get_secret_value())
            item.error = scrub_secrets(str(exc), *secrets)
            item.status = "failed"
            self.save(item)
            self.emit(item, type="error", message=item.error)
        finally:
            for p in providers:
                if hasattr(p, "aclose"):
                    await p.aclose()

    async def revise_blueprint(self, id, revision: BlueprintRevision):
        item = self.get(id)
        async with self.locks.setdefault(id, asyncio.Lock()):
            if item.status != "awaiting_approval" or revision.source_hash != item.blueprint_hash:
                raise ValueError("设定候选已过期或当前阶段不能修改")
            if digest(item.blueprint.model_dump(mode="json")) != item.blueprint_hash:
                raise ValueError("原设定 hash 不匹配")
            value = revision.blueprint.model_dump(mode="json")
            new_hash = digest(value)
            if new_hash != item.blueprint_hash:
                target = self.path(item) / f"blueprint_{new_hash}.json"
                if not target.exists():
                    write_new(target, {"source_hash": item.blueprint_hash, "blueprint": value})
                item.blueprint, item.blueprint_hash = revision.blueprint, new_hash
                item.approval = None
                self.save(item)
                self.emit(item, type="revision", message="已另存修订后的设定，旧确认已失效", blueprint_hash=new_hash)
            return item

    async def approve(self, id, approval: BlueprintApproval):
        item = self.get(id)
        async with self.locks.setdefault(id, asyncio.Lock()):
            if (not approval.approved or not approval.reviewed_by.strip()
                or not item.blueprint or approval.blueprint_hash != item.blueprint_hash
                or digest(item.blueprint.model_dump(mode="json")) != item.blueprint_hash):
                raise ValueError("请审阅完整设定并确认具体 blueprint hash")
            if item.status == "created":
                return item
            if item.status != "awaiting_approval":
                raise ValueError("当前阶段不能开书")
            # Persist exact approval before DB commit, enabling recovery across a crash.
            item.approval = approval
            self.save(item)
            b = item.blueprint
            async with self.studio.session_factory() as session:
                async with session.begin():
                    novel = await session.get(Novel, item.novel_id)
                    if novel is None:
                        novel = Novel(id=item.novel_id, title=b.title, genre=b.genre, premise=b.public_premise)
                        session.add(novel)
                        await session.flush()
                        await StoryStateService(session).bootstrap_world(novel.id, b.world)
                        arc = await session.scalar(select(Arc).where(Arc.novel_id == novel.id))
                        if arc:
                            novel.current_arc_id = arc.id
                    elif novel.title != b.title or novel.premise != b.public_premise:
                        raise ValueError("预分配 novel ID 与已批准设定不一致")
            item.status = "created"
            self.save(item)
            self.emit(item, type="approval", message="人工确认设定，已事务创建新小说；未来大纲仍是计划")
        return item

    async def start_chapters(self, id, config: RunInput, api_key=None):
        item = self.get(id)
        async with self.locks.setdefault(id, asyncio.Lock()):
            if item.status != "created" or config.novel_id != item.novel_id:
                raise ValueError("请先确认开书设定")
            if item.studio_run_id:
                return self.studio.get(item.studio_run_id)
            # Recover if Studio published a run but the launch receipt was interrupted.
            previous = next((r for r in self.studio.runs.values() if r.novel_id == str(item.novel_id)), None)
            if previous:
                item.studio_run_id = previous.id
                self.save(item)
                return previous
            if config.mode == "live" and item.config.mode == "demo":
                raise ValueError("合成演示设定仅用于 demo；真实创作请新建 live 规划")
            config = config.model_copy(update={"chapter_directions": [
                c.direction() for c in item.blueprint.opening_chapters[:config.chapter_count]]})
            run = await self.studio.create(config, api_key=api_key)
            item.studio_run_id = run.id
            self.save(item)
            return run

    async def close(self):
        tasks = [t for t in self.tasks.values() if not t.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.keys.clear()
