"""API dependency wiring for LLM-backed agents.

The registry resolves provider/model from settings; business code never
mentions a vendor. Tests override these dependencies with FakeLLM-backed
agents.
"""

from __future__ import annotations

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.director import DirectorAgent
from app.agents.scene_planner import ScenePlannerAgent
from app.agents.writer import WriterAgent
from app.config import get_settings
from app.db import get_session
from app.llm.prompts import get_default_registry
from app.llm.registry import build_registry
from app.planning.scene_validator import ScenePlanValidator
from app.services.chapter_draft import ChapterDraftService
from app.services.chapter_planning import ChapterPlanningService, build_director_agent


def get_llm_registry():
    return build_registry(get_settings())


def get_director_agent(
    session: AsyncSession = Depends(get_session),
    registry=Depends(get_llm_registry),
) -> DirectorAgent:
    settings = get_settings()
    client = registry.client_for_agent("director", settings, prompt_version="director_v2")
    return build_director_agent(session, client)


def get_chapter_planning_service(
    session: AsyncSession = Depends(get_session),
    director: DirectorAgent = Depends(get_director_agent),
) -> ChapterPlanningService:
    return ChapterPlanningService(session, director=director)


def get_scene_planner(
    session: AsyncSession = Depends(get_session),
    registry=Depends(get_llm_registry),
) -> ScenePlannerAgent:
    settings = get_settings()
    client = registry.client_for_agent("scene_planner", settings, prompt_version="scene_planner_v1")
    return ScenePlannerAgent(
        llm=client,
        prompt_registry=get_default_registry(),
        validator=ScenePlanValidator(session),
        max_plan_repairs=settings.generation_max_scene_plan_repairs,
        scene_min_chars=settings.generation_scene_min_chars,
        scene_max_chars=settings.generation_scene_max_chars,
        chapter_target_chars=settings.generation_chapter_target_chars,
    )


def get_writer(
    session: AsyncSession = Depends(get_session),
    registry=Depends(get_llm_registry),
) -> WriterAgent:
    settings = get_settings()
    client = registry.client_for_agent("writer", settings, prompt_version="writer_v2")
    return WriterAgent(llm=client, prompt_registry=get_default_registry())


def get_chapter_draft_service(
    session: AsyncSession = Depends(get_session),
    scene_planner: ScenePlannerAgent = Depends(get_scene_planner),
    writer: WriterAgent = Depends(get_writer),
) -> ChapterDraftService:
    settings = get_settings()
    return ChapterDraftService(
        session,
        scene_planner,
        writer,
        previous_scene_tail_chars=settings.generation_previous_scene_tail_chars,
    )


def get_llm_continuity_checker(
    session: AsyncSession = Depends(get_session),
    registry=Depends(get_llm_registry),
):
    from app.agents.continuity_checker import LLMContinuityChecker, PROMPT_VERSION as continuity_prompt_version

    settings = get_settings()
    if settings.continuity_mode == "deterministic_only":
        return None
    client = registry.client_for_agent(
        "continuity_checker", settings, prompt_version=continuity_prompt_version
    )
    return LLMContinuityChecker(llm=client, prompt_registry=get_default_registry())


def get_writer_repair_agent(
    session: AsyncSession = Depends(get_session),
    registry=Depends(get_llm_registry),
):
    from app.agents.writer_repair import WriterRepairAgent

    settings = get_settings()
    client = registry.client_for_agent("writer", settings, prompt_version="writer_repair_v1")
    return WriterRepairAgent(llm=client, prompt_registry=get_default_registry())


def get_continuity_service(
    session: AsyncSession = Depends(get_session),
    llm_checker=Depends(get_llm_continuity_checker),
    repair_agent=Depends(get_writer_repair_agent),
):
    from app.services.continuity import ContinuityService

    settings = get_settings()
    return ContinuityService(
        session,
        llm_checker,
        repair_agent,
        max_repairs=settings.continuity_max_repairs,
        mode=settings.continuity_mode,
    )


def get_memory_extractor_agent(
    session: AsyncSession = Depends(get_session),
    registry=Depends(get_llm_registry),
):
    from app.agents.memory_extractor import MemoryExtractorAgent

    settings = get_settings()
    client = registry.client_for_agent(
        "memory_extractor", settings, prompt_version="memory_extractor_v3"
    )
    return MemoryExtractorAgent(llm=client, prompt_registry=get_default_registry())


def get_memory_extraction_service(
    session: AsyncSession = Depends(get_session),
    extractor=Depends(get_memory_extractor_agent),
):
    from app.services.memory_extraction import MemoryExtractionService

    return MemoryExtractionService(session, extractor, require_evidence_refs=True)


def get_chapter_generation_service(
    session: AsyncSession = Depends(get_session),
    registry=Depends(get_llm_registry),
):
    return build_chapter_generation_service(session, registry, get_settings())


def build_chapter_generation_service(session, registry, settings):
    """Shared construction for HTTP requests and durable Studio jobs."""
    from app.agents.memory_extractor import MemoryExtractorAgent
    from app.agents.scene_planner import ScenePlannerAgent
    from app.agents.writer import WriterAgent
    from app.agents.writer_repair import WriterRepairAgent
    from app.planning.scene_validator import ScenePlanValidator
    from app.services.canonical_commit import CanonicalCommitService
    from app.services.chapter_draft import ChapterDraftService
    from app.services.chapter_generation import ChapterGenerationService
    from app.services.continuity import ContinuityService
    from app.services.memory_extraction import MemoryExtractionService

    prompts = get_default_registry()

    director = build_director_agent(
        session,
        registry.client_for_agent("director", settings, prompt_version="director_v2"),
    )
    scene_planner = ScenePlannerAgent(
        llm=registry.client_for_agent("scene_planner", settings, prompt_version="scene_planner_v1"),
        prompt_registry=prompts,
        validator=ScenePlanValidator(session),
        max_plan_repairs=settings.generation_max_scene_plan_repairs,
        scene_min_chars=settings.generation_scene_min_chars,
        scene_max_chars=settings.generation_scene_max_chars,
        chapter_target_chars=settings.generation_chapter_target_chars,
    )
    writer = WriterAgent(
        llm=registry.client_for_agent("writer", settings, prompt_version="writer_v2"),
        prompt_registry=prompts,
    )
    repair = WriterRepairAgent(
        llm=registry.client_for_agent("writer_repair" if "writer_repair" in settings.llm.agents else "writer", settings, prompt_version="writer_repair_v1"),
        prompt_registry=prompts,
    )
    llm_checker = None
    if settings.continuity_mode == "hybrid":
        from app.agents.continuity_checker import LLMContinuityChecker, PROMPT_VERSION as continuity_prompt_version

        llm_checker = LLMContinuityChecker(
            llm=registry.client_for_agent(
                "continuity_checker", settings, prompt_version=continuity_prompt_version
            ),
            prompt_registry=prompts,
        )
    extractor = MemoryExtractorAgent(
        llm=registry.client_for_agent(
            "memory_extractor", settings, prompt_version="memory_extractor_v3"
        ),
        prompt_registry=prompts,
    )

    return ChapterGenerationService(
        session,
        planning_service=ChapterPlanningService(session, director=director),
        draft_service=ChapterDraftService(
            session, scene_planner, writer,
            previous_scene_tail_chars=settings.generation_previous_scene_tail_chars,
        ),
        continuity_service=ContinuityService(
            session, llm_checker=llm_checker, repair_agent=repair,
            max_repairs=settings.continuity_max_repairs, mode=settings.continuity_mode,
        ),
        memory_service=MemoryExtractionService(
            session, extractor, require_evidence_refs=True
        ),
        commit_service=CanonicalCommitService(session),
    )
