"""Independent literary reviewer with exact scene-local evidence."""

from __future__ import annotations

import json

from app.draft.models import ChapterDraft
from app.llm.client import AgentLLMClient
from app.llm.prompts import PromptRegistry
from app.quality.models import LiteraryAssessment, LiteraryQualityReport
from app.quality.review_assistance import ReviewAssistanceAssessment
from app.schemas.agent_outputs import ChapterPlan
from app.services.canonical_commit import draft_hash_of

PROMPT_NAME = "literary_reviewer"
PROMPT_VERSION = "literary_reviewer_v1"


class LiteraryReviewerAgent:
    """Read-only advisory role. No database access and no automatic rewrite."""

    def __init__(self, llm: AgentLLMClient, prompt_registry: PromptRegistry) -> None:
        self.llm = llm
        self.prompts = prompt_registry

    async def review(self, draft: ChapterDraft, plan: ChapterPlan) -> LiteraryQualityReport:
        template = self.prompts.get(PROMPT_NAME, "v1")
        user = json.dumps({
            "chapter_plan": plan.model_dump(mode="json", exclude_none=True),
            "chapter_number": draft.chapter_number,
            "scenes": [{"scene_id": s.scene_id, "text": s.text} for s in draft.scenes],
        }, ensure_ascii=False)
        result = await self.llm.generate_structured(
            system=template.content, user=user, response_model=LiteraryAssessment,
            max_repair_attempts=2,
            metadata={"agent": "literary_reviewer", "prompt_version": PROMPT_VERSION},
        )
        assessment = result.data
        scenes = {scene.scene_id: scene.text for scene in draft.scenes}
        for finding in assessment.findings:
            if finding.scene_id not in scenes:
                raise ValueError(f"Literary review references unknown scene: {finding.scene_id}")
            if finding.exact_quote not in scenes[finding.scene_id]:
                raise ValueError(f"Literary review evidence is not in scene: {finding.scene_id}")
        return LiteraryQualityReport(
            **assessment.model_dump(), draft_hash=draft_hash_of(draft),
            prompt_version=PROMPT_VERSION, generation_metadata=result.metadata,
        )

    async def assist_review(self, package, before_state: dict):
        """Suggest form values while keeping semantic approval human-owned."""
        template = self.prompts.get("author_review", "v1")
        result = await self.llm.generate_structured(
            system=template.content,
            user=json.dumps({
                "scenes": [{"scene_id": s.scene_id, "text": s.text} for s in package.draft.scenes],
                "memory_batch": package.memory_result.batch.model_dump(mode="json", exclude_none=True),
                "memory_validation": package.memory_result.validation_report.model_dump(mode="json"),
                "dry_run_applicable": bool(package.memory_result.dry_run and package.memory_result.dry_run.applicable),
                "canonical_before_state": before_state,
            }, ensure_ascii=False),
            response_model=ReviewAssistanceAssessment, max_repair_attempts=2,
            metadata={"agent": "literary_reviewer", "prompt_version": "author_review_v1"},
        )
        ids = [suggestion.operation_id for suggestion in result.data.operation_reviews]
        expected = {op.operation_id for op in package.memory_result.batch.operations}
        if len(ids) != len(set(ids)) or set(ids) != expected:
            raise ValueError("审阅助手未完整核对当前候选的记忆操作，请重新生成建议。")
        return result.data, result.metadata
