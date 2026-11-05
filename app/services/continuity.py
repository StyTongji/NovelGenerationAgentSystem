"""ContinuityService: hybrid check + bounded targeted repair (preview-only).

    ChapterDraft + ChapterPlan
        -> ContinuityContext (author-level, relevance filtered)
        -> DeterministicContinuityChecker
        -> LLMContinuityChecker (optional, hybrid mode)
        -> ContinuityIssueMerger -> ContinuityReport (draft_hash bound)
        -> if blocking: WriterSafeRepairInstruction -> targeted scene repair
        -> re-check (bounded, max 2 repair rounds)

Zero database writes at every step.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.continuity_checker import LLMContinuityChecker, draft_hash
from app.agents.writer_repair import WriterRepairAgent, WriterRepairContext
from app.continuity.deterministic import DeterministicContinuityChecker
from app.continuity.merger import ContinuityIssueMerger
from app.continuity.models import (
    BLOCKING_SEVERITIES,
    ContinuityRepairRecord,
    ContinuityRepairResult,
    ContinuityReport,
)
from app.continuity.repair_projection import project_issue_to_writer_safe
from app.continuity.repair_redaction import redact_scene_for_repair
from app.context.builder import ContextBuilder
from app.context.models import ChapterGuidance
from app.draft.models import ChapterDraft
from app.models.enums import Severity
from app.schemas.agent_outputs import ChapterPlan, ScenePlan


class ContinuityService:
    def __init__(
        self,
        session: AsyncSession,
        llm_checker: LLMContinuityChecker | None = None,
        repair_agent: WriterRepairAgent | None = None,
        *,
        max_repairs: int = 2,
        mode: str = "hybrid",  # deterministic_only | hybrid
    ) -> None:
        self.session = session
        self.llm_checker = llm_checker
        self.repair_agent = repair_agent
        self.max_repairs = max_repairs
        self.mode = mode
        self.builder = ContextBuilder(session)
        self.deterministic = DeterministicContinuityChecker()
        self.merger = ContinuityIssueMerger()

    # ---------------------------------------------------------------- check

    async def check_draft(
        self,
        novel_id: uuid.UUID,
        draft: ChapterDraft,
        chapter_plan: ChapterPlan,
        *,
        scene_plans: list[ScenePlan] | None = None,
    ) -> ContinuityReport:
        """Server-side re-check; client continuity claims are ignored (§43)."""
        context = await self.builder.build_continuity_context(
            novel_id,
            chapter_number=draft.chapter_number,
            chapter_text=draft.full_text,
            chapter_plan=chapter_plan,
            scene_plans=scene_plans if scene_plans is not None else list(draft.scene_plans),
            draft=draft,
        )
        self._last_secret_statements = context.secret_fact_statements
        deterministic_issues = self.deterministic.check(context)

        llm_issues = []
        metadata = None
        if self.mode == "hybrid" and self.llm_checker is not None:
            llm_issues, metadata = await self.llm_checker.check(context)

        issues = self.merger.merge(deterministic_issues, llm_issues)
        blocking = [i for i in issues if i.blocking]
        highest = max(
            (i.severity for i in issues),
            key=lambda s: {"info": 0, "warning": 1, "error": 2, "critical": 3}[s.value],
            default=None,
        )
        return ContinuityReport(
            valid=not blocking,
            issues=issues,
            highest_severity=highest,
            blocking_issue_count=len(blocking),
            deterministic_issue_count=len(deterministic_issues),
            llm_issue_count=len(llm_issues),
            checked_scene_ids=[s.scene_id for s in draft.scenes],
            context_hash=context.context_hash,
            draft_hash=draft_hash(draft),
        )

    # --------------------------------------------------------------- repair

    async def repair_preview(
        self,
        novel_id: uuid.UUID,
        draft: ChapterDraft,
        chapter_plan: ChapterPlan,
        *,
        scene_plans: list[ScenePlan] | None = None,
    ) -> ContinuityRepairResult:
        """Bounded targeted repair. Only error/critical issues trigger
        repair; warnings are preserved for the user (§36)."""
        if self.repair_agent is None:
            raise RuntimeError("WriterRepairAgent not configured for repair_preview")

        working = draft.model_copy(deep=True)
        plans = scene_plans if scene_plans is not None else list(working.scene_plans)
        plans_by_id = {sp.scene_id: sp for sp in plans}
        history: list[ContinuityRepairRecord] = []
        all_metadata = []
        exhausted = False

        report = await self.check_draft(novel_id, working, chapter_plan, scene_plans=plans)

        for round_index in range(1, self.max_repairs + 1):
            blocking = [
                i for i in report.issues
                if i.severity in BLOCKING_SEVERITIES and i.scene_id
            ]
            if not blocking:
                break

            # group blocking issues by scene
            by_scene: dict[str, list] = {}
            for issue in blocking:
                by_scene.setdefault(issue.scene_id, []).append(issue)

            repaired_ids: list[str] = []
            for scene_id, issues in by_scene.items():
                scene_draft = next(
                    (s for s in working.scenes if s.scene_id == scene_id), None
                )
                scene_plan = plans_by_id.get(scene_id)
                if scene_draft is None:
                    continue
                instructions = [
                    project_issue_to_writer_safe(issue, scene_plan) for issue in issues
                ]
                # Phase 6 Fix A: redact leak evidence before it reaches the
                # repair writer (the flawed prose itself must not carry the
                # secret into a fresh prompt)
                protected = list(self._last_secret_statements or [])
                redacted = redact_scene_for_repair(
                    scene_id, scene_draft.text, issues, protected
                )
                repair_context = WriterRepairContext(
                    safe_scene_plan=scene_plan.to_writer_safe(),
                    original_text=redacted.redacted_text,
                    contains_redactions=redacted.redacted_any,
                    repair_instructions=instructions,
                    chapter_guidance=self._guidance_from_plan(chapter_plan),
                    hard_constraints=self._writer_visible_constraints(chapter_plan),
                    previous_scene_ending=self._previous_ending(working, scene_id),
                    next_scene_opening=self._next_opening(working, scene_id),
                    target_chars=scene_plan.target_chars if scene_plan else 800,
                )
                repaired = await self.repair_agent.repair_scene(repair_context)
                if repaired.generation_metadata is not None:
                    all_metadata.append(repaired.generation_metadata)
                # replace only the affected scene (§54)
                working.scenes = [
                    repaired if s.scene_id == scene_id else s for s in working.scenes
                ]
                working.full_text = "\n\n".join(s.text for s in working.scenes)
                repaired_ids.append(scene_id)

            report = await self.check_draft(novel_id, working, chapter_plan, scene_plans=plans)
            history.append(
                ContinuityRepairRecord(
                    round_index=round_index,
                    scene_ids_repaired=repaired_ids,
                    issues_before=len(blocking),
                    issues_after=report.blocking_issue_count,
                    metadata=[],
                )
            )

        exhausted = report.blocking_issue_count > 0

        result = ContinuityRepairResult(
            draft=working,
            report=report,
            repair_history=history,
            exhausted=exhausted,
        )
        result.generation_metadata = all_metadata  # type: ignore[attr-defined]
        return result

    # ------------------------------------------------------------ internals

    @staticmethod
    def _guidance_from_plan(plan: ChapterPlan) -> ChapterGuidance:
        safe = plan.to_writer_safe()
        return ChapterGuidance(
            chapter_goal=safe.chapter_goal,
            chapter_role=safe.chapter_role,
            pacing=safe.pacing,
            emotional_direction=safe.emotional_direction,
            restrictions=[r.instruction for r in safe.restrictions],
            expected_state_changes=list(safe.expected_state_changes),
        )

    @staticmethod
    def _writer_visible_constraints(plan: ChapterPlan):
        from app.context.models import ConstraintView

        return [
            ConstraintView(id="", type="hard", scope="novel", statement=s)
            for s in plan.hard_constraints
        ]

    @staticmethod
    def _previous_ending(draft: ChapterDraft, scene_id: str) -> str | None:
        for index, scene in enumerate(draft.scenes):
            if scene.scene_id == scene_id and index > 0:
                text = draft.scenes[index - 1].text
                return text[-400:]
        return None

    @staticmethod
    def _next_opening(draft: ChapterDraft, scene_id: str) -> str | None:
        for index, scene in enumerate(draft.scenes):
            if scene.scene_id == scene_id and index + 1 < len(draft.scenes):
                return draft.scenes[index + 1].text[:200]
        return None
