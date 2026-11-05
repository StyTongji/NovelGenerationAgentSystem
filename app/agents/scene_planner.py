"""ScenePlannerAgent: expands a WriterSafePlan into a ScenePlanBatch.

Permission model: the planner consumes ONLY the WriterSafePlan (never
DirectorContext, never planning secrets). Author-level reasoning already
happened in the Director; this layer just compiles chapter-level intent
into scene-level executable plans.

Repair is split: LLM structured repair (Phase 2 mechanism) handles
malformed JSON; domain repair re-plans when ScenePlanValidator rejects.
"""

from __future__ import annotations

import hashlib

from app.llm.client import AgentLLMClient, agent_operation
from app.llm.models import GenerationMetadata, LLMMessage, StructuredRequest
from app.llm.prompts import PromptRegistry
from app.planning.models import PlanGenerationResult
from app.planning.scene_validator import ScenePlanValidator
from app.schemas.agent_outputs import ScenePlanBatch, WriterSafePlan

PROMPT_NAME = "scene_planner"
PROMPT_VERSION = "scene_planner_v1"


class ScenePlanningResult(PlanGenerationResult):
    """Result shape reused: plan holds the ScenePlanBatch."""


class ScenePlannerAgent:
    def __init__(
        self,
        llm: AgentLLMClient,
        prompt_registry: PromptRegistry,
        validator: ScenePlanValidator,
        *,
        max_plan_repairs: int = 2,
        scene_min_chars: int = 500,
        scene_max_chars: int = 1800,
        chapter_target_chars: int = 5000,
    ) -> None:
        self.llm = llm
        self.prompts = prompt_registry
        self.validator = validator
        self.max_plan_repairs = max_plan_repairs
        self.scene_min_chars = scene_min_chars
        self.scene_max_chars = scene_max_chars
        self.chapter_target_chars = chapter_target_chars

    @agent_operation
    async def plan_scenes(
        self,
        safe_plan: WriterSafePlan,
        novel_id,
        chapter_number: int,
        *,
        author_plan=None,
    ) -> PlanGenerationResult:
        """``author_plan`` is used ONLY by the domain validator (reveal-scope
        escalation); the LLM prompt still sees the writer-safe plan alone."""
        system = self.prompts.get(PROMPT_NAME).content
        user_prompt = self._build_prompt(safe_plan)

        batch, report, metadata = await self._produce(system, user_prompt, safe_plan, novel_id, chapter_number, author_plan)
        repair_count = 0
        while not report.valid and repair_count < self.max_plan_repairs:
            repair_count += 1
            batch, report, metadata = await self._repair(
                system, user_prompt, batch, report, safe_plan, novel_id, chapter_number, author_plan
            )

        return PlanGenerationResult(
            plan=batch,  # ScenePlanBatch rides in the plan field
            writer_safe_plan=safe_plan,
            validation_report=report,
            metadata=metadata,
            repair_count=repair_count,
            context_hash=self._batch_hash(batch),
            based_on_chapter_number=chapter_number,
            prompt_version=PROMPT_VERSION,
        )

    # -------------------------------------------------------------- internals

    async def _produce(self, system, user_prompt, safe_plan, novel_id, chapter_number, author_plan=None):
        request = self._request(system, user_prompt)
        result = await self.llm.run_request(request)
        batch = self._coerce(result.data)
        report = await self.validator.validate(batch, safe_plan, novel_id, author_plan)
        return batch, report, result.metadata

    async def _repair(self, system, user_prompt, batch, report, safe_plan, novel_id, chapter_number, author_plan=None):
        issues_text = "\n".join(
            f"- [{i.severity.value}] {i.code}: {i.message}"
            + (f" | fix: {i.suggested_fix}" if i.suggested_fix else "")
            for i in report.errors()
        )
        repair_prompt = (
            f"{user_prompt}\n\n"
            "## PREVIOUS SCENE PLAN (invalid)\n"
            f"```json\n{batch.model_dump_json(exclude_none=True)}\n```\n\n"
            "## VALIDATION ISSUES TO FIX\n"
            f"{issues_text}\n\n"
            "Rewrite the scene plan fixing every issue. Respond with a single "
            "valid ScenePlanBatch JSON object."
        )
        request = self._request(system, repair_prompt, include_schema=False)
        result = await self.llm.run_request(request)
        batch = self._coerce(result.data)
        new_report = await self.validator.validate(batch, safe_plan, novel_id, author_plan)
        return batch, new_report, result.metadata

    def _request(self, system: str, user: str, *, include_schema: bool = True) -> StructuredRequest:
        return StructuredRequest(
            system=system,
            messages=[LLMMessage(role="user", content=user)],
            response_model=ScenePlanBatch,
            max_repair_attempts=2,
            metadata={"agent": "scene_planner", "prompt_version": PROMPT_VERSION},
            temperature=self.llm.temperature,
            model=self.llm.model,
            max_tokens=self.llm.max_tokens,
            include_json_schema_hint=include_schema,
        )

    @staticmethod
    def _coerce(data) -> ScenePlanBatch:
        if isinstance(data, ScenePlanBatch):
            return data
        return ScenePlanBatch.model_validate(data)

    @staticmethod
    def _batch_hash(batch: ScenePlanBatch) -> str:
        return hashlib.sha256(batch.model_dump_json().encode("utf-8")).hexdigest()

    def _build_prompt(self, safe_plan: WriterSafePlan) -> str:
        import json

        lines = ["## WRITER-SAFE CHAPTER PLAN (the only information you have)"]
        lines.append("```json")
        lines.append(safe_plan.model_dump_json(exclude_none=True))
        lines.append("```")
        lines.append("## SIZING BUDGET")
        lines.append(
            f"- chapter target: about {self.chapter_target_chars} Chinese characters total"
        )
        lines.append(
            f"- per-scene target_chars between {self.scene_min_chars} and {self.scene_max_chars}"
        )
        lines.append(
            "- scenes in the plan must cover every chapter beat and sum roughly to the chapter target"
        )
        lines.append("## OUTPUT")
        lines.append("One ScenePlanBatch JSON object matching the schema. No prose.")
        return "\n".join(lines)
