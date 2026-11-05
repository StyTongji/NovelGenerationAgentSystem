"""CanonicalCommitService — the ONLY sanctioned writer of canonical state.

Commit pipeline (single transaction):

    re-validate provenance (state hash / draft hash / plan hash /
    continuity draft hash / memory draft hash)
    -> final deterministic continuity recheck (no LLM)
    -> memory re-validation + FRESH dry-run against current state
    -> idempotency check (novel_id + draft_hash)
    -> BEGIN (session transaction)
        canonicalize proposed entities (temporary_ref -> UUID)
        apply operations in documented order:
            facts -> events -> knowledge -> items -> character states
            -> relationships -> threads -> foreshadowing -> debts
        save chapter (versioned, status=canonical, previous -> superseded)
        save chapter summaries (author/reader)
        advance novel.current_chapter_number
        create StorySnapshot (AFTER mutations)
        save ChapterCommit record (fingerprints + evidence provenance)
    -> COMMIT

Any exception anywhere -> full ROLLBACK, zero partial state.
Finalize never calls an LLM.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.commit.models import (
    ChapterCommitRequest,
    ChapterCommitResult,
    ChapterNumberCollisionError,
    ChapterPreviewPackage,
    CommitError,
    CommitValidationCode,
    DuplicateCommitError,
    StaleStoryStateError,
)
from app.context.builder import ContextBuilder
from app.draft.models import ChapterDraft
from app.memory.dry_run import MemoryDryRun
from app.memory.models import (
    MemoryUpdateBatch,
    MemoryUpdateOperation,
    MemoryUpdateType,
)
from app.memory.validator import MemoryUpdateValidator
from app.models import (
    Chapter,
    ChapterCommit,
    ChapterSummary,
    Character,
    CharacterKnowledge,
    CharacterState,
    Event,
    Relationship,
    Faction,
    Item,
    Location,
    Novel,
    PlotDebt,
    PlotThread,
    StoryFact,
    StorySnapshot,
)
from app.models.enums import (
    AliveStatus,
    ChapterStatus,
    ForeshadowStatus,
    ItemStatus,
    KnowledgeStatus,
    NovelStatus,
    SecrecyLevel,
    ThreadStatus,
)
from app.planning.provenance import (
    build_story_state_projection,
    compute_story_state_hash,
    hash_state_dict,
)
from app.repositories.character import CharacterRepository
from app.repositories.world import LocationRepository, PowerSystemRepository
from app.services.continuity import ContinuityService

# documented, fixed application order (Phase 8 §20)
APPLICATION_ORDER = [
    MemoryUpdateType.propose_entity,
    MemoryUpdateType.story_fact_create,
    MemoryUpdateType.create_event,
    MemoryUpdateType.character_knowledge_update,
    MemoryUpdateType.item_update,
    MemoryUpdateType.character_state_update,
    MemoryUpdateType.relationship_update,
    MemoryUpdateType.plot_thread_update,
    MemoryUpdateType.foreshadowing_update,
    MemoryUpdateType.plot_debt_update,
]


def draft_hash_of(draft: ChapterDraft) -> str:
    return hashlib.sha256(draft.full_text.encode("utf-8")).hexdigest()


def plan_hash_of(plan) -> str:
    return hashlib.sha256(plan.model_dump_json().encode("utf-8")).hexdigest()


class CanonicalCommitService:
    def __init__(self, session: AsyncSession, *, allow_legacy_batches: bool = False,
                 allow_automated_test_review: bool = False) -> None:
        self.session = session
        self.allow_legacy_batches = allow_legacy_batches
        self.allow_automated_test_review = allow_automated_test_review
        self.builder = ContextBuilder(session)
        self.characters = CharacterRepository(session)
        self.locations = LocationRepository(session)
        self.power_repo = PowerSystemRepository(session)
        # deterministic-only: commit recheck NEVER calls an LLM (§59)
        self._continuity = ContinuityService(
            session, llm_checker=None, repair_agent=None, mode="deterministic_only"
        )
        self._memory_validator = MemoryUpdateValidator(session)
        self._dry_run = MemoryDryRun()

    # ------------------------------------------------------------------ api

    async def commit_chapter(self, request: ChapterCommitRequest) -> ChapterCommitResult:
        if (request.approval and request.approval.review_kind == "automated_test"
                and not self.allow_automated_test_review):
            raise CommitError(CommitValidationCode.precondition_failed,
                              "Automated test approval is not accepted by the production commit path")
        novel = await self.session.get(Novel, uuid.UUID(request.novel_id))
        if novel is None:
            raise CommitError(CommitValidationCode.precondition_failed,
                              f"Novel {request.novel_id} not found")
        self._novel_id_cache = novel.id
        self._review_audit = {
            "attempt_id": request.attempt_id,
            "preview_package_hash": request.preview_package_hash,
            "approval": request.approval.model_dump(mode="json") if request.approval else None,
            "reviewed_continuity": request.continuity_report.model_dump(mode="json"),
        }
        self._memory_metadata = request.memory_result.generation_metadata

        draft = request.chapter_draft
        plan = request.chapter_plan
        d_hash = draft_hash_of(draft)

        if not self.allow_legacy_batches and (
            request.memory_result.batch.contract_version != 3 or not request.preview_package_hash
        ):
            raise CommitError(CommitValidationCode.precondition_failed,
                              "New commits require Memory v3 and a bound preview_package_hash")
        if not self.allow_legacy_batches:
            if request.approval is None:
                raise CommitError(CommitValidationCode.precondition_failed,
                                  "Explicit human review approval is required")
            request.approval.verify(request)

        # ---- Phase 8.5 Fix B: preview package integrity (extra layer on top
        # of the fine-grained checks below; it is NOT authorization)
        if request.preview_package_hash is not None:
            recomputed = ChapterPreviewPackage.compute_hash(
                plan=plan,
                draft=draft,
                continuity_report=request.continuity_report,
                memory_result=request.memory_result,
                story_state_hash=request.expected_story_state_hash,
            )
            if recomputed != request.preview_package_hash:
                from app.commit.models import PreviewPackageTamperedError

                raise PreviewPackageTamperedError(
                    request.preview_package_hash, recomputed
                )

        # ---- idempotency: same novel + draft already committed?
        existing = (
            await self.session.execute(
                select(ChapterCommit).where(
                    ChapterCommit.novel_id == novel.id,
                    ChapterCommit.draft_hash == d_hash,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return self._result_from_record(existing, duplicate=True)

        if not request.continuity_report.valid or not request.memory_result.validation_report.valid:
            raise CommitError(CommitValidationCode.precondition_failed,
                              "Cannot commit an invalid preview")

        # ---- provenance re-validation (never trust the client)
        current_state_hash = await compute_story_state_hash(self.session, novel.id)
        if request.expected_story_state_hash != current_state_hash:
            raise StaleStoryStateError(request.expected_story_state_hash, current_state_hash)

        if request.continuity_report.draft_hash != d_hash:
            raise CommitError(
                CommitValidationCode.draft_hash_mismatch,
                "continuity_report.draft_hash does not match the draft",
            )
        if request.memory_result.draft_hash != d_hash:
            raise CommitError(
                CommitValidationCode.draft_hash_mismatch,
                "memory_result.draft_hash does not match the draft",
            )
        if (
            request.memory_result.evidence_catalog is not None
            and request.memory_result.evidence_catalog.draft_hash != d_hash
        ):
            raise CommitError(
                CommitValidationCode.draft_hash_mismatch,
                "evidence_catalog.draft_hash does not match the draft",
            )
        if draft.chapter_plan_hash is not None and draft.chapter_plan_hash != plan_hash_of(plan):
            raise CommitError(
                CommitValidationCode.plan_hash_mismatch,
                "chapter_draft.chapter_plan_hash does not match the plan",
            )

        # ---- chapter number gate
        expected_number = novel.current_chapter_number + 1
        if draft.chapter_number != expected_number:
            raise ChapterNumberCollisionError(draft.chapter_number, novel.current_chapter_number)

        # ---- final deterministic continuity recheck (no LLM)
        fresh_continuity = await self._continuity.check_draft(
            novel.id, draft, plan, scene_plans=list(draft.scene_plans)
        )
        self._final_continuity_report = fresh_continuity
        if not fresh_continuity.valid:
            raise CommitError(
                CommitValidationCode.continuity_invalid,
                "Final deterministic continuity recheck failed: "
                + "; ".join(i.code for i in fresh_continuity.blocking_issues()),
            )

        # ---- memory re-validation against the CURRENT state
        context = await self.builder.build_extraction_context(
            novel.id,
            chapter_number=draft.chapter_number,
            chapter_text=draft.full_text,
            chapter_plan=plan,
            scene_plans=list(draft.scene_plans),
            draft=draft,
            draft_hash=d_hash,
        )
        if request.memory_result.batch.contract_version == 3 and request.memory_result.evidence_catalog != context.evidence_catalog:
            raise CommitError(CommitValidationCode.memory_invalid,
                              "Submitted catalog differs from server reconstruction")
        batch = await self._revalidate_and_resolve_batch(
            request.memory_result.batch, context, draft
        )
        memory_report = await self._memory_validator.validate(
            batch,
            context,
            draft,
            require_evidence_refs=not self.allow_legacy_batches or batch.contract_version == 3,
        )
        if not memory_report.valid:
            raise CommitError(
                CommitValidationCode.memory_invalid,
                "Memory batch re-validation failed: "
                + "; ".join(i.code for i in memory_report.errors()),
            )

        from app.memory.catalog import resolve_evidence_refs
        batch = resolve_evidence_refs(batch, context.evidence_catalog)

        # ---- dry-run must match the CURRENT state (never apply a stale run)
        from app.memory.dry_run import build_memory_before_state
        before_state = await build_memory_before_state(self.session, novel.id, draft.chapter_number, context)
        before_hash = hash_state_dict(before_state)
        if (
            request.memory_result.dry_run is not None
            and request.memory_result.dry_run.before_hash != before_hash
        ):
            raise CommitError(
                CommitValidationCode.dry_run_stale,
                "Client dry-run was computed against a different state; regenerate preview",
            )
        fresh_dry_run = self._dry_run.run(batch, before_state)
        if not fresh_dry_run.applicable:
            raise CommitError(
                CommitValidationCode.memory_invalid,
                "Fresh dry-run reported warnings: " + "; ".join(fresh_dry_run.warnings),
            )

        # ---- single transaction
        try:
            result = await self._commit_in_transaction(
                novel=novel,
                draft=draft,
                plan=plan,
                batch=batch,
                d_hash=d_hash,
                before_state_hash=current_state_hash,
            )
        except asyncio.CancelledError:
            await self.session.rollback()
            raise
        except CommitError:
            await self.session.rollback()
            raise
        except Exception as exc:
            await self.session.rollback()
            raise CommitError(
                CommitValidationCode.precondition_failed,
                f"commit_failed: {type(exc).__name__}",
            ) from exc
        return result

    # ---------------------------------------------------------- transaction

    async def _commit_in_transaction(
        self,
        *,
        novel: Novel,
        draft: ChapterDraft,
        plan,
        batch: MemoryUpdateBatch,
        d_hash: str,
        before_state_hash: str,
    ) -> ChapterCommitResult:
        chapter_number = draft.chapter_number

        # 1. canonicalize proposed entities (temporary_ref -> UUID)
        temp_mapping = await self._canonicalize_entities(batch, chapter_number)

        ordered = self._ordered_operations(batch)

        # 2. story facts
        fact_id_by_statement = await self._apply_story_facts(ordered, novel.id, chapter_number)

        # 3. chapter row first (events reference it)
        chapter = await self._save_chapter(
            novel=novel, draft=draft, plan=plan, d_hash=d_hash,
            before_state_hash=before_state_hash,
        )

        # 4. events
        await self._apply_events(ordered, novel.id, chapter, temp_mapping)

        # 5. knowledge
        await self._apply_knowledge(ordered, chapter_number, fact_id_by_statement)

        # 6. items
        await self._apply_items(ordered, temp_mapping)

        # 7. character states (collapsed to chapter-end state)
        await self._apply_character_states(ordered, chapter_number)

        # 8. relationships
        await self._apply_relationships(ordered, novel.id)

        # 9. plot threads
        await self._apply_plot_threads(ordered, chapter_number)

        # 10. foreshadowing
        await self._apply_foreshadowing(ordered, novel.id, chapter_number)

        # 11. plot debts
        await self._apply_plot_debts(ordered, novel.id, chapter_number)

        # 12. summaries (based on the FINAL repaired draft, bound to its hash)
        await self._save_summaries(novel.id, chapter, draft, batch, d_hash)

        # 13. advance novel progress
        novel.current_chapter_number = chapter_number
        if novel.status == NovelStatus.drafting:
            novel.status = NovelStatus.ongoing

        # 14. snapshot AFTER all mutations
        await self.session.flush()
        state_service = await self._state_service()
        snapshot = await state_service.create_snapshot(novel.id, after_chapter_number=chapter_number)
        after_state_hash = snapshot.state_hash or ""
        chapter.story_state_after_hash = after_state_hash
        await self.session.flush()

        # 15. commit record (idempotency + audit)
        commit_record = ChapterCommit(
            novel_id=novel.id,
            chapter_id=chapter.id,
            chapter_number=chapter_number,
            draft_hash=d_hash,
            before_state_hash=before_state_hash,
            after_state_hash=after_state_hash,
            operation_fingerprints_json=[
                op.fingerprint(str(novel.id), d_hash) for op in ordered
            ],
            operations_json=[self._operation_provenance(op, fact_id_by_statement, temp_mapping) for op in ordered],
            temporary_entity_mapping_json={k: str(v) for k, v in temp_mapping.items()},
            generation_metadata_json=[
                m.model_dump(exclude_none=True) for m in draft.generation_metadata
            ] + ([self._memory_metadata.model_dump(mode="json")] if self._memory_metadata else []),
            continuity_report_json={**self._final_continuity_report.model_dump(mode="json"),
                                    "review_audit": self._review_audit},
            snapshot_id=snapshot.id,
        )
        self.session.add(commit_record)
        await self.session.flush()

        await self.session.commit()

        return ChapterCommitResult(
            commit_id=commit_record.id,
            novel_id=str(novel.id),
            chapter_id=str(chapter.id),
            chapter_number=chapter_number,
            before_story_state_hash=before_state_hash,
            after_story_state_hash=after_state_hash,
            draft_hash=d_hash,
            operation_count=len(ordered),
            applied_operation_fingerprints=commit_record.operation_fingerprints_json,
            temporary_entity_mapping={k: str(v) for k, v in temp_mapping.items()},
            snapshot_id=str(snapshot.id),
            committed_at=commit_record.created_at,
            duplicate=False,
        )

    # ------------------------------------------------------ canonicalization

    async def _canonicalize_entities(self, batch: MemoryUpdateBatch, chapter_number: int) -> dict[str, uuid.UUID]:
        mapping: dict[str, uuid.UUID] = {}
        for op in batch.operations:
            if op.update_type != MemoryUpdateType.propose_entity or not op.proposed_entity:
                continue
            payload = op.proposed_entity
            if getattr(payload, "canonicalize", True) is False:
                continue
            # dedupe: an entity canonicalized by an earlier chapter must not
            # be created twice under a new temporary_ref
            proposed_name = payload.proposed_name or (payload.attributes or {}).get("name")
            existing_id = await self._find_existing_entity(
                payload.entity_type, proposed_name
            )
            if existing_id is not None:
                mapping[payload.temporary_ref] = existing_id
                continue
            entity_id = await self._create_canonical_entity(payload, chapter_number)
            mapping[payload.temporary_ref] = entity_id
        await self.session.flush()
        return mapping

    async def _find_existing_entity(self, entity_type: str, name: str | None) -> uuid.UUID | None:
        if not name:
            return None
        model = {
            "character": Character,
            "location": Location,
            "item": Item,
            "faction": Faction,
        }.get(entity_type)
        if model is None:
            return None
        result = await self.session.execute(
            select(model).where(
                model.novel_id == self._current_novel_id, model.name == name
            )
        )
        entity = result.scalars().first()
        return entity.id if entity is not None else None

    async def _create_canonical_entity(self, payload, chapter_number: int) -> uuid.UUID:
        name = payload.proposed_name or (payload.attributes or {}).get("name")
        if payload.entity_type == "character":
            realm = None
            if payload.attributes.get("realm"):
                realm = await self.power_repo.find_realm_by_name(self._current_novel_id, payload.attributes["realm"])
                if realm is None:
                    raise CommitError(CommitValidationCode.precondition_failed, "Unknown proposed character realm")
            entity = Character(
                novel_id=self._current_novel_id,
                name=name or payload.temporary_ref,
                description=payload.attributes.get("role") or payload.attributes.get("description"),
                personality_traits=[],
            )
            self.session.add(entity)
            await self.session.flush()
            self.session.add(
                CharacterState(
                    character_id=entity.id,
                    chapter_number=chapter_number,
                    alive_status=AliveStatus.alive,
                    realm_id=realm.id if realm else None,
                    current_goals=[],
                )
            )
            return entity.id
        if payload.entity_type == "location":
            entity = Location(novel_id=self._current_novel_id, name=name or payload.temporary_ref)
            self.session.add(entity)
            await self.session.flush()
            return entity.id
        if payload.entity_type == "item":
            entity = Item(novel_id=self._current_novel_id, name=name or payload.temporary_ref)
            self.session.add(entity)
            await self.session.flush()
            return entity.id
        if payload.entity_type == "faction":
            entity = Faction(novel_id=self._current_novel_id, name=name or payload.temporary_ref)
            self.session.add(entity)
            await self.session.flush()
            return entity.id
        raise CommitError(CommitValidationCode.precondition_failed,
                          f"Unknown entity_type {payload.entity_type!r}")

    # ---------------------------------------------------------- appliers

    async def _apply_story_facts(self, ordered, novel_id, chapter_number) -> dict[str, str]:
        fact_ids: dict[str, str] = {}
        for op in ordered:
            if op.update_type != MemoryUpdateType.story_fact_create or not op.story_fact:
                continue
            payload = op.story_fact
            fact = StoryFact(
                novel_id=novel_id,
                statement=payload.statement,
                secrecy=payload.secrecy,
                category=payload.category,
                introduced_chapter=chapter_number,
                revealed_to_reader_chapter=chapter_number if payload.reader_visible else None,
            )
            self.session.add(fact)
            fact_ids[payload.statement] = fact  # id assigned at flush
        await self.session.flush()
        resolved = {"statement:" + statement: str(fact.id) for statement, fact in fact_ids.items()}
        for op in ordered:
            if op.update_type == MemoryUpdateType.story_fact_create and op.story_fact:
                if op.story_fact.temporary_fact_ref:
                    resolved["temporary:" + op.story_fact.temporary_fact_ref] = resolved["statement:" + op.story_fact.statement]
        return resolved

    async def _apply_events(self, ordered, novel_id, chapter, temp_mapping) -> None:
        for op in ordered:
            if op.update_type != MemoryUpdateType.create_event or not op.event:
                continue
            payload = op.event
            participants = list(payload.participant_ids)
            for ref in payload.participant_temporary_refs:
                if ref in temp_mapping:
                    participants.append(str(temp_mapping[ref]))
            self.session.add(
                Event(
                    novel_id=novel_id,
                    chapter_id=chapter.id,
                    chapter_number=chapter.number,
                    event_type=payload.event_type,
                    title=payload.title,
                    # author summary is the canonical log; reader-safe summary
                    # + structured visibility ride in metadata (§22/§23)
                    summary=payload.author_summary,
                    participant_ids=participants,
                    location_id=(temp_mapping.get(payload.location_id) or uuid.UUID(payload.location_id))
                    if payload.location_id else None,
                    importance=payload.importance,
                    happened_at_story_time=payload.happened_at_story_time,
                    extra_metadata={
                        "reader_safe_summary": payload.reader_safe_summary,
                        "visibility": payload.visibility.model_dump(),
                        "author_summary": payload.author_summary,
                    },
                )
            )
        await self.session.flush()

    async def _apply_knowledge(self, ordered, chapter_number, fact_id_by_statement) -> None:
        for op in ordered:
            if op.update_type != MemoryUpdateType.character_knowledge_update or not op.knowledge:
                continue
            payload = op.knowledge
            fact_id = payload.fact_id
            if not fact_id and payload.fact_ref and payload.fact_ref.fact_id:
                fact_id = payload.fact_ref.fact_id
            if not fact_id and payload.fact_ref and payload.fact_ref.temporary_fact_ref:
                fact_id = fact_id_by_statement.get("temporary:" + payload.fact_ref.temporary_fact_ref)
            if not fact_id and payload.fact_statement:
                fact_id = fact_id_by_statement.get("statement:" + payload.fact_statement)
            if not fact_id and payload.temporary_fact_ref:
                fact_id = fact_id_by_statement.get("temporary:" + payload.temporary_fact_ref)
            if not fact_id:
                raise CommitError(
                    CommitValidationCode.precondition_failed,
                    f"knowledge fact reference unresolved for {op.operation_id}",
                )
            existing = await self.characters.get_knowledge(
                uuid.UUID(payload.character_id), uuid.UUID(fact_id)
            )
            if existing is not None:
                # idempotent update of status/confidence, keep provenance
                existing.status = payload.status
                existing.learned_chapter = existing.learned_chapter or chapter_number
                continue
            self.session.add(
                CharacterKnowledge(
                    character_id=uuid.UUID(payload.character_id),
                    fact_id=uuid.UUID(fact_id),
                    status=payload.status,
                    confidence=payload.belief_confidence or 1.0,
                    learned_chapter=chapter_number,
                )
            )
        await self.session.flush()

    async def _apply_items(self, ordered, temp_mapping=None) -> None:
        temp_mapping = temp_mapping or {}
        for op in ordered:
            if op.update_type != MemoryUpdateType.item_update or not op.item:
                continue
            payload = op.item
            resolved_id = uuid.UUID(payload.item_id) if payload.item_id else temp_mapping.get(payload.temporary_item_ref)
            if resolved_id is None:
                raise CommitError(CommitValidationCode.precondition_failed, "Unresolved temporary item dependency")
            item = await self.session.get(Item, resolved_id)
            if item is None:
                raise CommitError(CommitValidationCode.precondition_failed,
                                  f"item {payload.item_id} vanished mid-commit")
            if payload.change == "owner_transfer":
                current_owner = (
                    str(item.current_owner_character_id)
                    if item.current_owner_character_id
                    else None
                )
                if payload.expected_old_owner_id is not None and current_owner != payload.expected_old_owner_id:
                    raise CommitError(
                        CommitValidationCode.precondition_failed,
                        f"item {item.name}: owner changed mid-commit "
                        f"({current_owner} != {payload.expected_old_owner_id})",
                    )
                item.current_owner_character_id = (
                    uuid.UUID(payload.new_owner_id) if payload.new_owner_id else None
                )
            elif payload.change == "location_change":
                item.current_location_id = (
                    uuid.UUID(payload.new_location_id) if payload.new_location_id else None
                )
            elif payload.change == "consumed":
                item.status = ItemStatus.lost
            elif payload.change == "status_change":
                item.status = ItemStatus(payload.new_status)
        await self.session.flush()

    async def _apply_character_states(self, ordered, chapter_number) -> None:
        """Collapse same-chapter changes per (character, field) into ONE
        chapter-end CharacterState row; intermediate changes live in Events."""
        merged: dict[str, dict[str, str]] = {}
        order: list[str] = []
        for op in ordered:
            if op.update_type != MemoryUpdateType.character_state_update or not op.character_state:
                continue
            payload = op.character_state
            from app.memory.models import CANONICAL_CHARACTER_STATE_FIELDS
            if payload.field not in CANONICAL_CHARACTER_STATE_FIELDS:
                raise CommitError(CommitValidationCode.precondition_failed,
                                  f"Unsupported canonical character field: {payload.field}")
            if payload.character_id not in merged:
                merged[payload.character_id] = {}
                order.append(payload.character_id)
            merged[payload.character_id][payload.field] = payload.proposed_new_value

        for character_id in order:
            fields = merged[character_id]
            current = await self.characters.current_state(uuid.UUID(character_id))
            location_id = current.current_location_id if current else None
            realm_id = current.realm_id if current else None
            subrealm = current.subrealm if current else None
            for field, value in fields.items():
                if field == "location":
                    location = await self.locations.find_by_name(self._current_novel_id, value)
                    if location is None:
                        raise CommitError(CommitValidationCode.precondition_failed,
                                          f"unknown location {value!r}")
                    location_id = location.id
                elif field == "realm":
                    realm = await self.power_repo.find_realm_by_name(self._current_novel_id, value)
                    if realm is None:
                        raise CommitError(CommitValidationCode.precondition_failed,
                                          f"unknown realm {value!r}")
                    realm_id = realm.id
            self.session.add(
                CharacterState(
                    character_id=uuid.UUID(character_id),
                    chapter_number=chapter_number,
                    alive_status=AliveStatus(fields.get("alive_status", current.alive_status.value if current else "alive")),
                    current_location_id=location_id,
                    realm_id=realm_id,
                    subrealm=subrealm,
                    physical_condition=fields.get("physical_condition", current.physical_condition if current else None),
                    mental_condition=fields.get("mental_condition", current.mental_condition if current else None),
                    current_goals=current.current_goals if current else [],
                    reputation=current.reputation if current else {},
                    wealth_summary=current.wealth_summary if current else None,
                )
            )
        await self.session.flush()

    async def _apply_relationships(self, ordered, novel_id) -> None:
        for op in ordered:
            if op.update_type != MemoryUpdateType.relationship_update or not op.relationship:
                continue
            payload = op.relationship
            # qualitative note only; NO numeric deltas from the LLM (§33)
            existing = await self.characters.find_relationship(
                novel_id,
                uuid.UUID(payload.source_character_id),
                uuid.UUID(payload.target_character_id),
                payload.type,
            )
            if existing is not None:
                if payload.note:
                    existing.public_description = payload.note
                continue
            self.session.add(
                Relationship(
                    novel_id=novel_id,
                    source_character_id=uuid.UUID(payload.source_character_id),
                    target_character_id=uuid.UUID(payload.target_character_id),
                    type=payload.type,
                    public_description=payload.note,
                )
            )
        await self.session.flush()

    async def _apply_plot_threads(self, ordered, chapter_number) -> None:
        for op in ordered:
            if op.update_type != MemoryUpdateType.plot_thread_update or not op.plot_thread:
                continue
            payload = op.plot_thread
            thread = await self.session.get(PlotThread, uuid.UUID(payload.plot_thread_id))
            if thread is None:
                raise CommitError(CommitValidationCode.precondition_failed,
                                  "plot thread vanished mid-commit")
            if payload.action == "resolved":
                thread.status = ThreadStatus.resolved
            elif payload.action == "introduced":
                thread.status = ThreadStatus.active
            elif thread.status in (ThreadStatus.planned, ThreadStatus.dormant):
                thread.status = ThreadStatus.active
            else:
                thread.status = ThreadStatus.progressing
            thread.last_advanced_chapter = chapter_number
        await self.session.flush()

    async def _apply_foreshadowing(self, ordered, novel_id, chapter_number) -> None:
        from app.models import Foreshadowing

        for op in ordered:
            if op.update_type != MemoryUpdateType.foreshadowing_update or not op.foreshadowing:
                continue
            payload = op.foreshadowing
            if payload.action == "introduced":
                self.session.add(
                    Foreshadowing(
                        novel_id=novel_id,
                        plot_thread_id=uuid.UUID(payload.foreshadowing_id) if payload.foreshadowing_id else None,
                        setup=payload.setup or payload.note or "",
                        status=ForeshadowStatus.planted,
                        planted_chapter=chapter_number,
                    )
                )
                continue
            if payload.foreshadowing_id is None:
                continue
            foreshadow = await self.session.get(Foreshadowing, uuid.UUID(payload.foreshadowing_id))
            if foreshadow is None:
                continue
            if payload.action == "resolved":
                foreshadow.status = ForeshadowStatus.paid_off
                foreshadow.payoff_chapter = chapter_number
            elif payload.action in ("advanced", "echoed"):
                foreshadow.status = ForeshadowStatus.reinforced
        await self.session.flush()

    async def _apply_plot_debts(self, ordered, novel_id, chapter_number) -> None:
        for op in ordered:
            if op.update_type != MemoryUpdateType.plot_debt_update or not op.plot_debt:
                continue
            payload = op.plot_debt
            if payload.action == "created":
                self.session.add(
                    PlotDebt(
                        novel_id=novel_id,
                        debt_type=None,
                        description=payload.description or "",
                        created_chapter=chapter_number,
                        due_chapter=payload.due_chapter,
                    )
                )
                continue
            if payload.plot_debt_id is None:
                continue
            debt = await self.session.get(PlotDebt, uuid.UUID(payload.plot_debt_id))
            if debt is None:
                continue
            if payload.action == "resolved":
                debt.status = "fulfilled"
                debt.resolved_chapter = chapter_number
            # progressed: no structural change; the event log carries it
        await self.session.flush()

    # ------------------------------------------------------------- chapter

    async def _save_chapter(self, *, novel: Novel, draft: ChapterDraft, plan, d_hash: str,
                            before_state_hash: str) -> Chapter:
        # versioning: never overwrite; previous canonical -> superseded
        existing = (
            await self.session.execute(
                select(Chapter).where(
                    Chapter.novel_id == novel.id, Chapter.number == draft.chapter_number
                )
            )
        ).scalars().all()
        version = max((c.version for c in existing), default=0) + 1
        for chapter_row in existing:
            if chapter_row.status == ChapterStatus.canonical:
                chapter_row.status = ChapterStatus.superseded
        chapter = Chapter(
            novel_id=novel.id,
            number=draft.chapter_number,
            version=version,
            title=plan.chapter_goal[:180] if plan.chapter_goal else None,
            status=ChapterStatus.canonical,
            body=draft.full_text,
            user_direction=None,
            chapter_plan_json=plan.model_dump(mode="json"),
            continuity_report_json=None,  # stored on the commit record trail
            prompt_version=(draft.generation_metadata[0].prompt_version if draft.generation_metadata else None),
            model_config_json={
                "generation_metadata": [
                    m.model_dump(exclude_none=True) for m in draft.generation_metadata
                ],
            },
            draft_hash=d_hash,
            story_state_before_hash=before_state_hash,
        )
        self.session.add(chapter)
        await self.session.flush()
        return chapter

    async def _save_summaries(self, novel_id, chapter: Chapter, draft: ChapterDraft,
                              batch: MemoryUpdateBatch, d_hash: str) -> None:
        self.session.add(
            ChapterSummary(
                novel_id=novel_id,
                chapter_id=chapter.id,
                chapter_number=chapter.number,
                short_summary=batch.candidate_reader_summary or "",
                author_summary=batch.candidate_author_summary,
                reader_summary=batch.candidate_reader_summary,
                # Phase 8.5 Fix C: explicit provenance column (no field abuse)
                source_draft_hash=d_hash,
            )
        )
        await self.session.flush()

    # ------------------------------------------------------------ helpers

    def _ordered_operations(self, batch: MemoryUpdateBatch) -> list[MemoryUpdateOperation]:
        by_type: dict[MemoryUpdateType, list[MemoryUpdateOperation]] = {}
        for op in batch.operations:
            by_type.setdefault(op.update_type, []).append(op)
        ordered: list[MemoryUpdateOperation] = []
        for update_type in APPLICATION_ORDER:
            ordered.extend(by_type.get(update_type, []))
        return ordered

    def _operation_provenance(self, op: MemoryUpdateOperation, fact_mapping=None, temp_mapping=None) -> dict:
        fact_mapping = fact_mapping or {}
        resolved_fact_id = None
        if op.story_fact:
            resolved_fact_id = fact_mapping.get("statement:" + op.story_fact.statement)
        if op.knowledge:
            payload = op.knowledge
            ref = payload.fact_ref
            resolved_fact_id = (ref.fact_id if ref else payload.fact_id) or fact_mapping.get(
                "temporary:" + ((ref.temporary_fact_ref if ref else payload.temporary_fact_ref) or "")
            ) or fact_mapping.get("statement:" + (payload.fact_statement or ""))
        return {
            "operation_id": op.operation_id,
            "update_type": op.update_type.value,
            "target_id": op.target_id(),
            "source_scene_id": op.source_scene_id,
            "payload": op.payload.model_dump(mode="json", exclude_none=True),
            "resolved_fact_id": resolved_fact_id,
            "resolved_item_id": (op.item.item_id or str((temp_mapping or {}).get(op.item.temporary_item_ref))) if op.item else None,
            "evidence": [
                {
                    "evidence_id": e.evidence_id,
                    "scene_id": e.scene_id,
                    "exact_text": e.exact_quote,
                    "start_char": e.start_char,
                    "end_char": e.end_char,
                }
                for e in op.evidence
            ],
            "evidence_refs": [r.evidence_id for r in op.evidence_refs],
            "unplanned_change": op.unplanned_change,
        }

    async def _revalidate_and_resolve_batch(
        self, batch: MemoryUpdateBatch, context, draft: ChapterDraft
    ) -> MemoryUpdateBatch:
        """Resolve batch-internal fact references (knowledge ops pointing at
        story_fact_create statements) so re-validation sees consistent IDs."""
        batch = batch.model_copy(deep=True)
        if batch.contract_version == 3:
            return batch  # v3 dependencies are explicit; no text-based repair
        known = {f.statement: f.fact_id for f in context.existing_facts}
        proposed: dict[str, str] = {}
        for index, op in enumerate(batch.operations, start=1):
            if op.update_type == MemoryUpdateType.story_fact_create and op.story_fact:
                temp_ref = op.story_fact.temporary_fact_ref or f"new_fact_{index}"
                op.story_fact.temporary_fact_ref = temp_ref
                proposed[op.story_fact.statement] = temp_ref
        resolved_ops = []
        for op in batch.operations:
            if op.update_type == MemoryUpdateType.character_knowledge_update and op.knowledge:
                knowledge = op.knowledge
                if knowledge.fact_ref:
                    if knowledge.fact_ref.fact_id:
                        knowledge.fact_id = knowledge.fact_ref.fact_id
                    elif knowledge.fact_ref.temporary_fact_ref:
                        knowledge.temporary_fact_ref = knowledge.fact_ref.temporary_fact_ref
                if knowledge.fact_id is None and knowledge.fact_statement:
                    knowledge.fact_id = known.get(knowledge.fact_statement) or None
                    if knowledge.fact_id is None:
                        knowledge.temporary_fact_ref = proposed.get(knowledge.fact_statement)
            resolved_ops.append(op)
        return batch.model_copy(update={"operations": resolved_ops})

    async def _state_service(self):
        from app.services.story_state import StoryStateService

        return StoryStateService(self.session)

    @property
    def _current_novel_id(self) -> uuid.UUID:
        if self._novel_id_cache is None:
            raise RuntimeError("novel id not set")
        return self._novel_id_cache

    @_current_novel_id.setter
    def _current_novel_id(self, value: uuid.UUID) -> None:
        self._novel_id_cache = value

    _novel_id_cache: uuid.UUID | None = None

    def _result_from_record(self, record: ChapterCommit, *, duplicate: bool) -> ChapterCommitResult:
        return ChapterCommitResult(
            commit_id=record.id,
            novel_id=str(record.novel_id),
            chapter_id=str(record.chapter_id),
            chapter_number=record.chapter_number,
            before_story_state_hash=record.before_state_hash,
            after_story_state_hash=record.after_state_hash,
            draft_hash=record.draft_hash,
            operation_count=len(record.operation_fingerprints_json),
            applied_operation_fingerprints=record.operation_fingerprints_json,
            temporary_entity_mapping=record.temporary_entity_mapping_json,
            snapshot_id=str(record.snapshot_id) if record.snapshot_id else None,
            committed_at=record.created_at,
            duplicate=duplicate,
        )
