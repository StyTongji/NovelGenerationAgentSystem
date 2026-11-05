"""ChapterPlanningService: preview-only plan generation entry point.

    user direction -> ContextBuilder -> DirectorContext -> DirectorAgent
        -> validated ChapterPlan + report + provenance -> PREVIEW

Guarantee: no canonical state is written. The canonical database is only
touched (read) by ContextBuilder and the validator.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.director import DirectorAgent
from app.context.builder import ContextBuilder
from app.llm.client import AgentLLMClient
from app.llm.prompts import get_default_registry
from app.planning.models import PlanGenerationResult, PlanValidationReport
from app.planning.validator import ChapterPlanValidator
from app.schemas.agent_outputs import ChapterPlan


class ChapterPlanningService:
    def __init__(self, session: AsyncSession, director: DirectorAgent | None = None) -> None:
        self.session = session
        self._director = director
        self.builder = ContextBuilder(session)

    def _director_agent(self) -> DirectorAgent:
        if self._director is None:
            raise RuntimeError(
                "DirectorAgent not configured: build one from the LLM registry "
                "(see app.api.deps) before generating plans"
            )
        return self._director

    async def generate_preview(
        self,
        novel_id: uuid.UUID,
        user_direction: str | None,
        *,
        chapter_number: int | None = None,
    ) -> PlanGenerationResult:
        novel = await self.session.get(__import__("app.models", fromlist=["Novel"]).Novel, novel_id)
        if novel is None:
            raise ValueError(f"Novel {novel_id} not found")
        number = chapter_number or novel.current_chapter_number + 1

        context = await self.builder.build_director_context(
            novel_id, chapter_number=number, user_direction=user_direction
        )
        result = await self._director_agent().generate_plan(context)
        # provenance: context_hash is the shared STORY STATE hash so that
        # planning / drafting / commit all agree on "based on state X"
        from app.planning.provenance import compute_story_state_hash

        return result.model_copy(
            update={"context_hash": await compute_story_state_hash(self.session, novel_id)}
        )

    async def validate_plan(
        self, novel_id: uuid.UUID, plan: ChapterPlan, *, chapter_number: int | None = None
    ) -> PlanValidationReport:
        """Re-validate a (possibly user-edited) plan against fresh state."""
        novel = await self.session.get(__import__("app.models", fromlist=["Novel"]).Novel, novel_id)
        if novel is None:
            raise ValueError(f"Novel {novel_id} not found")
        number = chapter_number or plan.chapter_number or novel.current_chapter_number + 1
        context = await self.builder.build_director_context(
            novel_id, chapter_number=number, user_direction=None
        )
        validator = ChapterPlanValidator(self.session)
        return await validator.validate(plan, context)


def build_director_agent(session: AsyncSession, llm_client: AgentLLMClient) -> DirectorAgent:
    """Assemble a DirectorAgent from an AgentLLMClient (registry-built)."""
    return DirectorAgent(
        llm=llm_client,
        prompt_registry=get_default_registry(),
        validator=ChapterPlanValidator(session),
    )
