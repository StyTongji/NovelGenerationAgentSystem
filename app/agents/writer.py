"""WriterAgent: writes one scene from a WriterContext.

Boundary rules:
- consumes ONLY the WriterContext (POV knowledge firewalled upstream) and
  the WriterSafeScenePlan; never repositories, never the raw ChapterPlan
- uses generate_text (prose), not generate_structured
- output guard strips scaffolding and rejects non-prose responses
"""

from __future__ import annotations

import hashlib

from app.context.models import WriterContext
from app.draft.models import SceneDraft
from app.llm.client import AgentLLMClient
from app.llm.errors import LLMError
from app.llm.models import LLMMessage, TextRequest

PROMPT_NAME = "writer"
PROMPT_VERSION = "writer_v2"

_MAX_PROSE_CHARS = 30000


class WriterOutputError(LLMError):
    """The model returned something that is not usable scene prose."""


class WriterAgent:
    def __init__(self, llm: AgentLLMClient, prompt_registry, *, max_chars: int = 6000) -> None:
        self.llm = llm
        self.prompts = prompt_registry
        self.max_chars = max_chars

    async def write_scene(self, context: WriterContext) -> SceneDraft:
        system = self.prompts.get(PROMPT_NAME).content
        user_prompt = self._build_user_prompt(context)
        request = TextRequest(
            model=self.llm.model,
            temperature=self.llm.temperature,
            max_tokens=self.llm.max_tokens,
            metadata={"agent": "writer", "prompt_version": PROMPT_VERSION},
            system=system,
            messages=[LLMMessage(role="user", content=user_prompt)],
        )
        result = await self.llm.run_request(request)
        text = self._guard(result.text)

        context_hash = hashlib.sha256(context.model_dump_json().encode("utf-8")).hexdigest()
        return SceneDraft(
            scene_id=context.safe_scene_plan.scene_id if context.safe_scene_plan else f"scene-{context.scene_index}",
            scene_index=context.scene_index or 0,
            text=text,
            generation_metadata=result.metadata,
            context_hash=context_hash,
        )

    # ------------------------------------------------------------- guard

    def _guard(self, raw: str) -> str:
        text = raw.strip()
        # strip markdown fences if the model added them
        if text.startswith("```"):
            lines = text.splitlines()
            if len(lines) >= 2:
                lines = lines[1:]
                if lines and lines[-1].strip().startswith("```"):
                    lines = lines[:-1]
                text = "\n".join(lines).strip()
        # strip common scaffolding prefixes
        for prefix in ("以下是正文：", "以下是正文:", "以下是本场景正文：", "正文：", "【正文】"):
            if text.startswith(prefix):
                text = text[len(prefix):].lstrip()
        if not text:
            raise WriterOutputError("Writer returned empty prose")
        if text.startswith("{") or text.startswith("["):
            raise WriterOutputError("Writer returned JSON instead of prose")
        if len(text) > _MAX_PROSE_CHARS:
            raise WriterOutputError(
                f"Writer output exceeds maximum length ({len(text)} chars)"
            )
        return text

    # ------------------------------------------------------------ prompt

    def _build_user_prompt(self, context: WriterContext) -> str:
        sections: list[str] = []

        sections.append("## CURRENT STORY STATE")
        pov = context.pov_character
        state = pov.state
        pov_line = f"- POV: {pov.name}"
        if state:
            pov_line += (
                f"（{state.alive_status}"
                + (f", {state.realm}" + (f" {state.subrealm}" if state.subrealm else "") if state.realm else "")
                + (f", 位于 {state.location}" if state.location else "")
                + (f", 身体: {state.physical_condition}" if state.physical_condition else "")
                + "）"
            )
        sections.append(pov_line)
        if context.story_time:
            sections.append(f"- story time: {context.story_time}")
        if context.location:
            sections.append(f"- location: {context.location.name}")
            if context.location.description:
                sections.append(f"  {context.location.description}")

        sections.append("## POV KNOWLEDGE (everything this POV may know or believe)")
        for fact in context.pov_knowledge:
            sections.append(f"- {fact.content}")

        sections.append("## VISIBLE CHARACTERS")
        for view in context.visible_characters:
            line = f"- {view.name}"
            if view.state and view.state.realm:
                line += f"（{view.state.realm}"
                if view.state.subrealm:
                    line += f" {view.state.subrealm}"
                line += ")"
            if view.personality_traits:
                line += f" 性格: {'、'.join(view.personality_traits)}"
            sections.append(line)

        if context.relevant_relationships:
            sections.append("## RELEVANT RELATIONSHIPS")
            for rel in context.relevant_relationships:
                sections.append(
                    f"- {rel.source_name} -> {rel.target_name}: {rel.type}"
                    f" (trust {rel.trust})"
                    + (f"；{rel.public_description}" if rel.public_description else "")
                )

        if context.relevant_items:
            sections.append("## RELEVANT ITEMS")
            for item in context.relevant_items:
                sections.append(f"- {item.name}（{item.grade or item.type or 'item'}，status={item.status}）")

        if context.relevant_power_rules:
            sections.append("## POWER STATE")
            for rule in context.relevant_power_rules:
                sections.append(f"- {rule}")

        if context.relevant_events:
            sections.append("## RELEVANT PAST EVENTS")
            for event in context.relevant_events:
                sections.append(f"- (ch{event.chapter_number}) {event.summary}")

        sections.append("## SCENE PLAN")
        if context.safe_scene_plan is not None:
            plan = context.safe_scene_plan
            sections.append(f"- scene {plan.scene_index} goal: {plan.scene_goal}")
            if plan.conflict:
                sections.append(f"- conflict: {plan.conflict}")
            if plan.opening_state:
                sections.append(f"- opening state: {plan.opening_state}")
            if plan.required_actions:
                sections.append("- required actions: " + "；".join(plan.required_actions))
            if plan.required_outcomes:
                sections.append("- required outcomes: " + "；".join(plan.required_outcomes))
            if plan.allowed_information:
                sections.append("- allowed information: " + "；".join(plan.allowed_information))
            if plan.allowed_hints:
                sections.append("- allowed hints (may be hinted, never confirmed): " + "；".join(plan.allowed_hints))
            if plan.emotional_target:
                sections.append(f"- emotional target: {plan.emotional_target}")
            sections.append(f"- target length: about {plan.target_chars} Chinese characters")
            if plan.transition_in:
                sections.append(f"- transition in: {plan.transition_in}")
            if plan.transition_out:
                sections.append(f"- transition out: {plan.transition_out}")
            if plan.temporary_entities:
                sections.append("- temporary entities (keep descriptions consistent):")
                for ref in plan.temporary_entities:
                    info = next(
                        (e for e in context.temporary_entities if e.temporary_ref == ref), None
                    )
                    if info is not None:
                        introduced = (
                            f"已在 scene {info.introduced_in_scene} 介绍过"
                            if info.introduced_in_scene
                            else "本场景首次出场，需要介绍"
                        )
                        sections.append(
                            f"  - {ref}: {info.name_hint or '?'}（{info.role or info.entity_type}，{introduced}）"
                        )
                    else:
                        sections.append(f"  - {ref}")

        if context.chapter_guidance is not None:
            guidance = context.chapter_guidance
            sections.append("## CHAPTER GUIDANCE (writer-safe)")
            if guidance.chapter_goal:
                sections.append(f"- chapter goal: {guidance.chapter_goal}")
            if guidance.emotional_direction:
                sections.append(f"- emotional direction: {guidance.emotional_direction}")
            if guidance.restrictions:
                sections.append("- restrictions (apply to restricted topics; never state them as fact):")
                for item in guidance.restrictions:
                    sections.append(f"  - {item}")
            if guidance.expected_state_changes:
                sections.append("- expected state changes: " + "；".join(guidance.expected_state_changes))

        continuity: list[str] = []
        if context.recent_context.previous_chapter_ending:
            continuity.append("### end of previous chapter\n" + context.recent_context.previous_chapter_ending)
        if context.previous_scene_ending:
            continuity.append("### end of previous scene\n" + context.previous_scene_ending)
        if context.writer_instruction:
            continuity.append("### author guidance (writer-safe)\n" + context.writer_instruction)
        if continuity:
            sections.append("## CONTINUITY")
            sections.extend(continuity)

        sections.append("## HARD CONSTRAINTS (absolute)")
        for constraint in context.hard_constraints:
            sections.append(f"- {constraint.statement}")
        if not context.hard_constraints:
            sections.append("- (none)")

        sections.append("## OUTPUT RULES")
        sections.append(
            "- 只输出本场景中文正文；无标题、无解释、无 JSON、无元叙述。"
        )
        return "\n\n".join(sections)
