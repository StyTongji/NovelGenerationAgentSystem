"""MemoryUpdateValidator: the authority gate for extraction candidates.

Validation layers (Phase 7 §39):
 1 schema (Pydantic, upstream)          7 domain transition (realm/owner)
 2 draft binding (scene exists)         8 knowledge vs world-truth separation
 3 evidence (spans)                     9 plot lifecycle
 4 entity reference                     10 duplicate / idempotency (fingerprint)
 5 cross-novel reference                11 plan / reveal authorization
 6 old-state precondition               12 temporary entity handling

Output: MemoryValidationReport. Nothing here mutates the database.
"""

from __future__ import annotations

import uuid
import hashlib

from pydantic import BaseModel, Field

from typing import TYPE_CHECKING

from app.draft.models import ChapterDraft

if TYPE_CHECKING:
    from app.context.models import MemoryExtractionContext
from app.memory.catalog import EvidenceCatalogBuilder, resolve_evidence_refs
from app.memory.evidence import validate_evidence_refs, validate_evidence_spans
from app.memory.models import (
    MemoryUpdateBatch,
    MemoryUpdateOperation,
    MemoryUpdateType,
)
from app.models.enums import ItemStatus, Severity
from app.services.power import PowerSystemDomain, RealmPosition


class MemoryValidationIssue(BaseModel):
    code: str
    severity: Severity
    operation_id: str | None = None
    message: str


class MemoryValidationReport(BaseModel):
    valid: bool
    issues: list[MemoryValidationIssue] = Field(default_factory=list)
    error_count: int = 0
    warning_count: int = 0
    duplicate_operation_ids: list[str] = Field(default_factory=list)
    fingerprints: list[str] = Field(default_factory=list)

    def errors(self) -> list[MemoryValidationIssue]:
        return [i for i in self.issues if i.severity in (Severity.error, Severity.critical)]


class MemoryUpdateValidator:
    def __init__(self, session) -> None:
        self.session = session

    async def validate(
        self,
        batch: MemoryUpdateBatch,
        context: "MemoryExtractionContext",
        draft: ChapterDraft,
        *,
        require_evidence_refs: bool = False,
    ) -> MemoryValidationReport:
        issues: list[MemoryValidationIssue] = []

        require_evidence_refs = require_evidence_refs or batch.contract_version == 3
        supplied_catalog = context.evidence_catalog
        catalog = EvidenceCatalogBuilder.build(draft)
        actual_draft_hash = hashlib.sha256(draft.full_text.encode("utf-8")).hexdigest()
        if (require_evidence_refs or any(op.evidence_refs for op in batch.operations)) and supplied_catalog is not None and supplied_catalog.draft_hash != actual_draft_hash:
            issues.append(MemoryValidationIssue(
                code="evidence_catalog_draft_hash_mismatch",
                severity=Severity.critical,
                message=(f"EvidenceCatalog is bound to {supplied_catalog.draft_hash[:12]}..., "
                         f"but draft is {actual_draft_hash[:12]}..."),
            ))
        if require_evidence_refs:
            from pydantic import ValidationError
            from app.memory.models import MemoryUpdateBatchV3
            wire = batch.model_dump(exclude_none=True)
            wire["contract_version"] = 3
            for op in wire["operations"]:
                op.pop("evidence", None)
            try:
                MemoryUpdateBatchV3.model_validate(wire)
            except ValidationError:
                issues.append(MemoryValidationIssue(
                    code="v3_contract_invalid", severity=Severity.critical,
                    message="Memory v3 requires explicit fact dependencies and exactly one payload",
                ))
            ids = [op.operation_id for op in batch.operations]
            if len(ids) != len(set(ids)):
                issues.append(MemoryValidationIssue(
                    code="duplicate_operation_id", severity=Severity.critical,
                    message="operation_id must be unique within a batch",
                ))
            if supplied_catalog is not None and supplied_catalog != catalog:
                issues.append(MemoryValidationIssue(
                    code="evidence_catalog_tampered", severity=Severity.critical,
                    message="Catalog differs from server reconstruction",
                ))
            if len({s.scene_id for s in draft.scenes}) != len(draft.scenes) or draft.full_text != "\n\n".join(s.text for s in draft.scenes):
                issues.append(MemoryValidationIssue(
                    code="draft_scene_text_mismatch", severity=Severity.critical,
                    message="full_text must exactly match unique ordered scene texts",
                ))
            for op in batch.operations:
                if op.knowledge and (op.knowledge.fact_ref is None or any(
                    x is not None for x in (op.knowledge.fact_id, op.knowledge.temporary_fact_ref, op.knowledge.fact_statement)
                )):
                    issues.append(self._issue("fact_ref_required", Severity.critical, op,
                                              "v3 knowledge requires only an explicit FactRef"))
                if op.story_fact and not op.story_fact.temporary_fact_ref:
                    issues.append(self._issue("temporary_fact_ref_required", Severity.critical, op,
                                              "v3 new facts require an explicit temporary ref"))
                if op.evidence and op.evidence_refs:
                    expected = resolve_evidence_refs(MemoryUpdateBatch(operations=[op]), catalog).operations[0].evidence
                    if op.evidence != expected:
                        issues.append(self._issue("resolved_evidence_tampered", Severity.critical, op,
                                                  "Resolved quote/offsets must match the server catalog"))
        issues.extend(
            MemoryValidationIssue(
                code=issue.code,
                severity=issue.severity,
                operation_id=issue.operation_id,
                message=issue.message,
            )
            for issue in validate_evidence_refs(batch.operations, catalog)
        )
        # Server-owned catalog text/offsets are authoritative. Legacy v2
        # exact_quote spans remain supported for old fixtures only.
        batch = resolve_evidence_refs(batch, catalog)

        # 3 evidence (also covers layers 1/2 partially)
        evidence_result = validate_evidence_spans(batch.operations, draft)
        for ev in evidence_result.issues:
            issues.append(
                MemoryValidationIssue(
                    code=ev.code, severity=ev.severity,
                    operation_id=ev.operation_id, message=ev.message,
                )
            )

        # scene existence (layer 2)
        scene_ids = {s.scene_id for s in draft.scenes}
        for operation in batch.operations:
            if require_evidence_refs and not operation.evidence_refs:
                issues.append(MemoryValidationIssue(
                    code="missing_evidence_ref", severity=Severity.critical,
                    operation_id=operation.operation_id,
                    message="v3 mutation must select at least one EvidenceCatalog id",
                ))
            if operation.source_scene_id not in scene_ids:
                issues.append(
                    MemoryValidationIssue(
                        code="source_scene_unknown", severity=Severity.critical,
                        operation_id=operation.operation_id,
                        message=f"source_scene_id {operation.source_scene_id!r} not in draft",
                    )
                )

        # payload presence per type (layer 1 semantics)
        for operation in batch.operations:
            if operation.payload is None:
                issues.append(
                    MemoryValidationIssue(
                        code="missing_payload", severity=Severity.critical,
                        operation_id=operation.operation_id,
                        message=f"update_type {operation.update_type.value} carries no payload",
                    )
                )

        # 4/5 entity reference + cross-novel
        for operation in batch.operations:
            issues += await self._check_references(operation, context)

        # 6 old-state precondition + 7 domain transitions
        issues += await self._check_character_states(batch, context)
        issues += await self._check_item_changes(batch, context)

        # 8 knowledge vs world truth
        issues += self._check_knowledge(batch, context, strict=require_evidence_refs)

        # 9 plot lifecycle + 11 authorization
        issues += self._check_plot_lifecycle(batch, context)

        # 12 temporary entities
        issues += self._check_proposed_entities(batch, context, draft)
        issues += self._check_item_dependencies(batch, context)

        # 10 duplicates via fingerprint
        seen: dict[str, str] = {}
        duplicates: list[str] = []
        for operation in batch.operations:
            fp = operation.fingerprint(context.novel_id, context.draft_hash)
            if fp in seen:
                duplicates.append(operation.operation_id)
                issues.append(
                    MemoryValidationIssue(
                        code="duplicate_operation", severity=Severity.warning,
                        operation_id=operation.operation_id,
                        message=f"Duplicate of operation {seen[fp]} (same fingerprint)",
                    )
                )
            else:
                seen[fp] = operation.operation_id

        # reader summary must not carry hidden truths (§89)
        issues += self._check_reader_summary(batch, context)

        errors = [i for i in issues if i.severity in (Severity.error, Severity.critical)]
        warnings = [i for i in issues if i.severity == Severity.warning]
        return MemoryValidationReport(
            valid=not errors,
            issues=sorted(issues, key=lambda i: (i.severity.value, i.code)),
            error_count=len(errors),
            warning_count=len(warnings),
            duplicate_operation_ids=duplicates,
            fingerprints=[op.fingerprint(context.novel_id, context.draft_hash) for op in batch.operations],
        )

    # ------------------------------------------------------------ references

    async def _check_references(self, operation: MemoryUpdateOperation, context: "MemoryExtractionContext") -> list[MemoryValidationIssue]:
        out: list[MemoryValidationIssue] = []
        known_ids = context.known_entity_ids()

        async def check(kind: str, entity_id: str | None) -> None:
            if not entity_id:
                return
            scope = known_ids.get(kind, set())
            if entity_id in scope:
                return
            # distinguish cross-novel from hallucinated
            if kind == "character" and await self._exists_in_other_novel("Character", entity_id, context):
                out.append(self._issue("cross_novel_reference", Severity.critical, operation, f"{kind} {entity_id} belongs to another novel"))
            elif kind == "item" and await self._exists_in_other_novel("Item", entity_id, context):
                out.append(self._issue("cross_novel_reference", Severity.critical, operation, f"{kind} {entity_id} belongs to another novel"))
            elif kind == "fact" and await self._exists_in_other_novel("StoryFact", entity_id, context):
                out.append(self._issue("cross_novel_reference", Severity.critical, operation, f"{kind} {entity_id} belongs to another novel"))
            else:
                out.append(self._issue("unknown_entity_reference", Severity.critical, operation, f"{kind} {entity_id} does not exist in this novel"))

        payload = operation.payload
        if payload is None:
            return out
        if hasattr(payload, "character_id") and payload.character_id:
            await check("character", payload.character_id)
        if hasattr(payload, "source_character_id") and payload.source_character_id:
            await check("character", payload.source_character_id)
        if hasattr(payload, "target_character_id") and payload.target_character_id:
            await check("character", payload.target_character_id)
        if hasattr(payload, "item_id") and payload.item_id:
            await check("item", payload.item_id)
        if hasattr(payload, "expected_old_owner_id") and payload.expected_old_owner_id:
            await check("character", payload.expected_old_owner_id)
        if hasattr(payload, "new_owner_id") and payload.new_owner_id:
            await check("character", payload.new_owner_id)
        if hasattr(payload, "fact_id") and payload.fact_id:
            await check("fact", payload.fact_id)
        if hasattr(payload, "fact_ref") and payload.fact_ref and payload.fact_ref.fact_id:
            await check("fact", payload.fact_ref.fact_id)
        if hasattr(payload, "plot_thread_id") and payload.plot_thread_id:
            await check("plot_thread", payload.plot_thread_id)
        if hasattr(payload, "foreshadowing_id") and payload.foreshadowing_id:
            await check("foreshadowing", payload.foreshadowing_id)
        if hasattr(payload, "plot_debt_id") and payload.plot_debt_id:
            await check("plot_debt", payload.plot_debt_id)
        if hasattr(payload, "participant_ids"):
            for pid in payload.participant_ids:
                await check("character", pid)
        if hasattr(payload, "known_by_character_ids"):
            for pid in payload.known_by_character_ids:
                await check("character", pid)
            for pid in payload.suspected_by_character_ids:
                await check("character", pid)
        return out

    async def _exists_in_other_novel(self, model_name: str, entity_id: str, context) -> bool:
        from app.models import Character, Item, StoryFact

        model = {"Character": Character, "Item": Item, "StoryFact": StoryFact}[model_name]
        try:
            entity = await self.session.get(model, uuid.UUID(entity_id))
        except ValueError:
            return False
        if entity is None:
            return False
        return str(getattr(entity, "novel_id", "")) != context.novel_id

    # ------------------------------------------------- character state 6/7

    async def _check_character_states(self, batch: MemoryUpdateBatch, context) -> list[MemoryValidationIssue]:
        out: list[MemoryValidationIssue] = []
        states = {c.character_id: c for c in context.character_states}
        location_names = {location.name for location in context.locations} | {
            op.proposed_entity.proposed_name for op in batch.operations
            if op.proposed_entity and op.proposed_entity.entity_type == "location"}
        for operation in batch.operations:
            if operation.update_type != MemoryUpdateType.character_state_update:
                continue
            payload = operation.character_state
            if payload is None:
                continue
            from app.memory.models import CANONICAL_CHARACTER_STATE_FIELDS
            if payload.field not in CANONICAL_CHARACTER_STATE_FIELDS:
                out.append(self._issue("unsupported_character_state_field", Severity.critical,
                    operation, f"Unsupported canonical character field: {payload.field}"))
                continue
            if payload.field == "location" and payload.proposed_new_value not in location_names:
                out.append(self._issue("unknown_location_name", Severity.critical, operation,
                    "Location state must name an existing or explicitly proposed canonical location"))
            current = states.get(payload.character_id)
            if current is None:
                continue
            actual_old = current.field_value(payload.field)
            if payload.expected_old_value is not None:
                if _norm(payload.expected_old_value) != _norm(actual_old):
                    out.append(self._issue(
                        "old_value_mismatch", Severity.critical, operation,
                        f"expected_old_value {payload.expected_old_value!r} does not "
                        f"match canonical {actual_old!r} for {payload.field}",
                    ))
            if payload.field == "realm":
                out.extend(await self._check_realm_transition(payload, context))
        return out

    async def _check_realm_transition(self, payload, context) -> list[MemoryValidationIssue]:
        out: list[MemoryValidationIssue] = []
        state = next(
            (c for c in context.character_states if c.character_id == payload.character_id), None
        )
        if state is None or not state.realm:
            return out
        domain = PowerSystemDomain(self.session, uuid.UUID(context.novel_id))
        try:
            current = await domain.position_of(state.realm)
            target = await domain.position_of(payload.proposed_new_value)
            await domain.validate_progression(current, target, max_jump=1)
        except Exception as exc:
            out.append(self._issue(
                "illegal_realm_transition", Severity.critical, None,
                f"Realm transition {state.realm!r} -> {payload.proposed_new_value!r}: {exc}",
            ))
        return out

    async def _check_item_changes(self, batch: MemoryUpdateBatch, context) -> list[MemoryValidationIssue]:
        """Preconditions are validated against an OVERLAY that applies earlier
        operations of the same batch in order, so intra-chapter transfer
        chains (A->B then B->C) validate correctly."""
        out: list[MemoryValidationIssue] = []
        items = {i.item_id: i for i in context.items}
        owner_overlay: dict[str, str | None] = {}
        status_overlay: dict[str, str] = {}
        for operation in batch.operations:
            if operation.update_type != MemoryUpdateType.item_update:
                continue
            payload = operation.item
            if payload is None:
                continue
            if payload.change == "status_change" and payload.new_status not in {status.value for status in ItemStatus}:
                out.append(self._issue(
                    "invalid_item_status", Severity.critical, operation,
                    "物品状态必须是 " + ", ".join(status.value for status in ItemStatus)
                    + "；开合、启封等具体事件请使用事件或事实描述。",
                ))
                continue
            key = payload.reference_key
            current = items.get(payload.item_id)
            if current is None and not payload.temporary_item_ref:
                continue
            effective_owner = owner_overlay.get(
                key,
                current.owner_id if current else None,
            )
            if payload.change == "owner_transfer":
                if (
                    payload.expected_old_owner_id is not None
                    and (effective_owner is not None or batch.contract_version == 3)
                    and payload.expected_old_owner_id != effective_owner
                ):
                    out.append(self._issue(
                        "old_value_mismatch", Severity.critical, operation,
                        f"expected_old_owner {payload.expected_old_owner_id} does not "
                        f"match current effective owner {effective_owner}",
                    ))
                if payload.new_owner_id is None:
                    out.append(self._issue(
                        "transfer_missing_new_owner", Severity.error, operation,
                        "owner_transfer requires new_owner_id",
                    ))
                owner_overlay[key] = payload.new_owner_id
            elif payload.change in ("consumed", "status_change"):
                status_overlay[key] = (
                    payload.new_status or "consumed"
                )
        return out

    # ---------------------------------------------------- knowledge (layer 8)

    def _check_knowledge(self, batch: MemoryUpdateBatch, context, *, strict=False) -> list[MemoryValidationIssue]:
        out: list[MemoryValidationIssue] = []
        existing_statements = {f.statement: f.fact_id for f in context.existing_facts}
        new_fact_statements: dict[str, tuple[str, str]] = {}
        used_temp_refs: set[str] = set()
        for index, op in enumerate(batch.operations, start=1):
            if op.update_type != MemoryUpdateType.story_fact_create or not op.story_fact:
                continue
            # Legacy v2 callers had no temp ref. Normalize deterministically;
            # v3 extractor output always declares it explicitly.
            temp_ref = op.story_fact.temporary_fact_ref or (None if strict else f"new_fact_{index}")
            if temp_ref is None:
                continue
            if not strict:
                op.story_fact.temporary_fact_ref = temp_ref
            if temp_ref in used_temp_refs:
                out.append(self._issue(
                    "duplicate_temporary_fact_ref", Severity.critical, op,
                    f"temporary_fact_ref {temp_ref!r} is declared more than once",
                ))
            used_temp_refs.add(temp_ref)
            new_fact_statements[op.story_fact.statement] = (temp_ref, op.operation_id)

        for operation in batch.operations:
            if operation.update_type != MemoryUpdateType.character_knowledge_update:
                continue
            payload = operation.knowledge
            if payload is None:
                continue
            if strict:
                payload = payload.model_copy(deep=True)
            ref = payload.fact_ref
            if ref is not None:
                if not ref.valid:
                    out.append(self._issue(
                        "fact_ref_not_exclusive", Severity.critical, operation,
                        "fact_ref must contain exactly one of fact_id or temporary_fact_ref",
                    ))
                elif ref.fact_id:
                    payload.fact_id = ref.fact_id
                    payload.temporary_fact_ref = None
                else:
                    payload.temporary_fact_ref = ref.temporary_fact_ref
                    payload.fact_id = None
            elif payload.fact_id and payload.temporary_fact_ref:
                out.append(self._issue(
                    "fact_ref_not_exclusive", Severity.critical, operation,
                    "knowledge update must reference exactly one fact id",
                ))
            if payload.fact_id is None and payload.temporary_fact_ref is None and payload.fact_statement is None:
                out.append(self._issue(
                    "knowledge_missing_fact", Severity.critical, operation,
                    "knowledge update must reference fact_id or temporary_fact_ref",
                ))
            if payload.temporary_fact_ref is not None:
                if payload.temporary_fact_ref not in used_temp_refs:
                    out.append(self._issue(
                        "knowledge_unknown_fact", Severity.critical, operation,
                        f"temporary_fact_ref {payload.temporary_fact_ref!r} matches no "
                        "story_fact_create in this batch",
                    ))
            elif not strict and payload.fact_id is None and payload.fact_statement is not None:
                if payload.fact_statement in existing_statements:
                    # normalize to existing fact (no duplicate creation)
                    payload.fact_id = existing_statements[payload.fact_statement]
                elif payload.fact_statement in new_fact_statements:
                    payload.temporary_fact_ref = new_fact_statements[payload.fact_statement][0]
                else:
                    out.append(self._issue(
                        "knowledge_unknown_fact", Severity.critical, operation,
                        f"fact_statement {payload.fact_statement!r} matches no existing "
                        "fact and no story_fact_create in this batch",
                    ))
            # suspicion must not be upgraded to known
            evidence_text = " ".join(span.exact_quote for span in operation.evidence)
            if payload.status == "known" and any(
                kw in evidence_text for kw in ("怀疑", "似乎", "隐约", "仿佛", "可能是", "觉得")
            ):
                out.append(self._issue(
                    "suspicion_upgraded_to_known", Severity.critical, operation,
                    "Evidence expresses suspicion; knowledge status must be 'suspected', not 'known'",
                ))

        # duplicate story fact creation (§37/#56)
        for operation in batch.operations:
            if operation.update_type != MemoryUpdateType.story_fact_create or not operation.story_fact:
                continue
            statement = operation.story_fact.statement
            if statement in existing_statements:
                out.append(self._issue(
                    "duplicate_story_fact", Severity.critical, operation,
                    f"Fact {statement!r} already exists in canon; emit a "
                    "character_knowledge_update instead of story_fact_create",
                ))
        return out

    # ------------------------------------------------------ plot lifecycle 9

    def _check_plot_lifecycle(self, batch: MemoryUpdateBatch, context) -> list[MemoryValidationIssue]:
        out: list[MemoryValidationIssue] = []
        threads = {t.id: t for t in context.plot_threads}
        debts = {d.id: d for d in context.open_plot_debts}
        resolved_debt_ids = set(context.resolved_plot_debt_ids)

        for operation in batch.operations:
            if operation.update_type == MemoryUpdateType.plot_thread_update and operation.plot_thread:
                thread = threads.get(operation.plot_thread.plot_thread_id)
                if thread is None:
                    out.append(self._issue(
                        "unknown_plot_thread", Severity.critical, operation,
                        "plot thread not found (cross-novel or hallucinated)",
                    ))
                    continue
                if thread.status in ("resolved", "abandoned") and operation.plot_thread.action != "resolved":
                    out.append(self._issue(
                        "resolved_thread_reopened", Severity.critical, operation,
                        f"Thread {thread.title!r} is {thread.status}; cannot advance it",
                    ))
                if operation.plot_thread.action == "resolved":
                    if thread.status in ("resolved", "abandoned"):
                        out.append(self._issue(
                            "duplicate_resolution", Severity.warning, operation,
                            "Thread already resolved",
                        ))
                    if (
                        thread.target_resolution_chapter is not None
                        and context.chapter_number < thread.target_resolution_chapter
                    ):
                        out.append(self._issue(
                            "premature_thread_resolution", Severity.critical, operation,
                            f"Planned resolution is chapter {thread.target_resolution_chapter}; "
                            f"current chapter is {context.chapter_number}",
                        ))

            if operation.update_type == MemoryUpdateType.plot_debt_update and operation.plot_debt:
                change = operation.plot_debt
                if change.action == "resolved":
                    if change.plot_debt_id in resolved_debt_ids:
                        out.append(self._issue(
                            "duplicate_debt_resolution", Severity.warning, operation,
                            "Debt already resolved; idempotent no-op",
                        ))
                    elif change.plot_debt_id and change.plot_debt_id not in debts:
                        out.append(self._issue(
                            "unknown_plot_debt", Severity.critical, operation,
                            "plot debt not open or not found",
                        ))
        return out

    # ------------------------------------------------- temporary entities 12

    def _check_proposed_entities(self, batch: MemoryUpdateBatch, context, draft) -> list[MemoryValidationIssue]:
        out: list[MemoryValidationIssue] = []
        declared = set(context.temporary_entities)
        types = {e.temporary_ref: e.entity_type for e in draft.temporary_entities}
        seen = set()
        proposed_item_names = set()
        for operation in batch.operations:
            if operation.update_type != MemoryUpdateType.propose_entity or not operation.proposed_entity:
                continue
            if operation.proposed_entity.temporary_ref not in declared:
                out.append(self._issue(
                    "undeclared_temporary_ref", Severity.critical, operation,
                    f"temporary_ref {operation.proposed_entity.temporary_ref!r} was not "
                    "declared in the chapter draft",
                ))
            ref = operation.proposed_entity.temporary_ref
            if batch.contract_version == 3:
                if ref in seen:
                    out.append(self._issue("duplicate_temporary_entity_ref", Severity.critical, operation, "Temporary entity ref declared twice in batch"))
                if types.get(ref) != operation.proposed_entity.entity_type:
                    out.append(self._issue("temporary_entity_type_mismatch", Severity.critical, operation, "Proposal type differs from Draft declaration"))
                entity = operation.proposed_entity
                if entity.entity_type == "character" and entity.attributes.get("realm"):
                    if entity.attributes["realm"] not in context.power_realms:
                        out.append(self._issue("unknown_proposed_character_realm", Severity.critical,
                            operation, "Proposed character realm must use an existing canonical realm name"))
                if entity.entity_type == "item":
                    name = entity.proposed_name or entity.attributes.get("name")
                    if name and (name in proposed_item_names or any(name == item.name or name in item.aliases for item in context.items)):
                        out.append(self._issue("duplicate_proposed_item", Severity.critical, operation,
                                               "New item proposal must not alias canonical or other proposed items"))
                    if name:
                        proposed_item_names.add(name)
            seen.add(ref)
        return out

    def _check_item_dependencies(self, batch, context):
        out = []
        proposed = {op.proposed_entity.temporary_ref: op.proposed_entity for op in batch.operations
                    if op.update_type == MemoryUpdateType.propose_entity and op.proposed_entity}
        for op in batch.operations:
            if not op.item or not op.item.temporary_item_ref:
                continue
            ref = op.item.temporary_item_ref
            entity = proposed.get(ref)
            if not entity or entity.entity_type != "item" or ref not in context.temporary_entities:
                out.append(self._issue("unknown_temporary_item_ref", Severity.critical, op,
                                       "Item dependency requires a declared item proposal in this batch"))
            elif any(entity.proposed_name == item.name or entity.proposed_name in item.aliases for item in context.items):
                out.append(self._issue("temporary_item_matches_existing", Severity.critical, op,
                                       "Use canonical item_id for an existing item; do not alias it through a proposal"))
        return out

    # -------------------------------------------------------- reader summary

    def _check_reader_summary(self, batch: MemoryUpdateBatch, context) -> list[MemoryValidationIssue]:
        out: list[MemoryValidationIssue] = []
        if not batch.candidate_reader_summary:
            return out
        for statement in context.secret_fact_statements:
            if statement in batch.candidate_reader_summary:
                out.append(self._issue(
                    "reader_summary_leaks_secret", Severity.critical, None,
                    f"candidate_reader_summary contains protected statement: {statement!r}",
                ))
                break
        return out

    def _issue(self, code, severity, operation, message) -> MemoryValidationIssue:
        return MemoryValidationIssue(
            code=code, severity=severity,
            operation_id=operation.operation_id if operation is not None else None,
            message=message,
        )


def _norm(value: str | None) -> str:
    return (value or "").strip().lower()
