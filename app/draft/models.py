"""Chapter draft models: working memory, scene drafts, chapter draft.

Naming convention (documented, enforced by review):

- **Canonical Story State**  — persisted, accepted world truth
- **ChapterWorkingMemory**   — draft-local, in-memory continuity data for
  the current generation run; discarded if the draft is rejected

Nothing in this module writes to the database. Phase 5 is preview-only.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.llm.models import GenerationMetadata
from app.schemas.agent_outputs import ProposedEntity, ScenePlan, WriterSafeScenePlan


class TemporaryEntityInfo(BaseModel):
    """Draft-local registration of a proposed (not yet canonical) entity."""

    temporary_ref: str
    entity_type: str
    name_hint: str | None = None
    role: str | None = None
    writer_description: str | None = None
    introduced_in_scene: str | None = None


class ChapterWorkingMemory(BaseModel):
    """In-memory continuity for one draft generation run.

    NOT canonical state: no CharacterState / Event / PlotThread updates
    happen here. If the draft is discarded, this object is simply dropped.
    """

    chapter_number: int
    completed_scene_ids: list[str] = Field(default_factory=list)
    previous_scene_ending: str | None = None
    # deterministic continuity: planned outcomes of completed scenes
    accumulated_outcomes: list[str] = Field(default_factory=list)
    scene_summaries: list[str] = Field(default_factory=list)
    temporary_entities: dict[str, TemporaryEntityInfo] = Field(default_factory=dict)

    def register_temporary_entities(self, proposed: list[ProposedEntity]) -> None:
        for entity in proposed:
            self.temporary_entities.setdefault(
                entity.temporary_ref,
                TemporaryEntityInfo(
                    temporary_ref=entity.temporary_ref,
                    entity_type=entity.entity_type,
                    name_hint=entity.name_hint,
                    role=entity.role,
                ),
            )

    def mark_scene_completed(
        self, scene: WriterSafeScenePlan, ending_excerpt: str | None
    ) -> None:
        self.completed_scene_ids.append(scene.scene_id)
        self.accumulated_outcomes.extend(scene.required_outcomes)
        self.scene_summaries.append(
            f"Scene {scene.scene_index}: {scene.scene_goal}"
            + (f"（结果：{'；'.join(scene.required_outcomes)}）" if scene.required_outcomes else "")
        )
        if ending_excerpt:
            self.previous_scene_ending = ending_excerpt
        for ref in scene.temporary_entities:
            info = self.temporary_entities.get(ref)
            if info is not None and info.introduced_in_scene is None:
                info.introduced_in_scene = scene.scene_id


class SceneDraft(BaseModel):
    scene_id: str
    scene_index: int
    text: str
    generation_metadata: GenerationMetadata | None = None
    context_hash: str = ""


class ChapterDraft(BaseModel):
    novel_id: str
    chapter_number: int
    title: str | None = None
    scenes: list[SceneDraft] = Field(default_factory=list)
    full_text: str
    chapter_plan_hash: str
    story_context_hash: str
    generation_metadata: list[GenerationMetadata] = Field(default_factory=list)
    # author-level scene plans (checker provenance; never writer-visible)
    scene_plans: list[ScenePlan] = Field(default_factory=list)
    status: str = "preview"
    temporary_entities: list[TemporaryEntityInfo] = Field(default_factory=list)
    created_at: datetime


class ChapterDraftGenerationError(Exception):
    """A scene failed mid-generation. Carries the completed portion; the
    canonical database remains untouched."""

    def __init__(
        self,
        message: str,
        *,
        completed_scene_drafts: list[SceneDraft],
        failed_scene_id: str | None,
        generation_metadata: list[GenerationMetadata],
    ) -> None:
        super().__init__(message)
        self.completed_scene_drafts = completed_scene_drafts
        self.failed_scene_id = failed_scene_id
        self.generation_metadata = generation_metadata


class StalePlanError(Exception):
    """The plan's provenance does not match the current story state."""
