"""ScenePlanValidator: light-weight domain validation of a ScenePlanBatch.

Distinct from ChapterPlanValidator: the batch was expanded from an already
validated, writer-safe plan, so the checks here are structural plus a
re-verification of entity references and reveal-scope escalation.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import Severity
from app.planning.models import PlanIssue, PlanValidationReport
from app.repositories.character import CharacterRepository
from app.repositories.world import LocationRepository
from app.schemas.agent_outputs import ScenePlanBatch, WriterSafePlan


class ScenePlanValidator:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.characters = CharacterRepository(session)
        self.locations = LocationRepository(session)

    async def validate(
        self,
        batch: ScenePlanBatch,
        safe_plan: WriterSafePlan,
        novel_id: uuid.UUID,
        author_plan=None,
    ) -> PlanValidationReport:
        issues: list[PlanIssue] = []
        issues += self._check_structure(batch, safe_plan)
        issues += await self._check_references(batch, safe_plan, novel_id)
        issues += self._check_reveal_scope(batch, safe_plan, author_plan)
        valid = not any(i.severity in (Severity.error, Severity.critical) for i in issues)
        issues.sort(key=lambda i: (i.severity.value, i.code, i.message))
        return PlanValidationReport(valid=valid, issues=issues)

    # ------------------------------------------------------------ structure

    def _check_structure(self, batch: ScenePlanBatch, safe_plan: WriterSafePlan) -> list[PlanIssue]:
        issues: list[PlanIssue] = []
        scene_ids = [s.scene_id for s in batch.scenes]
        if len(set(scene_ids)) != len(scene_ids):
            issues.append(self._issue("duplicate_scene_id", Severity.error, "scene_id values must be unique"))

        expected_indices = list(range(1, len(batch.scenes) + 1))
        actual_indices = [s.scene_index for s in batch.scenes]
        if actual_indices != expected_indices:
            issues.append(self._issue(
                "scene_index_gap", Severity.error,
                f"scene_index must be contiguous from 1, got {actual_indices}",
            ))

        planned_beats = {str(scene.index) for scene in safe_plan.scenes}
        covered = {beat for s in batch.scenes for beat in s.source_beat_ids}
        missing = planned_beats - covered
        if missing:
            issues.append(self._issue(
                "missing_beat", Severity.error,
                f"Chapter beats not covered by any scene: {sorted(missing)}",
                suggested_fix="Add scenes or extend source_beat_ids to cover every beat",
            ))

        declared_refs = {p.temporary_ref for p in safe_plan.proposed_entities}
        for scene in batch.scenes:
            undeclared = [ref for ref in scene.temporary_entities if ref not in declared_refs]
            if undeclared:
                issues.append(self._issue(
                    "undeclared_temporary_ref", Severity.error,
                    f"Scene {scene.scene_index} uses undeclared temporary refs: {undeclared}",
                    entity_type="character",
                ))
        return issues

    # ---------------------------------------------------------- references

    async def _check_references(
        self, batch: ScenePlanBatch, safe_plan: WriterSafePlan, novel_id: uuid.UUID
    ) -> list[PlanIssue]:
        issues: list[PlanIssue] = []
        all_characters = await self.characters.list_for_novel(novel_id)
        known_names = {c.name for c in all_characters}
        known_names |= {alias for c in all_characters for alias in (c.aliases or [])}
        known_ids = {str(c.id) for c in all_characters}
        known_locations = {loc.name for loc in await self.locations.list_for_novel(novel_id)}
        hints = {p.name_hint for p in safe_plan.proposed_entities if p.name_hint}
        hints |= {p.temporary_ref for p in safe_plan.proposed_entities}

        def check_ref(label: str, scene, ref) -> None:
            if ref.character_id:
                if ref.character_id not in known_ids:
                    issues.append(self._issue(
                        "unknown_character_id", Severity.critical,
                        f"Scene {scene.scene_index} {label} id {ref.character_id} does not exist",
                        entity_type="character", entity_id=ref.character_id,
                    ))
                return
            if ref.temporary_ref and ref.temporary_ref not in hints:
                issues.append(self._issue(
                    "undeclared_temporary_ref", Severity.error,
                    f"Scene {scene.scene_index} {label} temporary_ref {ref.temporary_ref!r} "
                    "is not declared in the chapter plan",
                ))
                return
            if ref.name and ref.name not in known_names and ref.name not in hints:
                issues.append(self._issue(
                    "unknown_reference", Severity.error,
                    f"Scene {scene.scene_index} {label} {ref.name!r} matches no canonical "
                    "entity or proposed entity",
                ))

        for scene in batch.scenes:
            check_ref("POV", scene, scene.pov)
            for participant in scene.participants:
                check_ref("participant", scene, participant)
            if scene.location is not None:
                label = scene.location.name or scene.location.temporary_ref
                if (
                    scene.location.character_id is None
                    and label not in known_locations
                    and label not in hints
                ):
                    issues.append(self._issue(
                        "unknown_location", Severity.error,
                        f"Scene {scene.scene_index} location {label!r} does not exist",
                        entity_type="location",
                    ))
            if scene.target_chars < 100:
                issues.append(self._issue(
                    "target_chars_too_small", Severity.warning,
                    f"Scene {scene.scene_index} target_chars={scene.target_chars} is unreasonably small",
                ))
        return issues

    # ------------------------------------------------------- reveal scope

    def _check_reveal_scope(
        self, batch: ScenePlanBatch, safe_plan: WriterSafePlan, author_plan=None
    ) -> list[PlanIssue]:
        """Escalation detection uses the AUTHOR-level plan (validator is
        domain code, not an agent) while the writer-safe plan never carries
        the raw prohibition texts."""
        issues: list[PlanIssue] = []
        prohibited: list[str] = []
        if author_plan is not None:
            prohibited += [p.statement for p in author_plan.prohibited_revelations]
            prohibited += list(author_plan.must_not_reveal)
        for scene in batch.scenes:
            texts = scene.allowed_information + scene.allowed_hints + [scene.scene_goal]
            for statement in prohibited:
                if statement and statement in " ".join(texts):
                    issues.append(self._issue(
                        "reveal_scope_escalation", Severity.critical,
                        f"Scene {scene.scene_index} allows information that the chapter plan "
                        "prohibits from being revealed",
                        entity_type="scene", entity_id=scene.scene_id,
                        evidence=[statement],
                        suggested_fix="Downgrade to a hint or remove the escalation",
                    ))
        return issues

    def _issue(self, code, severity, message, **kw) -> PlanIssue:
        return PlanIssue(code=code, severity=severity, message=message, **kw)
