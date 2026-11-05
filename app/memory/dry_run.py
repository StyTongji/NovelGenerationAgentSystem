"""MemoryDryRun: pure domain simulation of candidate operations.

Option A (preferred by the Phase 7 spec): build a before-state projection
from canonical data, apply VALIDATED operations to an in-memory dict, and
hash the result. Nothing is ever written to the database; no transaction,
no savepoint, no side effects.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, Field

from app.memory.models import (
    MemoryUpdateBatch,
    MemoryUpdateOperation,
    MemoryUpdateType,
)
from app.models.enums import ItemStatus


async def build_memory_before_state(session, novel_id, chapter_number, context):
    """Shared preview/commit projection, including canonical mental state."""
    from app.planning.provenance import build_story_state_projection
    state = await build_story_state_projection(session, novel_id)
    canonical = {character.character_id: character for character in context.character_states}
    for character in state.get("characters", []):
        current = canonical.get(character["id"])
        if current is not None:
            character["state"]["mental_condition"] = current.mental_condition
    state["chapter_number"] = chapter_number
    return state


class OperationDryRunResult(BaseModel):
    operation_id: str
    update_type: str
    status: Literal["applicable", "warning", "requires_confirmation"] = "applicable"
    message: str | None = None


class MemoryDryRunResult(BaseModel):
    applicable: bool
    operation_results: list[OperationDryRunResult] = Field(default_factory=list)
    before_hash: str
    proposed_after_hash: str
    before_state: dict
    proposed_after_state: dict
    conflicts: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    requires_confirmation: list[str] = Field(default_factory=list)


class MemoryDryRun:
    """Pure-domain state transition simulation."""

    def run(self, batch: MemoryUpdateBatch, before_state: dict) -> MemoryDryRunResult:
        after = json.loads(json.dumps(before_state, ensure_ascii=False))  # deep copy
        results: list[OperationDryRunResult] = []
        warnings: list[str] = []
        requires: list[str] = []
        conflicts: list[str] = []

        # Resolve proposed entities before dependent changes, matching commit.
        operations = sorted(batch.operations, key=lambda op: op.update_type != MemoryUpdateType.propose_entity)
        for operation in operations:
            result = self._apply(operation, after)
            results.append(result)
            if result.status == "warning":
                warnings.append(f"{operation.operation_id}: {result.message}")
            elif result.status == "requires_confirmation":
                requires.append(f"{operation.operation_id}: {result.message}")

        return MemoryDryRunResult(
            applicable=not warnings,
            operation_results=results,
            before_hash=_hash(before_state),
            proposed_after_hash=_hash(after),
            before_state=before_state,
            proposed_after_state=after,
            conflicts=conflicts,
            warnings=warnings,
            requires_confirmation=requires,
        )

    # ------------------------------------------------------------- appliers

    def _apply(self, operation: MemoryUpdateOperation, state: dict) -> OperationDryRunResult:
        handler = {
            MemoryUpdateType.create_event: self._apply_event,
            MemoryUpdateType.character_state_update: self._apply_character_state,
            MemoryUpdateType.character_knowledge_update: self._apply_knowledge,
            MemoryUpdateType.relationship_update: self._apply_relationship,
            MemoryUpdateType.item_update: self._apply_item,
            MemoryUpdateType.story_fact_create: self._apply_story_fact,
            MemoryUpdateType.plot_thread_update: self._apply_thread,
            MemoryUpdateType.foreshadowing_update: self._apply_noop,
            MemoryUpdateType.plot_debt_update: self._apply_debt,
            MemoryUpdateType.propose_entity: self._apply_propose_entity,
        }[operation.update_type]
        return handler(operation, state)

    def _ok(self, operation, message: str | None = None) -> OperationDryRunResult:
        return OperationDryRunResult(
            operation_id=operation.operation_id,
            update_type=operation.update_type.value, status="applicable", message=message,
        )

    def _warn(self, operation, message: str) -> OperationDryRunResult:
        return OperationDryRunResult(
            operation_id=operation.operation_id,
            update_type=operation.update_type.value, status="warning", message=message,
        )

    def _confirm(self, operation, message: str) -> OperationDryRunResult:
        return OperationDryRunResult(
            operation_id=operation.operation_id,
            update_type=operation.update_type.value, status="requires_confirmation",
            message=message,
        )

    def _apply_event(self, operation, state) -> OperationDryRunResult:
        payload = operation.event
        state.setdefault("candidate_events", []).append(
            {
                "event_type": payload.event_type,
                "author_summary": payload.author_summary,
                "reader_safe_summary": payload.reader_safe_summary,
                "participants": payload.participant_ids,
                "importance": payload.importance,
                "visibility": payload.visibility.model_dump(),
                "unplanned": operation.unplanned_change,
            }
        )
        return self._ok(operation)

    def _apply_character_state(self, operation, state) -> OperationDryRunResult:
        payload = operation.character_state
        for character in state.get("characters", []):
            if character["id"] == payload.character_id:
                field_map = {
                    "alive_status": "alive_status",
                    "location": "location",
                    "realm": "realm",
                    "physical_condition": "physical_condition",
                    "mental_condition": "mental_condition",
                }
                target_field = field_map.get(payload.field, payload.field)
                old = character["state"].get(target_field)
                character["state"][target_field] = payload.proposed_new_value
                message = f"{character['name']} {payload.field}: {old} -> {payload.proposed_new_value}"
                if payload.requires_confirmation or payload.field == "alive_status":
                    return self._confirm(operation, message)
                return self._ok(operation, message)
        return self._warn(operation, "character not found in state projection")

    def _apply_knowledge(self, operation, state) -> OperationDryRunResult:
        payload = operation.knowledge
        state.setdefault("knowledge_updates", []).append(
            {
                "character_id": payload.character_id,
                "fact_id": payload.fact_id,
                "fact_ref": payload.fact_ref.model_dump(exclude_none=True) if payload.fact_ref else None,
                "temporary_fact_ref": payload.temporary_fact_ref,
                "fact_statement": payload.fact_statement,
                "status": payload.status,
            }
        )
        return self._ok(operation)

    def _apply_relationship(self, operation, state) -> OperationDryRunResult:
        payload = operation.relationship
        state.setdefault("relationship_updates", []).append(
            {
                "source": payload.source_character_id,
                "target": payload.target_character_id,
                "type": payload.type,
                "note": payload.note,
            }
        )
        return self._ok(operation)

    def _apply_item(self, operation, state) -> OperationDryRunResult:
        payload = operation.item
        for item in state.get("items", []):
            if item["id"] == payload.reference_key:
                if payload.change == "owner_transfer":
                    old = item.get("owner")
                    item["owner"] = payload.new_owner_id
                    return self._ok(operation, f"item owner: {old} -> {payload.new_owner_id}")
                if payload.change == "consumed":
                    item["status"] = ItemStatus.lost.value
                    return self._ok(operation, "item consumed")
                if payload.change == "status_change":
                    if payload.new_status not in {status.value for status in ItemStatus}:
                        return self._warn(operation, "invalid item status; use a supported status or record an event/fact")
                    item["status"] = payload.new_status
                    return self._ok(operation, f"item status -> {payload.new_status}")
                if payload.change == "location_change":
                    item["location"] = payload.new_location_id
                    return self._ok(operation)
        return self._warn(operation, "item not found in state projection")

    def _apply_story_fact(self, operation, state) -> OperationDryRunResult:
        payload = operation.story_fact
        state.setdefault("new_facts", []).append(
            {
                "statement": payload.statement,
                "temporary_fact_ref": payload.temporary_fact_ref,
                "secrecy": payload.secrecy.value,
                "reader_visible": payload.reader_visible,
                "visibility": payload.visibility.model_dump(),
            }
        )
        return self._ok(operation)

    def _apply_thread(self, operation, state) -> OperationDryRunResult:
        payload = operation.plot_thread
        for thread in state.get("plot_threads", []):
            if thread["id"] == payload.plot_thread_id:
                old = thread["status"]
                if payload.action == "resolved":
                    thread["status"] = "resolved"
                elif thread["status"] in ("planned", "dormant"):
                    thread["status"] = "active"
                else:
                    thread["status"] = "progressing"
                thread["last_advanced_chapter"] = state.get("chapter_number")
                return self._ok(operation, f"thread {old} -> {thread['status']} ({payload.action})")
        return self._warn(operation, "plot thread not found in state projection")

    def _apply_noop(self, operation, state) -> OperationDryRunResult:
        payload = operation.foreshadowing
        state.setdefault("foreshadowing_updates", []).append(payload.model_dump(exclude_none=True))
        return self._ok(operation)

    def _apply_debt(self, operation, state) -> OperationDryRunResult:
        payload = operation.plot_debt
        if payload.action == "resolved":
            for debt in state.get("open_plot_debts", []):
                if debt["id"] == payload.plot_debt_id:
                    debt["status"] = "fulfilled"
                    return self._ok(operation, "debt fulfilled")
            return self._warn(operation, "open debt not found")
        state.setdefault("plot_debt_updates", []).append(payload.model_dump(exclude_none=True))
        return self._ok(operation)

    def _apply_propose_entity(self, operation, state) -> OperationDryRunResult:
        payload = operation.proposed_entity
        if payload.entity_type == "item":
            state.setdefault("items", []).append({"id": payload.temporary_ref,
                "name": payload.proposed_name, "owner": None, "location": None, "status": "intact"})
        state.setdefault("proposed_entities", []).append(
            {
                "temporary_ref": payload.temporary_ref,
                "entity_type": payload.entity_type,
                "proposed_name": payload.proposed_name,
                "attributes": payload.attributes,
            }
        )
        return self._ok(operation)


def _hash(state: dict) -> str:
    serialized = json.dumps(state, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
