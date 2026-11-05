"""ChapterDraftService: validated plan -> scene plans -> per-scene prose
-> ChapterDraft PREVIEW.

Pipeline (PIPELINE.md Step 5, preview-only):

    validate plan (never trust the client)
      -> to_writer_safe()
      -> ScenePlannerAgent (validated, bounded repair)
      -> for each scene:
             resolve POV
             build_writer_context (per-scene knowledge firewall)
             WriterAgent.write_scene
             update ChapterWorkingMemory (draft-local, NOT canonical)
      -> deterministic merge -> ChapterDraft(status="preview")

Canonical Story State is never written. If a scene fails, a
ChapterDraftGenerationError carries the completed portion and the database
is still untouched.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.scene_planner import ScenePlannerAgent
from app.agents.writer import WriterAgent
from app.context.builder import ContextBuilder
from app.context.models import ChapterGuidance, TemporaryEntityBrief
from app.draft.models import (
    ChapterDraft,
    ChapterDraftGenerationError,
    ChapterWorkingMemory,
    StalePlanError,
)
from app.planning.models import PlanGenerationResult
from app.planning.validator import ChapterPlanValidator
from app.schemas.agent_outputs import ChapterPlan, WriterSafeScenePlan
from app.services.chapter_planning import ChapterPlanningService


def _tail(text: str, max_chars: int) -> str:
    return text if len(text) <= max_chars else text[-max_chars:]


class ChapterDraftService:
    def __init__(
        self,
        session: AsyncSession,
        scene_planner: ScenePlannerAgent,
        writer: WriterAgent,
        *,
        previous_scene_tail_chars: int = 1500,
    ) -> None:
        self.session = session
        self.scene_planner = scene_planner
        self.writer = writer
        self.previous_scene_tail_chars = previous_scene_tail_chars
        self.builder = ContextBuilder(session)

    # ------------------------------------------------------------------ api

    async def generate_preview(
        self,
        novel_id: uuid.UUID,
        plan: ChapterPlan,
        *,
        expected_context_hash: str | None = None,
        checkpoints=None,
    ) -> ChapterDraft:
        """Generate a full chapter draft preview from a (possibly user-edited)
        plan. The plan is re-validated against fresh canonical state; the
        client's claim of validity is never trusted."""
        number = plan.chapter_number or 1
        context = await self.builder.build_director_context(
            novel_id, chapter_number=number, user_direction=None
        )
        validator = ChapterPlanValidator(self.session)
        report = await validator.validate(plan, context)
        if not report.valid:
            raise ChapterDraftGenerationError(
                "Plan is invalid against current canonical state: "
                + "; ".join(f"{i.code}: {i.message}" for i in report.errors()),
                completed_scene_drafts=[],
                failed_scene_id=None,
                generation_metadata=[],
            )

        from app.planning.provenance import compute_story_state_hash

        current_hash = await compute_story_state_hash(self.session, novel_id)
        if expected_context_hash is not None and expected_context_hash != current_hash:
            raise StalePlanError(
                "Plan was generated against a different story state "
                f"(expected {expected_context_hash[:12]}..., current {current_hash[:12]}...)"
            )

        safe_plan = plan.to_writer_safe()

        async def plan_scenes():
            return await self.scene_planner.plan_scenes(safe_plan, novel_id, number, author_plan=plan)
        planning = await checkpoints.stage("scene_plan", PlanGenerationResult, plan_scenes) if checkpoints else await plan_scenes()
        if not planning.validation_report.valid:
            raise ChapterDraftGenerationError(
                "Scene planning failed validation after bounded repairs: "
                + "; ".join(
                    f"{i.code}: {i.message}" for i in planning.validation_report.errors()
                ),
                completed_scene_drafts=[],
                failed_scene_id=None,
                generation_metadata=[planning.metadata] if planning.metadata else [],
            )
        batch = planning.plan  # ScenePlanBatch
        scene_planning_metadata = planning.metadata

        memory = ChapterWorkingMemory(chapter_number=number)
        memory.register_temporary_entities(plan.proposed_entities)

        chapter_guidance = ChapterGuidance(
            chapter_goal=safe_plan.chapter_goal,
            chapter_role=safe_plan.chapter_role,
            pacing=safe_plan.pacing,
            emotional_direction=safe_plan.emotional_direction,
            restrictions=[r.instruction for r in safe_plan.restrictions],
            expected_state_changes=list(safe_plan.expected_state_changes),
        )

        scene_drafts: list = []
        metadata: list = []
        for scene in batch.scenes:
            safe_scene: WriterSafeScenePlan = scene.to_writer_safe()
            pov_id = self._resolve_pov(scene, plan)
            if pov_id is None:
                raise ChapterDraftGenerationError(
                    f"Scene {scene.scene_index} POV cannot be resolved to a canonical character",
                    completed_scene_drafts=scene_drafts,
                    failed_scene_id=scene.scene_id,
                    generation_metadata=metadata,
                )
            try:
                writer_context = await self.builder.build_writer_context(
                    novel_id,
                    chapter_number=number,
                    pov_character_id=pov_id,
                    safe_scene_plan=safe_scene,
                    scene_index=scene.scene_index,
                    previous_scene_ending=memory.previous_scene_ending,
                    chapter_guidance=chapter_guidance,
                    writer_instruction=self._writer_instruction(safe_plan),
                    temporary_entities=[
                        TemporaryEntityBrief(
                            temporary_ref=info.temporary_ref,
                            entity_type=info.entity_type,
                            name_hint=info.name_hint,
                            role=info.role,
                            introduced_in_scene=info.introduced_in_scene,
                        )
                        for info in memory.temporary_entities.values()
                    ],
                    retrieval_hint=scene.scene_goal,
                )
                from app.draft.models import SceneDraft
                async def write_scene():
                    return await self.writer.write_scene(writer_context)
                draft = await checkpoints.stage(f"scene_{scene.scene_index}", SceneDraft, write_scene) if checkpoints else await write_scene()
            except ChapterDraftGenerationError:
                raise
            except Exception as exc:
                raise ChapterDraftGenerationError(
                    f"Scene {scene.scene_index} ({scene.scene_id}) failed: {exc}",
                    completed_scene_drafts=scene_drafts,
                    failed_scene_id=scene.scene_id,
                    generation_metadata=metadata,
                ) from exc

            scene_drafts.append(draft)
            if draft.generation_metadata is not None:
                metadata.append(draft.generation_metadata)
            memory.mark_scene_completed(
                safe_scene,
                _tail(draft.text, self.previous_scene_tail_chars),
            )

        return ChapterDraft(
            novel_id=str(novel_id),
            chapter_number=number,
            title=None,
            scenes=scene_drafts,
            # author-level scene plans: required by the continuity checker
            # (per-scene POV / narrative_mode); never writer-visible
            scene_plans=list(batch.scenes),
            full_text="\n\n".join(draft.text for draft in scene_drafts),
            chapter_plan_hash=hashlib.sha256(
                plan.model_dump_json().encode("utf-8")
            ).hexdigest(),
            story_context_hash=current_hash,
            generation_metadata=([scene_planning_metadata] if scene_planning_metadata else [])
            + metadata,
            status="preview",
            temporary_entities=list(memory.temporary_entities.values()),
            created_at=datetime.now(UTC),
        )

    async def generate_from_direction(
        self,
        novel_id: uuid.UUID,
        user_direction: str | None,
        *,
        planning_service: ChapterPlanningService | None = None,
    ) -> tuple[PlanGenerationResult, ChapterDraft]:
        """Convenience path: plan preview -> draft preview. Still no commit."""
        service = planning_service
        if service is None:
            raise RuntimeError(
                "ChapterPlanningService with a configured DirectorAgent is required "
                "for generate_from_direction"
            )
        planning = await service.generate_preview(novel_id, user_direction)
        draft = await self.generate_preview(novel_id, planning.plan)
        return planning, draft

    # ------------------------------------------------------------- internals

    @staticmethod
    def _resolve_pov(scene, plan: ChapterPlan) -> uuid.UUID | None:
        if scene.pov.character_id:
            return uuid.UUID(scene.pov.character_id)
        return None  # POV must be a canonical character (validator enforces)

    @staticmethod
    def _writer_instruction(safe_plan) -> str | None:
        """Writer-safe instruction derived from the safe plan — never the
        raw author direction."""
        parts: list[str] = []
        if safe_plan.soft_preferences:
            parts.append("作者偏好：" + "；".join(safe_plan.soft_preferences))
        if safe_plan.ending_hook:
            parts.append(f"本章结尾钩子方向：{safe_plan.ending_hook}")
        return "\n".join(parts) if parts else None
