"""Provenance helpers: a single definition of "which story state produced
this artifact".

``compute_story_state_hash`` is THE canonical state fingerprint used by:
- ChapterPlanningService  (PlanGenerationResult.context_hash)
- ChapterDraftService     (ChapterDraft.story_context_hash + staleness check)
- MemoryExtractionService (dry-run before-state)

It hashes the serialized canonical state (Phase 1 snapshot projection);
it deliberately excludes user_direction and retrieval-time context so that
planning and drafting agree on the same value for the same DB state.
"""

from __future__ import annotations

import hashlib
import json
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.story_state import StoryStateService


async def build_story_state_projection(
    session: AsyncSession, novel_id: uuid.UUID
) -> dict:
    service = StoryStateService(session)
    return await service.build_state_dict(novel_id)


async def compute_story_state_hash(session: AsyncSession, novel_id: uuid.UUID) -> str:
    state = await build_story_state_projection(session, novel_id)
    return hash_state_dict(state)


def hash_state_dict(state: dict) -> str:
    serialized = json.dumps(state, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
