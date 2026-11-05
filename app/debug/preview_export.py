"""Preview Artifact Export (development / debug only).

Exports the artifacts of one full preview pipeline run to the local
filesystem so developers can inspect what each stage produced:

    <base_dir>/artifacts/previews/<novel_id>/chapter_<n>/<run_id>/
        chapter.txt              final (post-repair) draft prose
        chapter_plan.json        author-level ChapterPlan
        scene_plans.json         author-level ScenePlans
        continuity_report.json   merged continuity report
        memory_preview.json      candidate batch + validation report
        dry_run.json             dry-run result (or {"enabled": false})
        metadata.json            provenance (hashes, models, status=preview)

Hard guarantees:
- DTO -> file only: the service holds NO database session and never
  touches canonical state (no Chapter row, no snapshot, no
  current_chapter_number update, no CanonicalCommitService import)
- every run gets a unique run_id; an existing run directory is never
  overwritten (caller may surface the error)
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from app.continuity.models import ContinuityReport
from app.draft.models import ChapterDraft
from app.planning.models import PlanGenerationResult
from app.schemas.agent_outputs import ChapterPlan
from app.services.memory_extraction import MemoryExtractionResult

RUN_ID_TIME_FORMAT = "%Y%m%d_%H%M%S"


class PreviewExportResult(BaseModel):
    run_id: str
    path: str
    files: list[str] = Field(default_factory=list)


class PreviewExportService:
    """Filesystem exporter for preview artifacts. Debug tooling only."""

    def __init__(self, base_dir: str | Path) -> None:
        self.base_dir = Path(base_dir)

    def export(
        self,
        *,
        novel_id: str,
        draft: ChapterDraft,
        chapter_plan: ChapterPlan,
        continuity_report: ContinuityReport | None = None,
        planning_result: PlanGenerationResult | None = None,
        memory_result: MemoryExtractionResult | None = None,
    ) -> PreviewExportResult:
        run_id = self._new_run_id()
        run_dir = (
            self.base_dir
            / "artifacts"
            / "previews"
            / str(novel_id)
            / f"chapter_{draft.chapter_number}"
            / run_id
        )
        if run_dir.exists():
            raise FileExistsError(
                f"Run directory already exists, refusing to overwrite: {run_dir}"
            )
        run_dir.mkdir(parents=True, exist_ok=False)

        files: list[str] = []

        def _write(name: str, content: str) -> None:
            (run_dir / name).write_text(content, encoding="utf-8")
            files.append(name)

        _write("chapter.txt", draft.full_text)
        _write("chapter_plan.json", chapter_plan.model_dump_json(indent=2))
        _write(
            "scene_plans.json",
            json.dumps(
                [sp.model_dump(exclude_none=True) for sp in draft.scene_plans],
                ensure_ascii=False,
                indent=2,
            ),
        )
        _write(
            "continuity_report.json",
            continuity_report.model_dump_json(indent=2)
            if continuity_report is not None
            else json.dumps({"enabled": False}),
        )
        if memory_result is not None:
            _write(
                "memory_preview.json",
                json.dumps(
                    {
                        "batch": memory_result.batch.model_dump(exclude_none=True),
                        "validation_report": memory_result.validation_report.model_dump(),
                        "draft_hash": memory_result.draft_hash,
                        "context_hash": memory_result.context_hash,
                    },
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                ),
            )
            dry_run_payload = (
                memory_result.dry_run.model_dump()
                if memory_result.dry_run is not None
                else {"enabled": False}
            )
            _write("dry_run.json", json.dumps(dry_run_payload, ensure_ascii=False, indent=2, default=str))
        else:
            _write("memory_preview.json", json.dumps({"enabled": False}))
            _write("dry_run.json", json.dumps({"enabled": False}))

        _write("metadata.json", self._metadata(
            novel_id=novel_id,
            draft=draft,
            run_id=run_id,
            planning_result=planning_result,
            memory_result=memory_result,
        ))

        return PreviewExportResult(run_id=run_id, path=str(run_dir), files=files)

    # ------------------------------------------------------------ internals

    @staticmethod
    def _new_run_id() -> str:
        timestamp = datetime.now(UTC).strftime(RUN_ID_TIME_FORMAT)
        return f"{timestamp}_{uuid.uuid4().hex[:8]}"

    def _metadata(
        self,
        *,
        novel_id: str,
        draft: ChapterDraft,
        run_id: str,
        planning_result: PlanGenerationResult | None,
        memory_result: MemoryExtractionResult | None,
    ) -> str:
        generation_metadata = []
        if draft.generation_metadata:
            generation_metadata.extend(
                m.model_dump(exclude_none=True) for m in draft.generation_metadata
            )
        if planning_result is not None and planning_result.metadata is not None:
            generation_metadata.append(planning_result.metadata.model_dump(exclude_none=True))
        if memory_result is not None and memory_result.generation_metadata is not None:
            generation_metadata.append(
                memory_result.generation_metadata.model_dump(exclude_none=True)
            )

        prompt_versions = sorted(
            {
                m["prompt_version"]
                for m in generation_metadata
                if m.get("prompt_version")
            }
        )
        models = sorted({m["model"] for m in generation_metadata if m.get("model")})

        metadata = {
            "novel_id": novel_id,
            "chapter_number": draft.chapter_number,
            "run_id": run_id,
            "story_state_hash": draft.story_context_hash,
            "chapter_plan_hash": draft.chapter_plan_hash,
            "draft_hash": hashlib.sha256(draft.full_text.encode("utf-8")).hexdigest(),
            "generated_at": datetime.now(UTC).isoformat(),
            "status": "preview",
            "models": models,
            "prompt_versions": prompt_versions,
            "generation_metadata": generation_metadata,
            "scene_count": len(draft.scenes),
            "repair_history": [
                r.model_dump()
                for r in getattr(draft, "repair_history", []) or []
            ],
        }
        return json.dumps(metadata, ensure_ascii=False, indent=2)
