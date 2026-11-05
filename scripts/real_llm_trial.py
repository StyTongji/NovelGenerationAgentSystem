#!/usr/bin/env python3
"""Real-LLM 3-Chapter Authoring Trial runner (Phase 8.6/8.7).

Development tooling that drives the PRODUCTION pipeline only:

    ChapterGenerationService.generate_preview
    -> PreviewArtifactExporter
    -> human review
    -> ChapterGenerationService.finalize

It never re-implements pipeline logic and never writes canonical state
outside ChapterGenerationService.finalize.

Usage (API key comes ONLY from the environment):

    export NOVEL_LLM_API_KEY=...
    export NOVEL_LLM_BASE_URL=https://api.example.com/v1

    python scripts/real_llm_trial.py init --model <model>
    python scripts/real_llm_trial.py preview --run <run_id>            # ch1
    python scripts/real_llm_trial.py memory-retry --run <run_id> --chapter 1
    #   ... human review of real_trial_runs/<run_id>/chapter_01/ ...
    python scripts/real_llm_trial.py finalize --run <run_id> --chapter 1
    python scripts/real_llm_trial.py preview --run <run_id>            # ch2
    ...
    python scripts/real_llm_trial.py state --run <run_id>
    python scripts/real_llm_trial.py report --run <run_id>

Smoke test without a key:
    python scripts/real_llm_trial.py init --provider fake
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

RUNS_ROOT = REPO_ROOT / "real_trial_runs"

# pre-written user directions for the three trial chapters (spec §9/§14/§17)
CHAPTER_DIRECTIONS = {
    1: (
        "林凡因为一次宗门任务来到天剑城。让他接触到一件与轮回古镜纹路相似的"
        "旧物，但暂时不要让他知道它真正意味着什么。苏青也参与这次任务。"
        "这一章主要写探索、人物互动和悬念，不要安排大规模战斗，不要让林凡突破。"
        "结尾留下一个能推动下一章调查的线索。"
    ),
    2: (
        "继续上一章。让林凡顺着上一章得到的线索继续调查，重点写他和苏青对"
        "这件事的不同判断。可以出现一些危险，但暂时不要直接揭晓父亲失踪真相。"
        "结尾让林凡意识到，这件事可能和一个已经消失的宗门有关。"
    ),
    3: (
        "继续推进剧情。安排一次小规模冲突，让林凡使用自己目前已经拥有的能力"
        "和物品解决问题。不要升级过快。结尾把父亲失踪线再推进一点，但仍然"
        "不要给出最终答案。"
    ),
}

WORLD_SPEC = {
    "power_system": {
        "name": "青玄修炼体系",
        "rules": [
            "常态下跨两个大境界正面战斗几乎不可能取胜",
            "突破需要契机与积累，不可凭空跃升",
        ],
        "realms": [
            {"name": "炼体", "rank": 1},
            {"name": "练气", "rank": 2, "sublevels": ["一层", "三层", "六层", "九层", "圆满"]},
            {"name": "筑基", "rank": 3, "sublevels": ["初期", "中期", "后期"]},
            {"name": "金丹", "rank": 4},
            {"name": "元婴", "rank": 5},
        ],
    },
    "characters": [
        {
            "name": "林凡", "is_protagonist": True, "gender": "男",
            "description": "17岁，青云宗外门弟子，练气九层",
            "personality_traits": ["谨慎", "观察力强", "不轻信陌生人"],
            "background": "父亲数年前神秘失踪，林凡修炼刻苦，目标是进入内门并查明父亲下落",
            "realm_name": "练气", "subrealm": "九层",
            "current_goals": ["进入内门", "调查父亲失踪"],
        },
        {
            "name": "苏青", "gender": "女",
            "description": "青云宗内门弟子，筑基初期",
            "personality_traits": ["冷静", "有责任感"],
            "background": "与林凡相识，但关系尚未非常亲密",
            "realm_name": "筑基", "subrealm": "初期",
        },
        {
            "name": "玄清真人", "gender": "男",
            "description": "青云宗长老，金丹期修为，表面温文尔雅",
            "personality_traits": ["温和", "深藏不露"],
            "realm_name": "金丹",
        },
        {
            "name": "魔教长老", "gender": "男",
            "description": "魔道势力中的核心人物，身份隐秘",
            "personality_traits": ["冷酷", "多疑"],
            "realm_name": "元婴",
        },
    ],
    "locations": [
        {"name": "青玄大陆"},
        {"name": "天剑城", "parent_name": "青玄大陆",
         "description": "大陆东部最大的坊市之城，鱼龙混杂"},
        {"name": "青云宗", "parent_name": "青玄大陆"},
    ],
    "items": [
        {
            "name": "轮回古镜", "aliases": ["古镜"], "type": "artifact",
            "grade": "仙品（残缺）",
            "description": "一面残缺的古镜，在特殊条件下能显现过去的残缺影像",
            "owner_name": "林凡",
            "abilities": ["在特殊条件下显现残缺的过去影像"],
            "restrictions": ["无法主动随时使用", "显现的信息不完整", "需要特殊地点或媒介触发"],
        }
    ],
    "initial_facts": [
        {
            "statement": "玄清真人与魔道势力存在秘密联系",
            "secrecy": "top_secret",
            "known_by": ["魔教长老"],
            "suspected_by": ["苏青"],
        },
        {
            "statement": "林凡持有轮回古镜",
            "secrecy": "secret",
            "known_by": ["林凡"],
        },
    ],
    "plot_threads": [
        {
            "title": "父亲失踪之谜",
            "status": "active",
            "truth": "父亲并未死亡，去向与轮回遗迹有关",
            "public_summary": "林凡的父亲数年前神秘失踪",
            "priority": 0.95,
        }
    ],
    "plot_debts": [
        {"debt_type": "event", "description": "七日后的宗门考核",
         "created_chapter": 0, "due_chapter": 12},
    ],
    "arc": {
        "title": "外门试炼篇",
        "start_chapter": 1,
        "planned_end_chapter": 20,
        "goal": "林凡通过宗门考核进入内门，同时追查父亲失踪的线索",
    },
    "constraints": [
        {"type": "hard", "statement": "前三章苏青不得死亡"},
        {"type": "hard", "statement": "前三章不得完整揭露玄清真人与魔道势力的秘密"},
        {"type": "hard", "statement": "林凡不得非法跨境突破（练气九层不得直接跃至金丹）"},
        {"type": "soft", "statement": "本章以探索、互动与悬念为主，减少大规模战斗"},
    ],
}


def _configure(
    run_dir: Path, provider: str, model: str, base_url: str | None,
    *, require_key: bool = True,
) -> None:
    """Set env BEFORE app modules are imported (handled by main())."""
    os.environ.setdefault("NOVEL_ENVIRONMENT", "development")
    os.environ["NOVEL_DATABASE_URL"] = f"sqlite+aiosqlite:///{run_dir / 'trial.db'}"
    os.environ["NOVEL_SYNC_DATABASE_URL"] = f"sqlite:///{run_dir / 'trial.db'}"
    os.environ["NOVEL_PREVIEW_ARTIFACT_DIR"] = ""
    if provider == "real":
        if require_key and not os.environ.get("NOVEL_LLM_API_KEY"):
            sys.exit(
                "ERROR: NOVEL_LLM_API_KEY is not set. The key may only come "
                "from the environment; never put it in files."
            )
        if base_url:
            os.environ["NOVEL_LLM_BASE_URL"] = base_url
        if model:
            os.environ["NOVEL_TRIAL_MODEL"] = model


def _build_services(run_dir: Path, provider: str, model: str, engine=None, *, chapter=1, memory_model=None, capabilities=None, chapter_token_budget=150_000, auto_until=None, memory_max_tokens=8192, chapter_target_chars=5000):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from app.agents.memory_extractor import MemoryExtractorAgent
    from app.agents.scene_planner import ScenePlannerAgent
    from app.agents.writer import WriterAgent
    from app.agents.writer_repair import WriterRepairAgent
    from app.debug import PreviewExportService
    from app.llm.client import AgentLLMClient
    from app.llm.fake import FakeLLMProvider
    from app.llm.openai_compatible import OpenAICompatibleProvider
    from app.llm.prompts import get_default_registry
    from app.memory.models import MemoryUpdateBatch  # noqa: F401
    from app.memory.validator import MemoryUpdateValidator
    from app.planning.scene_validator import ScenePlanValidator
    from app.services.canonical_commit import CanonicalCommitService
    from app.services.chapter_draft import ChapterDraftService
    from app.services.chapter_generation import ChapterGenerationService
    from app.services.chapter_planning import ChapterPlanningService, build_director_agent
    from app.services.continuity import ContinuityService
    from app.services.memory_extraction import MemoryExtractionService

    if engine is None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{run_dir / 'trial.db'}", poolclass=NullPool
        )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    session = session_factory()

    if provider == "fake":
        llm_provider = FakeLLMProvider()
        model_name = "fake-model"
    else:
        from app.llm.budget import TrialBudget
        llm_provider = OpenAICompatibleProvider(
            base_url=os.environ.get("NOVEL_LLM_BASE_URL", "https://api.scnet.cn/api/llm/v1"),
            api_key_env="NOVEL_LLM_API_KEY",
            model=model,
            budget=TrialBudget(run_dir / "usage_ledger.json", chapter, chapter_tokens=chapter_token_budget),
            trace_path=run_dir / "llm_calls.jsonl",
        )
        model_name = model
        if auto_until:
            cutoff = datetime.fromisoformat(auto_until)
            if cutoff.tzinfo is None:
                raise ValueError("auto-until requires a timezone-aware ISO timestamp")
            llm_provider.request_cutoffs["Auto"] = cutoff
        if capabilities:
            profile = json.loads(Path(capabilities).read_text())
            if profile["base_url"].rstrip("/") != llm_provider.base_url.rstrip("/"):
                raise ValueError("Capability profile belongs to a different endpoint")
            llm_provider.model_capabilities = {
                name: info for name, info in profile["models"].items()
                if info.get("listed") and info.get("text_ok")
            }

    prompts = get_default_registry()
    # one reliable model for every agent in round 1; temperatures keep the
    # role defaults (director 0.5 / writer 0.85 / extractor 0.1, config-driven)
    temperatures = {
        "director": 0.5, "scene_planner": 0.5, "writer": 0.85,
        "writer_repair": 0.85, "continuity_checker": 0.1, "memory_extractor": 0.1,
    }

    def client(agent: str) -> AgentLLMClient:
        return AgentLLMClient(
            llm_provider, agent_name=agent,
            model=memory_model if agent == "memory_extractor" and memory_model else model_name,
            temperature=temperatures[agent],
            max_tokens=memory_max_tokens if agent == "memory_extractor" else 8192,
            deadline_seconds=getattr(llm_provider, "deadline_seconds", 240.0),
        )
    planning = ChapterPlanningService(
        session,
        director=build_director_agent(session, client("director")),
    )
    draft = ChapterDraftService(
        session,
        ScenePlannerAgent(
            llm=client("scene_planner"), prompt_registry=prompts,
            validator=ScenePlanValidator(session),
            chapter_target_chars=chapter_target_chars,
        ),
        WriterAgent(llm=client("writer"), prompt_registry=prompts),
    )
    from app.agents.continuity_checker import LLMContinuityChecker
    continuity = ContinuityService(
        session,
        llm_checker=LLMContinuityChecker(client("continuity_checker"), prompts),
        repair_agent=WriterRepairAgent(llm=client("writer_repair"), prompt_registry=prompts),
        mode="hybrid",
    )
    memory = MemoryExtractionService(
        session,
        MemoryExtractorAgent(llm=client("memory_extractor"), prompt_registry=prompts),
        require_evidence_refs=True,
    )
    generation = ChapterGenerationService(
        session, planning, draft, continuity, memory, CanonicalCommitService(session)
    )
    generation.trial_provider = llm_provider
    return session, generation, PreviewExportService(run_dir), engine


async def _create_schema(engine) -> None:
    from app.models import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _init_world(session) -> tuple[str, dict]:
    from app.planning.provenance import hash_state_dict
    from app.schemas.story import BootstrapWorldRequest
    from app.services.story_state import StoryStateService

    service = StoryStateService(session)
    novel = await service.create_novel(
        title="九霄问道（真人 Trial）", genre="玄幻",
        premise="外门弟子林凡追查父亲失踪之谜，逐步卷入轮回遗迹的因果。",
    )
    await service.bootstrap_world(
        novel.id, BootstrapWorldRequest.model_validate(WORLD_SPEC)
    )
    # build projection BEFORE commit so the session.get connection is
    # properly released by the commit (avoids a GC'd fairy at exit)
    from app.planning.provenance import build_story_state_projection

    state = await build_story_state_projection(session, novel.id)
    await session.commit()
    return str(novel.id), state


def _load_state(run_dir: Path) -> dict:
    state_file = run_dir / "state.json"
    if not state_file.exists():
        available = [
            d.name for d in RUNS_ROOT.iterdir()
            if d.is_dir() and (d / "state.json").exists()
        ] if RUNS_ROOT.is_dir() else []
        sys.exit(
            f"ERROR: run {run_dir.name!r} has no state.json.\n"
            f"Available runs: {available}\n"
            f"Usage: python scripts/real_llm_trial.py <command> --run <one_of_the_above>"
        )
    return json.loads(state_file.read_text(encoding="utf-8"))


def _save_state(run_dir: Path, state: dict) -> None:
    (run_dir / "state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _write_review_template(chapter_dir: Path, chapter_number: int, package, context_debug) -> None:
    plan = package.plan_result.plan
    draft = package.draft
    memory = package.memory_result
    ops_by_type: dict[str, int] = {}
    for op in memory.batch.operations:
        ops_by_type[op.update_type.value] = ops_by_type.get(op.update_type.value, 0) + 1
    scene_lengths = [len(s.text) for s in draft.scenes]

    lines = [
        f"# Chapter {chapter_number:02d} Review",
        "",
        "## 客观数据（自动记录）",
        f"- target/pacing: {plan.pacing or 'n/a'}",
        f"- scene_count: {len(draft.scenes)}",
        f"- actual_chars: {len(draft.full_text)}",
        f"- scene_chars: {scene_lengths}",
        f"- continuity issues: {len(package.continuity_report.issues)} "
        f"(blocking: {package.continuity_report.blocking_issue_count})",
        f"- memory operations: {ops_by_type}",
        f"- requires_confirmation: {memory.dry_run.requires_confirmation if memory.dry_run else 'n/a'}",
        f"- draft_hash: {hashlib.sha256(draft.full_text.encode()).hexdigest()[:16]}...",
        "",
        "## Director",
        "- 是否真正理解用户要求？",
        "- 有没有机械照抄？",
        "- 有没有自然新增冲突？",
        "- 是否提前揭露秘密？",
        "- ending hook 是否自然？",
        "",
        "## Scene Planner",
        "- Scene 数是否合理？是否切得太碎？",
        "- 是否多个 Scene 做同一件事？transition 是否自然？",
        "",
        "## Writer",
        "- 文笔 / 对话 / 人物差异 / 环境与心理描写：",
        "- 套话与重复（瞳孔微缩 / 心头一震 / 深吸一口气 等）：",
        "- AI 味 / Scene 拼接感：",
        "",
        "## Continuity",
        "- true positives：",
        "- false positives：",
        "- suspected false negatives：",
        "- repair count：",
        "",
        "## Memory（逐条标记 correct / missing / wrong / over-extracted）",
    ]
    for op in memory.batch.operations:
        lines.append(
            f"- [{op.update_type.value}] {op.target_id()[:40]} — "
            f"evidence: {op.evidence[0].exact_quote[:40]}… → "
            f"correct / missing / wrong / over-extracted"
        )
    lines += [
        "",
        "## 高风险 mutation 人工确认",
        "- death / realm / item ownership / story_fact_create / knowledge known / "
        "plot resolve / new entity：",
        "",
        "## Repair Damage（如有）",
        "- 文风是否突变 / 删除有效细节 / 语气改变 / 丢失 required outcome：",
        "",
        "## Writing Quality Baseline（主观打分留给人类）",
        "| 维度 | 分数 /10 |",
        "| --- | --- |",
        "| 文笔 | |",
        "| 剧情自然度 | |",
        "| 角色一致性 | |",
        "| 人物区分度 | |",
        "| 对话 | |",
        "| 章节衔接 | |",
        "| 悬念 | |",
        "| 场景融合 | |",
        "| AI 模板感（越高越严重） | |",
        "| 重复（越高越严重） | |",
        "",
    ]
    (chapter_dir / f"chapter_{chapter_number:02d}_review.md").write_text(
        "\n".join(lines), encoding="utf-8"
    )


# --------------------------------------------------------------------- commands

def _check_package(package):
    from app.commit.models import ChapterPreviewPackage
    expected = ChapterPreviewPackage.compute_hash(
        plan=package.plan_result.plan, draft=package.draft,
        continuity_report=package.continuity_report, memory_result=package.memory_result,
        story_state_hash=package.story_state_hash,
    )
    if expected != package.preview_package_hash:
        raise ValueError("Source preview integrity check failed")


async def _open_attempt(args, run_dir, state, session, *, kind, direction=None, source_package=None):
    from app.planning.provenance import compute_story_state_hash
    from app.services.trial_artifacts import TrialAttempt, safe_name, digest
    current = await compute_story_state_hash(session, uuid.UUID(state["novel_id"]))
    chapter = args.chapter or state["next_chapter"]
    if current != state["story_state_hash"]:
        raise ValueError("Current story_state_hash differs from trial before-state")
    if source_package is not None and (source_package.story_state_hash != current
        or source_package.draft.story_context_hash != current
        or source_package.novel_id != state["novel_id"]
        or source_package.chapter_number != chapter):
        raise ValueError("Source preview does not match current story state/chapter")
    signature = {
        "kind": kind, "chapter": chapter, "direction": direction, "state_hash": current,
        "model": state.get("model"), "memory_model": getattr(args, "memory_model", None),
        "chapter_target_chars": getattr(args, "target_chars", 5000),
        "base_url": os.environ.get("NOVEL_LLM_BASE_URL", "https://api.scnet.cn/api/llm/v1"),
        "source_attempt": getattr(args, "source_attempt", None),
        "replacements": getattr(args, "replacements", None),
        "edited_batch": getattr(args, "edited_batch", None),
        "chapter_token_budget": getattr(args, "chapter_token_budget", 150_000),
        "auto_until": getattr(args, "auto_until", None),
        "memory_max_tokens": getattr(args, "memory_max_tokens", 8192),
        "capabilities": str(Path(args.capabilities).resolve()) if getattr(args, "capabilities", None) else None,
        "capabilities_hash": digest(json.loads(Path(args.capabilities).read_text())) if getattr(args, "capabilities", None) else None,
        "runner_hash": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "source_hash": digest(source_package.model_dump(mode="json")) if source_package else None,
        "code_hash": digest({str(p.relative_to(REPO_ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                             for p in sorted((REPO_ROOT / "app").rglob("*.py"))}),
        "prompt_hash": digest({p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                               for p in sorted((REPO_ROOT / "prompts").glob("*.md"))}),
    }
    name = getattr(args, "attempt", None) or datetime.now(UTC).strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8]
    return TrialAttempt(run_dir / f"chapter_{chapter:02d}" / "attempts" / safe_name(name),
                        signature, resume=getattr(args, "resume", False))


def _save_failure(attempt, exc):
    from app.services.trial_artifacts import write_new
    from app.llm.errors import scrub_secrets
    safe = scrub_secrets(json.dumps({"error_type": type(exc).__name__, "message": str(exc),
                        "raw_outputs": getattr(exc, "raw_outputs", [])}, ensure_ascii=False),
                        os.environ.get("NOVEL_LLM_API_KEY"))
    write_new(attempt.path / f"failure_{uuid.uuid4().hex}.json", json.loads(safe))


async def _close_services(session, generation, engine):
    try:
        await session.close()
    finally:
        try:
            provider = getattr(generation, "trial_provider", None)
            if provider is not None and hasattr(provider, "aclose"):
                await provider.aclose()
        finally:
            await engine.dispose()


async def cmd_resume(args):
    from app.services.trial_artifacts import safe_name
    path = RUNS_ROOT / safe_name(args.run) / f"chapter_{args.chapter:02d}" / "attempts" / safe_name(args.attempt)
    signature = json.loads((path / "manifest.json").read_text())["signature"]
    args.resume = True
    args.direction = signature["direction"]
    args.memory_model = signature["memory_model"]
    args.source_attempt = signature["source_attempt"]
    args.capabilities = signature.get("capabilities")
    args.chapter_token_budget = signature.get("chapter_token_budget", 150_000)
    args.auto_until = signature.get("auto_until")
    args.memory_max_tokens = signature.get("memory_max_tokens", 8192)
    args.target_chars = signature.get("chapter_target_chars", 5000)
    args.replacements = signature.get("replacements")
    args.edited_batch = signature.get("edited_batch")
    if signature["kind"] == "memory":
        await cmd_memory_retry(args)
    elif signature["kind"] == "preview":
        await cmd_preview(args)
    elif signature["kind"] == "revision":
        await cmd_revise(args)
    elif signature["kind"] == "memory_edit":
        await cmd_memory_edit(args)
    else:
        raise ValueError("Unsupported checkpoint kind")


async def cmd_audit(args):
    from sqlalchemy import select, func
    from app.models import Chapter, ChapterCommit, StorySnapshot, Event
    from app.planning.provenance import compute_story_state_hash
    run_dir = RUNS_ROOT / args.run
    state = _load_state(run_dir)
    session, generation, _, engine = _build_services(run_dir, state["provider"], state.get("model", ""))
    try:
        counts = {model.__tablename__: await session.scalar(select(func.count()).select_from(model).where(model.novel_id == uuid.UUID(state["novel_id"])))
                  for model in (Chapter, ChapterCommit, StorySnapshot, Event)}
        current = await compute_story_state_hash(session, uuid.UUID(state["novel_id"]))
        print(json.dumps({"counts": counts, "story_state_hash": current,
                          "state_file_matches": current == state["story_state_hash"]}, indent=2))
    finally:
        await _close_services(session, generation, engine)


def _read_candidate(run_dir, chapter, attempt_name):
    from app.commit.models import ChapterPreviewPackage
    from app.services.trial_artifacts import safe_name
    path = run_dir / f"chapter_{chapter:02d}"
    if attempt_name:
        path = path / "attempts" / safe_name(attempt_name)
    package = ChapterPreviewPackage.model_validate_json((path / "preview_package.json").read_text())
    _check_package(package)
    return path, package


async def cmd_inspect(args):
    """Offline author handoff: no key, database connection or model call."""
    from app.services.author_review import export_author_review
    run_dir = RUNS_ROOT / args.run
    state = _load_state(run_dir)
    chapter = args.chapter or state.get("last_chapter") or state["next_chapter"]
    path, package = _read_candidate(run_dir, chapter, args.attempt)
    report = export_author_review(package, path / "reading")
    print(f"Author review: {report}")
    print(f"Scene copies: {report.parent / 'scenes'}")
    print("Edit a scene copy, then run revise with --scene-id and --text-file; no approval is granted.")


async def cmd_revise(args):
    """Keep the original plan and untouched scenes; revalidate an edited candidate."""
    from app.services.trial_artifacts import safe_name
    run_dir = RUNS_ROOT / safe_name(args.run)
    state = _load_state(run_dir)
    args.chapter = args.chapter or state.get("last_chapter") or state["next_chapter"]
    _, source = _read_candidate(run_dir, args.chapter, args.source_attempt)
    if getattr(args, "replacements", None) is None:
        args.replacements = {args.scene_id: Path(args.text_file).read_text(encoding="utf-8")}
    _configure(run_dir, state["provider"], state.get("model", ""), None, require_key=True)
    session, generation, _, engine = _build_services(
        run_dir, state["provider"], state.get("model", ""), chapter=args.chapter,
        memory_model=getattr(args, "memory_model", None),
        capabilities=getattr(args, "capabilities", None),
        chapter_token_budget=getattr(args, "chapter_token_budget", 150_000),
        auto_until=getattr(args, "auto_until", None),
        memory_max_tokens=getattr(args, "memory_max_tokens", 8192),
    )
    try:
        attempt = await _open_attempt(args, run_dir, state, session, kind="revision",
                                      source_package=source)
        if (attempt.path / "preview_package.json").exists():
            print(f"Completed checkpoint retained: {attempt.path}")
            return
        try:
            package = await generation.revise_preview(source, args.replacements, checkpoints=attempt)
            attempt.save_package(package)
            print(f"Revision saved: {attempt.path}")
            print(f"memory valid={package.memory_result.validation_report.valid}; "
                  f"dry-run applicable={bool(package.memory_result.dry_run and package.memory_result.dry_run.applicable)}")
            print("New review.json is unapproved. Original draft and unrelated scenes are retained.")
        except BaseException as exc:
            _save_failure(attempt, exc)
            raise
    finally:
        await _close_services(session, generation, engine)


async def cmd_memory_edit(args):
    """Validate an author's memory corrections offline; never invoke an extractor."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool
    from app.memory.models import MemoryUpdateBatch, MemoryUpdateBatchV3
    from app.services.memory_extraction import MemoryExtractionService, MemoryExtractionResult
    from app.services.trial_artifacts import safe_name

    run_dir = RUNS_ROOT / safe_name(args.run)
    state = _load_state(run_dir)
    args.chapter = args.chapter or state.get("last_chapter") or state["next_chapter"]
    _, source = _read_candidate(run_dir, args.chapter, args.source_attempt)
    if not source.continuity_report.valid:
        raise ValueError("Fix the draft's continuity issues before editing its memory")
    if getattr(args, "edited_batch", None) is None:
        args.edited_batch = json.loads(Path(args.batch_file).read_text(encoding="utf-8"))
    batch = MemoryUpdateBatch.model_validate(MemoryUpdateBatchV3.model_validate(args.edited_batch).model_dump())
    engine = create_async_engine(f"sqlite+aiosqlite:///{run_dir / 'trial.db'}", poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            attempt = await _open_attempt(args, run_dir, state, session, kind="memory_edit",
                                          source_package=source)
            if (attempt.path / "preview_package.json").exists():
                print(f"Completed checkpoint retained: {attempt.path}")
                return
            try:
                memory = MemoryExtractionService(session, require_evidence_refs=True)
                result = await attempt.stage("memory", MemoryExtractionResult, lambda: memory.validate_batch(
                    uuid.UUID(source.novel_id), source.draft, source.plan_result.plan, batch, dry_run=True,
                ))
                revised = source.model_copy(deep=True, update={"memory_result": result, "generated_at": datetime.now(UTC)})
                revised.preview_package_hash = revised.compute_hash(
                    plan=revised.plan_result.plan, draft=revised.draft,
                    continuity_report=revised.continuity_report, memory_result=result,
                    story_state_hash=revised.story_state_hash,
                )
                attempt.save_package(revised)
                print(f"Memory edit saved: {attempt.path}")
                print(f"valid={result.validation_report.valid}; dry-run applicable={bool(result.dry_run and result.dry_run.applicable)}")
                print("No API calls or canonical writes. Review the new candidate before finalize.")
            except BaseException as exc:
                _save_failure(attempt, exc)
                raise
    finally:
        await engine.dispose()

async def cmd_init(args) -> None:
    run_id = getattr(args, "run", None) or (
        datetime.now(UTC).strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    )
    run_dir = RUNS_ROOT / run_id
    if run_dir.exists():
        sys.exit(f"ERROR: run directory already exists: {run_dir}")
    run_dir.mkdir(parents=True, exist_ok=False)
    _configure(run_dir, args.provider, args.model, args.base_url)

    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{run_dir / 'trial.db'}", poolclass=NullPool
    )
    session, generation, exporter, _ = _build_services(
        run_dir, args.provider, args.model, engine
    )
    try:
        await _create_schema(engine)
        novel_id, state = await _init_world(session)
    finally:
        await session.invalidate()
        await engine.dispose()
        import gc
        gc.collect()  # run finalizers while the loop is still alive

    from app.planning.provenance import hash_state_dict

    (run_dir / "before_state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    _save_state(run_dir, {
        "run_id": run_id,
        "novel_id": novel_id,
        "provider": args.provider,
        "model": args.model,
        "base_url": os.environ.get("NOVEL_LLM_BASE_URL", "https://api.scnet.cn/api/llm/v1"),
        "story_state_hash": hash_state_dict(state),
        "next_chapter": 1,
        "committed_chapters": [],
    })
    print(f"run initialized: {run_dir}")
    print(f"novel_id: {novel_id}")
    print(f"story_state_hash: {hash_state_dict(state)[:16]}...")


async def _do_preview(args, run_dir, state, session, generation, exporter) -> None:
    chapter_number = args.chapter or state["next_chapter"]
    direction = args.direction or CHAPTER_DIRECTIONS.get(chapter_number)
    if not direction:
        raise ValueError("Provide --direction for this chapter")
    attempt = await _open_attempt(args, run_dir, state, session, kind="preview",
                                  direction=direction)
    if (attempt.path / "preview_package.json").exists():
        print(f"Completed checkpoint retained: {attempt.path}")
        return
    try:
        package = await generation.generate_preview(
            uuid.UUID(state["novel_id"]), direction, chapter_number=chapter_number,
            checkpoints=attempt,
        )
        attempt.save_package(package)
        _write_review_template(attempt.path, chapter_number, package, {})
        print(f"preview generated: chapter {chapter_number}, attempt={attempt.path.name}")
        print(f"memory valid={package.memory_result.validation_report.valid}")
        print(f"artifacts: {attempt.path}")
    except BaseException as exc:
        _save_failure(attempt, exc)
        raise

async def cmd_preview(args) -> None:
    run_dir = RUNS_ROOT / args.run
    state = _load_state(run_dir)
    _configure(
        run_dir, state["provider"], state.get("model", ""), None,
        require_key=True,
    )
    session, generation, exporter, engine = _build_services(
        run_dir, state["provider"], state.get("model", ""),
        chapter=args.chapter or state["next_chapter"],
        capabilities=getattr(args, "capabilities", None),
        chapter_target_chars=getattr(args, "target_chars", 5000),
        chapter_token_budget=getattr(args, "chapter_token_budget", 150_000),
        memory_max_tokens=getattr(args, "memory_max_tokens", 8192),
    )
    try:
        await _do_preview(args, run_dir, state, session, generation, exporter)
    finally:
        await _close_services(session, generation, engine)


async def cmd_memory_retry(args) -> None:
    from app.commit.models import ChapterPreviewPackage
    from app.services.memory_extraction import MemoryExtractionResult
    from app.services.trial_artifacts import safe_name
    run_dir = RUNS_ROOT / safe_name(args.run)
    state = _load_state(run_dir)
    chapter_number = args.chapter or state.get("last_chapter") or state["next_chapter"]
    args.chapter = chapter_number
    _configure(run_dir, state["provider"], state.get("model", ""), None, require_key=True)
    session, generation, _exporter, engine = _build_services(
        run_dir, state["provider"], state.get("model", ""),
        chapter=chapter_number, memory_model=getattr(args, "memory_model", None),
        capabilities=getattr(args, "capabilities", None),
        chapter_token_budget=getattr(args, "chapter_token_budget", 150_000),
        auto_until=getattr(args, "auto_until", None),
        memory_max_tokens=getattr(args, "memory_max_tokens", 8192),
    )
    try:
        chapter_dir = run_dir / f"chapter_{chapter_number:02d}"
        source = getattr(args, "source_attempt", None)
        source_path = (chapter_dir / "attempts" / safe_name(source) / "preview_package.json"
                       if source else chapter_dir / "preview_package.json")
        package = ChapterPreviewPackage.model_validate_json(source_path.read_text())
        _check_package(package)
        attempt = await _open_attempt(args, run_dir, state, session, kind="memory",
                                      source_package=package)
        if (attempt.path / "preview_package.json").exists():
            print(f"Completed checkpoint retained: {attempt.path}")
            return
        try:
            result = await attempt.stage("memory", MemoryExtractionResult, lambda:
                generation.memory_service.extract_preview(
                    uuid.UUID(state["novel_id"]), package.draft, package.plan_result.plan,
                    package.continuity_report, scene_plans=list(package.draft.scene_plans),
                ))
            updated = package.model_copy(deep=True, update={"memory_result": result})
            updated.preview_package_hash = ChapterPreviewPackage.compute_hash(
                plan=updated.plan_result.plan, draft=updated.draft,
                continuity_report=updated.continuity_report, memory_result=result,
                story_state_hash=updated.story_state_hash,
            )
            attempt.save_package(updated)
            print(f"memory retry completed: chapter {chapter_number}, attempt={attempt.path.name}")
            print(f"memory ops: {len(result.batch.operations)} valid={result.validation_report.valid}")
            print(f"dry-run applicable: {bool(result.dry_run and result.dry_run.applicable)}")
            print(f"artifacts: {attempt.path}")
        except BaseException as exc:
            _save_failure(attempt, exc)
            raise
    finally:
        await _close_services(session, generation, engine)

async def _do_finalize(args, run_dir, state, session, generation) -> None:
    from app.commit.models import ChapterPreviewPackage, ReviewApproval
    from app.services.trial_artifacts import safe_name, write_new
    chapter_number = args.chapter or state.get("last_chapter") or state["next_chapter"]
    if not getattr(args, "attempt", None) or not getattr(args, "approval", None):
        raise ValueError("finalize requires --attempt and --approval; no implicit approval")
    attempt_dir = run_dir / f"chapter_{chapter_number:02d}" / "attempts" / safe_name(args.attempt)
    package = ChapterPreviewPackage.model_validate_json((attempt_dir / "preview_package.json").read_text())
    _check_package(package)
    receipt = ReviewApproval.model_validate_json(Path(args.approval).read_text())
    result = await generation.finalize(package, attempt_id=args.attempt, approval=receipt)
    suffix = "duplicate_" + uuid.uuid4().hex if result.duplicate else "commit"
    write_new(attempt_dir / f"{suffix}.json", {
        "result": result.model_dump(mode="json"), "approval": receipt.model_dump(mode="json"),
    })
    from app.planning.provenance import compute_story_state_hash
    current = await compute_story_state_hash(session, uuid.UUID(state["novel_id"]))
    # Recover a state-file write interrupted AFTER the DB committed. Never
    # rewind a run when an older chapter is re-finalized after later commits.
    if current == result.after_story_state_hash:
        state["story_state_hash"] = result.after_story_state_hash
        state["next_chapter"] = chapter_number + 1
        state["last_chapter"] = chapter_number
        if chapter_number not in state["committed_chapters"]:
            state["committed_chapters"].append(chapter_number)
        _save_state(run_dir, state)
    print(f"chapter {chapter_number}: commit={result.commit_id}, duplicate={result.duplicate}")
    print(f"after_state_hash: {result.after_story_state_hash}")

async def cmd_finalize(args) -> None:
    run_dir = RUNS_ROOT / args.run
    state = _load_state(run_dir)
    _configure(
        run_dir, state["provider"], state.get("model", ""), None,
        require_key=False,
    )
    session, generation, _exporter, engine = _build_services(
        run_dir, state["provider"], state.get("model", "")
    )
    try:
        await _do_finalize(args, run_dir, state, session, generation)
    finally:
        await session.close()
        await engine.dispose()


async def cmd_state(args) -> None:
    run_dir = RUNS_ROOT / args.run
    state = _load_state(run_dir)
    _configure(
        run_dir, state["provider"], state.get("model", ""), None,
        require_key=False,
    )
    session, _generation, _e, engine = _build_services(
        run_dir, state["provider"], state.get("model", "")
    )
    try:
        from app.planning.provenance import build_story_state_projection, hash_state_dict

        projection = await build_story_state_projection(
            session, uuid.UUID(state["novel_id"])
        )
        out = run_dir / "after_state.json"
        out.write_text(
            json.dumps(projection, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        print(f"current story_state_hash: {hash_state_dict(projection)[:16]}...")
        print(f"state written: {out}")
    finally:
        await session.invalidate()
        await engine.dispose()
        import gc
        gc.collect()


async def cmd_report(args) -> None:
    run_dir = RUNS_ROOT / args.run
    state = _load_state(run_dir)
    report = [
        "# Real-LLM Trial Report",
        "",
        "## Real-LLM Trial Configuration",
        f"- run_id: {state['run_id']}",
        f"- provider/model: {state['provider']} / {state.get('model', 'n/a')}",
        "",
        "## Novel Setup",
        "见 before_state.json 与 WORLD_SPEC（scripts/real_llm_trial.py）。",
        "",
    ]
    for chapter in sorted(state["committed_chapters"]):
        chapter_dir = run_dir / f"chapter_{chapter:02d}"
        review = chapter_dir / f"chapter_{chapter:02d}_review.md"
        report.append(f"## Chapter {chapter} Review")
        report.append(
            "（人工评价填写于 "
            f"{review.name}；以下为客观数据摘要）"
        )
        metadata = chapter_dir / "metadata.json"
        if metadata.exists():
            m = json.loads(metadata.read_text(encoding="utf-8"))
            report.append(f"- scenes: {m['scene_count']}")
        commit = chapter_dir / "commit.json"
        if commit.exists():
            c = json.loads(commit.read_text(encoding="utf-8"))
            report.append(f"- commit_id: {c['commit_id']}")
        report.append("")
    report += [
        "## Writing Quality",
        "（汇总三章主观打分——由人类填写）",
        "",
        "## Director Quality / Scene Planner Quality / Continuity Results / "
        "Repair Quality / Memory Extraction Results / Retrieval / Secret Isolation",
        "（逐章 review 汇总）",
        "",
        "## Token / API Usage",
        "（metadata.json 中 generation_metadata.usage 汇总——由人类或后续脚本填写）",
        "",
        "## Canonical State After 3 Chapters",
        "见 after_state.json，与 before_state.json 人工对比。",
        "",
        "## World Reconstruction Test",
        "仅读 after_state.json + events + knowledge + summaries，回答：能否还原三章剧情？",
        "",
        "## Fourth-Chapter Readiness",
        "构建 ch4 Director/WriterContext 人工检查 Context 是否足够。",
        "",
        "## Bugs Found / Quality Problems Found / Highest-Priority Improvements",
        "（人类填写）",
        "",
        "## Recommended Next Phase",
        "根据质量瓶颈选择：Coherence / Arc Planning / Memory / Calibration / Style / UI",
    ]
    out = run_dir / "TRIAL_REPORT.md"
    out.write_text("\n".join(report), encoding="utf-8")
    print(f"report template written: {out}")


def _install_exit_noise_filter() -> None:
    """SQLAlchemy 2.0 + aiosqlite known shutdown race: a connection finalized
    after the event loop closed logs "Exception terminating connection"
    (plus a SAWarning). Harmless (exit code 0, data correct) — silenced for
    the dev runner only; production code is unaffected."""
    import logging
    import warnings as _warnings

    class _ExitNoiseFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            return "terminating connection" not in record.getMessage()

    for pool_logger in ("sqlalchemy.pool.impl.NullPool",
                        "sqlalchemy.pool.impl.QueuePool",
                        "sqlalchemy.pool.Manager",
                        "sqlalchemy.pool"):
        logging.getLogger(pool_logger).addFilter(_ExitNoiseFilter())

    from sqlalchemy import exc as sa_exc

    _warnings.filterwarnings(
        "ignore",
        message="The garbage collector is trying to clean up*",
        category=sa_exc.SAWarning,
    )


def main() -> None:
    import logging

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    _install_exit_noise_filter()

    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init")
    p_init.add_argument("--run", default=None,
                        help="custom run id (default: auto-generated timestamp)")
    p_init.add_argument("--provider", choices=["real", "fake"], default="real")
    p_init.add_argument("--model", default="DeepSeek-V4.1-Flash-1017", help="fixed model for all roles")
    p_init.add_argument("--base-url", default=None)

    p_prev = sub.add_parser("preview")
    p_prev.add_argument("--run", required=True)
    p_prev.add_argument("--chapter", type=int, default=None)
    p_prev.add_argument("--direction", default=None)
    p_prev.add_argument("--attempt", default=None)
    p_prev.add_argument("--capabilities", default=None)
    p_prev.add_argument("--target-chars", type=int, choices=range(1000, 10801), metavar="1000..10800", default=5000)
    p_prev.add_argument("--chapter-token-budget", type=int, default=150_000)
    p_prev.add_argument("--memory-max-tokens", type=int, default=8192)

    p_fin = sub.add_parser("finalize")
    p_fin.add_argument("--run", required=True)
    p_fin.add_argument("--chapter", type=int, default=None)
    p_fin.add_argument("--attempt", required=True)
    p_fin.add_argument("--approval", required=True)

    p_retry = sub.add_parser("memory-retry")
    p_retry.add_argument("--run", required=True)
    p_retry.add_argument("--chapter", type=int, default=None)
    p_retry.add_argument("--memory-model", default=None)
    p_retry.add_argument("--attempt", default=None)
    p_retry.add_argument("--source-attempt", default=None)
    p_retry.add_argument("--capabilities", default=None)
    p_retry.add_argument("--chapter-token-budget", type=int, default=150_000,
                         help="Explicit authorized chapter cap; existing usage is retained")
    p_retry.add_argument("--auto-until", default=None, help="Timezone-aware cutoff for new Auto HTTP requests")
    p_retry.add_argument("--memory-max-tokens", type=int, default=8192)

    p_resume = sub.add_parser("resume")
    p_resume.add_argument("--run", required=True)
    p_resume.add_argument("--chapter", type=int, required=True)
    p_resume.add_argument("--attempt", required=True)
    p_inspect = sub.add_parser("inspect", help="Export local reading copies; no LLM calls")
    p_inspect.add_argument("--run", required=True)
    p_inspect.add_argument("--chapter", type=int, default=None)
    p_inspect.add_argument("--attempt", default=None)
    p_revise = sub.add_parser("revise", help="Replace one scene and rerun Continuity/Memory, without replanning")
    p_revise.add_argument("--run", required=True)
    p_revise.add_argument("--chapter", type=int, default=None)
    p_revise.add_argument("--source-attempt", required=True)
    p_revise.add_argument("--attempt", required=True)
    p_revise.add_argument("--scene-id", required=True)
    p_revise.add_argument("--text-file", required=True)
    p_revise.add_argument("--capabilities", default=None)
    p_revise.add_argument("--memory-model", default=None)
    p_revise.add_argument("--memory-max-tokens", type=int, default=8192)
    p_revise.add_argument("--chapter-token-budget", type=int, default=150_000)
    p_revise.add_argument("--auto-until", default=None)
    p_edit = sub.add_parser("memory-edit", help="Revalidate edited memory offline; no LLM calls")
    p_edit.add_argument("--run", required=True)
    p_edit.add_argument("--chapter", type=int, default=None)
    p_edit.add_argument("--source-attempt", required=True)
    p_edit.add_argument("--attempt", required=True)
    p_edit.add_argument("--batch-file", required=True)
    p_audit = sub.add_parser("audit")
    p_audit.add_argument("--run", required=True)

    p_state = sub.add_parser("state")
    p_state.add_argument("--run", required=True)

    p_report = sub.add_parser("report")
    p_report.add_argument("--run", required=True)

    args = parser.parse_args()
    from app.services.trial_artifacts import safe_name
    if getattr(args, "run", None):
        safe_name(args.run)
    commands = {
        "init": cmd_init, "preview": cmd_preview, "memory-retry": cmd_memory_retry,
        "finalize": cmd_finalize,
        "state": cmd_state, "report": cmd_report,
        "resume": cmd_resume, "audit": cmd_audit,
        "inspect": cmd_inspect, "revise": cmd_revise,
        "memory-edit": cmd_memory_edit,
    }
    # One owner per run: no concurrent preview/commit/budget state races.
    import fcntl
    RUNS_ROOT.mkdir(parents=True, exist_ok=True)
    with (RUNS_ROOT / f".{getattr(args, 'run', None) or 'init'}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        asyncio.run(commands[args.command](args))


if __name__ == "__main__":
    main()
