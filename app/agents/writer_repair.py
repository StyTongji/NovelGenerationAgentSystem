"""WriterRepairAgent: targeted repair of a single scene (§31-§33).

Consumes ONLY writer-safe data: the original scene prose, truth-free repair
instructions, required outcomes, continuity excerpts. Never the
ContinuityContext or author explanations.
"""

from __future__ import annotations

import hashlib

from pydantic import BaseModel, Field

from app.continuity.models import ContinuityRepairInstruction
from app.context.models import ChapterGuidance, ConstraintView
from app.draft.models import SceneDraft
from app.llm.client import AgentLLMClient
from app.llm.models import LLMMessage, TextRequest
from app.schemas.agent_outputs import WriterSafeScenePlan

PROMPT_NAME = "writer_repair"
PROMPT_VERSION = "writer_repair_v1"


class WriterRepairContext(BaseModel):
    """Everything the repair writer may see. No author_explanation, no
    secret truth, no director notes, no ContinuityContext."""

    safe_scene_plan: WriterSafeScenePlan
    original_text: str
    # True when original_text contains <REDACTED_PROTECTED_INFORMATION>
    # markers; the prompt then explains how to treat them
    contains_redactions: bool = False
    repair_instructions: list[ContinuityRepairInstruction] = Field(default_factory=list)
    chapter_guidance: ChapterGuidance | None = None
    hard_constraints: list[ConstraintView] = Field(default_factory=list)
    previous_scene_ending: str | None = None
    next_scene_opening: str | None = None
    target_chars: int = 800


class WriterRepairAgent:
    def __init__(self, llm: AgentLLMClient, prompt_registry) -> None:
        self.llm = llm
        self.prompts = prompt_registry

    async def repair_scene(self, context: WriterRepairContext) -> SceneDraft:
        system = self.prompts.get(PROMPT_NAME).content
        user_prompt = self._build_prompt(context)
        request = TextRequest(
            model=self.llm.model,
            temperature=self.llm.temperature,
            max_tokens=self.llm.max_tokens,
            metadata={"agent": "writer_repair", "prompt_version": PROMPT_VERSION},
            system=system,
            messages=[LLMMessage(role="user", content=user_prompt)],
        )
        result = await self.llm.run_request(request)
        text = _guard(result.text)
        return SceneDraft(
            scene_id=context.safe_scene_plan.scene_id,
            scene_index=context.safe_scene_plan.scene_index,
            text=text,
            generation_metadata=result.metadata,
            context_hash=hashlib.sha256(
                (context.original_text + user_prompt).encode("utf-8")
            ).hexdigest(),
        )

    def _build_prompt(self, context: WriterRepairContext) -> str:
        sections: list[str] = []
        sections.append("## ORIGINAL SCENE PROSE")
        if context.contains_redactions:
            sections.append(
                "- 注意：原文中的 <REDACTED_PROTECTED_INFORMATION> 是被移除的受保护信息，"
                "不要试图还原它；按修订指令以不确定/暗示的方式重写相关句子。"
            )
        sections.append("<<<SCENE")
        sections.append(context.original_text)
        sections.append("SCENE>>>")

        sections.append("## REPAIR INSTRUCTIONS (fix ONLY these; writer-safe)")
        for instruction in context.repair_instructions:
            sections.append(f"- [{instruction.category}] {instruction.instruction}")

        if any(i.preserve for i in context.repair_instructions):
            sections.append("## MUST PRESERVE (keep these plot points intact)")
            preserved = {p for i in context.repair_instructions for p in i.preserve}
            for item in sorted(preserved):
                sections.append(f"- {item}")

        plan = context.safe_scene_plan
        sections.append("## SCENE PLAN (unchanged)")
        sections.append(f"- goal: {plan.scene_goal}")
        if plan.required_outcomes:
            sections.append(f"- required outcomes: {'；'.join(plan.required_outcomes)}")

        if context.chapter_guidance is not None:
            guidance = context.chapter_guidance
            if guidance.restrictions:
                sections.append("## CHAPTER RESTRICTIONS (unchanged)")
                for restriction in guidance.restrictions:
                    sections.append(f"- {restriction}")

        sections.append("## HARD CONSTRAINTS (absolute)")
        for constraint in context.hard_constraints:
            sections.append(f"- {constraint.statement}")
        if not context.hard_constraints:
            sections.append("- (none)")

        continuity = []
        if context.previous_scene_ending:
            continuity.append("### previous scene ending\n" + context.previous_scene_ending)
        if context.next_scene_opening:
            continuity.append("### next scene opening\n" + context.next_scene_opening)
        if continuity:
            sections.append("## CONTINUITY")
            sections.extend(continuity)

        sections.append(
            f"## OUTPUT\n返回修复后的完整场景正文，约 {context.target_chars} 字，"
            "无标题、无解释、无 diff。"
        )
        return "\n\n".join(sections)


def _guard(raw: str) -> str:
    text = raw.strip()
    for prefix in ("以下是修订后的正文：", "以下是正文：", "修订后正文：", "正文："):
        if text.startswith(prefix):
            text = text[len(prefix):].lstrip()
    if not text:
        raise ValueError("Repair writer returned empty prose")
    return text
