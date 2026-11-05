"""ChapterPlanValidator: domain validation of a schema-valid ChapterPlan.

Pydantic validation only proves the JSON shape; this layer proves the plan
is legal against canonical story state:

A. character existence (ids resolve; temporary_refs are declared)
B. dead characters not participating in normal narrative mode
C. location existence
D. item ownership (co-occurrence heuristic)
E. ability ownership (co-occurrence heuristic)
F. realm consistency (PowerSystemDomain progression rules)
G. hard constraints (death keywords against "<name>不能死亡" constraints)
H. reveal policy (full reveal of protected secrets)
I. character knowledge (acting on facts the character does not know)
+ plot thread state, plot debt staleness, arc progression

The validator reads canonical state via repositories — it is domain code,
not an agent. The Director itself never queries the database.
"""

from __future__ import annotations

import re
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.context.models import DirectorContext
from app.models import Ability, Character, Item, Location, PlotDebt, PlotThread, StoryFact
from app.models.enums import KnowledgeStatus, SecrecyLevel, Severity
from app.planning.models import PlanIssue, PlanValidationReport
from app.repositories.character import CharacterRepository
from app.repositories.novel import FactRepository
from app.repositories.plot import DebtRepository, PlotRepository
from app.repositories.world import ItemRepository, LocationRepository, PowerSystemRepository
from app.schemas.agent_outputs import ChapterPlan
from app.services.power import PowerSystemDomain, RealmPosition

DEATH_KEYWORDS = ("死亡", "战死", "毙命", "陨落", "身亡", "死了", "死去", "丧命", "命丧")

# text fields scanned by co-occurrence heuristics, as (text, field label)
def _plan_text_fields(plan: ChapterPlan) -> list[tuple[str, str]]:
    fields: list[tuple[str, str]] = [(plan.chapter_goal, "chapter_goal")]
    if plan.ending_hook:
        fields.append((plan.ending_hook, "ending_hook"))
    for event in plan.required_events:
        fields.append((event, "required_events"))
    for goal in plan.character_goals:
        fields.append((goal.goal, "character_goals"))
    for expectation in plan.character_state_expectations:
        fields.append((expectation.expectation, "character_state_expectations"))
    for scene in plan.scenes:
        fields.append((scene.goal, f"scene[{scene.index}].goal"))
        for beat in scene.beats:
            fields.append((beat, f"scene[{scene.index}].beats"))
    return fields


def _co_occurs(text: str, *terms: str) -> bool:
    return all(len(term) >= 2 and term in text for term in terms)


class ChapterPlanValidator:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.characters = CharacterRepository(session)
        self.items = ItemRepository(session)
        self.locations = LocationRepository(session)
        self.threads = PlotRepository(session)
        self.debts = DebtRepository(session)
        self.facts = FactRepository(session)
        self.power_repo = PowerSystemRepository(session)

    async def validate(self, plan: ChapterPlan, context: DirectorContext) -> PlanValidationReport:
        issues: list[PlanIssue] = []
        issues += await self._check_characters(plan, context)
        issues += await self._check_locations(plan, context)
        issues += await self._check_items(plan, context)
        issues += await self._check_abilities(plan, context)
        issues += await self._check_realms(plan, context)
        issues += await self._check_hard_constraints(plan, context)
        issues += await self._check_reveal_policy(plan, context)
        issues += await self._check_knowledge(plan, context)
        issues += await self._check_plot_threads(plan, context)
        issues += self._check_plot_debts(plan, context)
        issues += self._check_progression(plan, context)
        issues += self._check_prohibited_statements(plan)
        valid = not any(i.severity in (Severity.error, Severity.critical) for i in issues)
        issues.sort(key=lambda i: (i.severity.value, i.code, i.message))
        return PlanValidationReport(valid=valid, issues=issues)

    # ------------------------------------------------------------- helpers

    def _issue(self, code, severity, message, **kw) -> PlanIssue:
        return PlanIssue(code=code, severity=severity, message=message, **kw)

    async def _characters_in_novel(self, context: DirectorContext) -> list[Character]:
        return await self.characters.list_for_novel(uuid.UUID(context.novel_id))

    def _temporary_refs(self, plan: ChapterPlan) -> set[str]:
        return {p.temporary_ref for p in plan.proposed_entities if p.temporary_ref}

    # ----------------------------------------------------------------- A, B

    async def _check_characters(self, plan: ChapterPlan, context: DirectorContext) -> list[PlanIssue]:
        issues: list[PlanIssue] = []
        all_characters = await self._characters_in_novel(context)
        by_id = {str(c.id): c for c in all_characters}
        by_name = {c.name: c for c in all_characters}
        temp_refs = self._temporary_refs(plan)

        # A: id resolution
        for ref in plan.participating_characters:
            if ref.character_id:
                if ref.character_id not in by_id:
                    issues.append(self._issue(
                        "unknown_character_id", Severity.critical,
                        f"Participating character id {ref.character_id} does not exist",
                        entity_type="character", entity_id=ref.character_id,
                        suggested_fix="Use a real character_id from context, or declare a proposed_entity with temporary_ref",
                    ))
            elif ref.temporary_ref and ref.temporary_ref not in temp_refs:
                issues.append(self._issue(
                    "undeclared_temporary_ref", Severity.error,
                    f"temporary_ref {ref.temporary_ref!r} is not declared in proposed_entities",
                    suggested_fix="Add a proposed_entities entry for this temporary_ref",
                ))

        # scene participants: name / temporary_ref / proposed name_hint all allowed
        known_names = set(by_name) | {
            alias for c in all_characters for alias in (c.aliases or [])
        }
        hints = {p.name_hint for p in plan.proposed_entities if p.name_hint}
        hints |= temp_refs
        for scene in plan.scenes:
            for participant in scene.participants:
                character: Character | None = by_name.get(participant)
                if character is None:
                    for c in all_characters:
                        if participant in (c.aliases or []):
                            character = c
                            break
                if character is None and participant not in hints:
                    issues.append(self._issue(
                        "unknown_participant", Severity.warning,
                        f"Scene {scene.index} participant {participant!r} matches no canonical "
                        "character or proposed entity",
                        entity_type="character",
                        suggested_fix="Reference an existing character, or propose the entity",
                    ))
                    continue
                # B: dead characters in normal narrative mode
                if character is not None:
                    state = await self.characters.current_state(character.id)
                    alive = (
                        state.alive_status.value
                        if state and hasattr(state.alive_status, "value")
                        else (str(state.alive_status) if state else "alive")
                    )
                    mode = scene.narrative_mode or "normal"
                    if alive == "dead" and mode == "normal":
                        issues.append(self._issue(
                            "dead_character_active", Severity.critical,
                            f"Dead character {character.name!r} participates in scene "
                            f"{scene.index} with normal narrative mode",
                            entity_type="character", entity_id=str(character.id),
                            evidence=[f"canonical state: alive_status=dead"],
                            suggested_fix="Remove the character, or set narrative_mode to flashback/illusion/corpse/remnant",
                        ))
        return issues

    # -------------------------------------------------------------------- C

    async def _check_locations(self, plan: ChapterPlan, context: DirectorContext) -> list[PlanIssue]:
        issues: list[PlanIssue] = []
        novel_id = uuid.UUID(context.novel_id)
        known = {loc.name for loc in await self.locations.list_for_novel(novel_id)}
        hints = {p.name_hint for p in plan.proposed_entities if p.name_hint}
        hints |= {p.temporary_ref for p in plan.proposed_entities}
        for ref in plan.primary_locations:
            label = ref.name or ref.temporary_ref or ""
            if ref.character_id:  # misuse, tolerated silently
                continue
            if label and label not in known and label not in hints:
                issues.append(self._issue(
                    "unknown_location", Severity.error,
                    f"Primary location {label!r} does not exist in canon",
                    entity_type="location",
                    suggested_fix="Use an existing location or propose it as a new entity",
                ))
        for scene in plan.scenes:
            if scene.location and scene.location not in known and scene.location not in hints:
                issues.append(self._issue(
                    "unknown_location", Severity.error,
                    f"Scene {scene.index} location {scene.location!r} does not exist in canon",
                    entity_type="location", evidence=[scene.goal],
                    suggested_fix="Use an existing location or propose it as a new entity",
                ))
        return issues

    # ----------------------------------------------------------------- D, E

    def _participating_names(self, plan: ChapterPlan) -> set[str]:
        names = {ref.name for ref in plan.participating_characters if ref.name}
        for scene in plan.scenes:
            names.update(scene.participants)
        return {n for n in names if n}

    def _participating_ids(self, plan: ChapterPlan) -> set[str]:
        return {ref.character_id for ref in plan.participating_characters if ref.character_id}

    async def _check_items(self, plan: ChapterPlan, context: DirectorContext) -> list[PlanIssue]:
        issues: list[PlanIssue] = []
        novel_id = uuid.UUID(context.novel_id)
        participating_names = self._participating_names(plan)
        participating_ids = self._participating_ids(plan)
        fields = _plan_text_fields(plan)
        for item in await self.items.list_for_novel(novel_id):
            if item.current_owner_character_id is None:
                continue
            owner_id = str(item.current_owner_character_id)
            if owner_id in participating_ids:
                continue
            for text, field_label in fields:
                used_by = next(
                    (name for name in participating_names if name in text and name != ""),
                    None,
                )
                if used_by and _co_occurs(text, item.name):
                    issues.append(self._issue(
                        "item_ownership_violation", Severity.critical,
                        f"Plan has {used_by!r} using item {item.name!r} owned by someone else",
                        entity_type="item", entity_id=str(item.id),
                        evidence=[f"{field_label}: {text}"],
                        suggested_fix="Transfer ownership first in the story, or remove the usage",
                    ))
                    break
        return issues

    async def _check_abilities(self, plan: ChapterPlan, context: DirectorContext) -> list[PlanIssue]:
        issues: list[PlanIssue] = []
        novel_id = uuid.UUID(context.novel_id)
        participating_names = self._participating_names(plan)
        participating_ids = self._participating_ids(plan)
        fields = _plan_text_fields(plan)
        result = await self.session.execute(
            __import__("sqlalchemy", fromlist=["select"]).select(Ability).where(
                Ability.novel_id == novel_id, Ability.owner_character_id.is_not(None)
            )
        )
        for ability in result.scalars():
            if str(ability.owner_character_id) in participating_ids:
                continue
            owner = await self.session.get(Character, ability.owner_character_id)
            owner_name = owner.name if owner else "?"
            for text, field_label in fields:
                used_by = next((name for name in participating_names if name in text), None)
                if used_by and used_by != owner_name and _co_occurs(text, ability.name):
                    issues.append(self._issue(
                        "ability_ownership_violation", Severity.critical,
                        f"Plan has {used_by!r} using ability {ability.name!r} "
                        f"mastered by {owner_name!r}",
                        entity_type="ability", entity_id=str(ability.id),
                        evidence=[f"{field_label}: {text}"],
                        suggested_fix="Remove the usage or have the character learn it first",
                    ))
                    break
        return issues

    # -------------------------------------------------------------------- F

    async def _check_realms(self, plan: ChapterPlan, context: DirectorContext) -> list[PlanIssue]:
        issues: list[PlanIssue] = []
        novel_id = uuid.UUID(context.novel_id)
        state_by_name = {v.name: v for v in context.active_characters}
        fields = _plan_text_fields(plan)
        for character in await self._characters_in_novel(context):
            view = state_by_name.get(character.name)
            if view is None or view.state is None or not view.state.realm:
                continue
            current_realm = await self.power_repo.find_realm_by_name(novel_id, view.state.realm)
            if current_realm is None:
                continue
            for text, field_label in fields:
                if character.name not in text:
                    continue
                for target_realm in await self._realms(novel_id):
                    if target_realm.name in text and target_realm.rank > current_realm.rank:
                        try:
                            domain = PowerSystemDomain(self.session, novel_id)
                            await domain.validate_progression(
                                RealmPosition(rank=current_realm.rank, name=current_realm.name),
                                RealmPosition(rank=target_realm.rank, name=target_realm.name),
                                max_jump=1,
                            )
                        except Exception as exc:
                            issues.append(self._issue(
                                "realm_jump_violation", Severity.critical,
                                f"Plan implies {character.name!r} reaches {target_realm.name!r}: {exc}",
                                entity_type="character", entity_id=str(character.id),
                                evidence=[f"{field_label}: {text}"],
                                suggested_fix="Keep progression adjacent to the current realm, or plan a proper breakthrough arc",
                            ))
        return issues

    async def _realms(self, novel_id):
        from app.models import PowerSystem
        from sqlalchemy import select

        result = await self.session.execute(
            select(PowerSystem).where(PowerSystem.novel_id == novel_id)
        )
        power_system = result.scalars().first()
        if power_system is None:
            return []
        return await self.power_repo.realms_for(power_system.id)

    # -------------------------------------------------------------------- G

    async def _check_hard_constraints(self, plan: ChapterPlan, context: DirectorContext) -> list[PlanIssue]:
        issues: list[PlanIssue] = []
        # constraints listed in context
        protected_names: set[str] = set()
        for constraint in context.hard_constraints:
            match = re.search(r"([\u4e00-\u9fa5]{2,6}?)(?:不能死亡|不得死亡|不能死|不许死)", constraint.statement)
            if match:
                protected_names.add(match.group(1))

        fields = _plan_text_fields(plan)
        for name in protected_names:
            for text, field_label in fields:
                if name in text and any(kw in text for kw in DEATH_KEYWORDS):
                    issues.append(self._issue(
                        "hard_constraint_violation", Severity.critical,
                        f"Plan appears to kill protected character {name!r}, "
                        "violating a hard constraint",
                        entity_type="character",
                        evidence=[f"{field_label}: {text}"],
                        suggested_fix=f"Keep {name} alive; adjust the plan or the user direction",
                    ))
        return issues

    # -------------------------------------------------------------------- H

    async def _check_reveal_policy(self, plan: ChapterPlan, context: DirectorContext) -> list[PlanIssue]:
        issues: list[PlanIssue] = []
        novel_id = uuid.UUID(context.novel_id)
        all_facts = {str(f.id): f for f in await self.facts.list_for_novel(novel_id)}
        hard_statements = [c.statement for c in context.hard_constraints]

        for revelation in plan.revelations:
            if revelation.policy != "full":
                continue
            fact = None
            if revelation.fact_id:
                fact = all_facts.get(revelation.fact_id)
            elif revelation.statement:
                fact = next(
                    (f for f in all_facts.values() if f.statement == revelation.statement), None
                )
            if fact is None:
                continue
            secrecy = fact.secrecy.value if hasattr(fact.secrecy, "value") else str(fact.secrecy)
            if secrecy not in (SecrecyLevel.secret.value, SecrecyLevel.top_secret.value):
                continue
            # protected by an overlapping hard constraint?
            protected = _statements_overlap(fact.statement, hard_statements)
            if protected:
                issues.append(self._issue(
                    "reveal_policy_violation", Severity.critical,
                    "Plan fully reveals a protected secret that hard constraints "
                    "still guard at this chapter",
                    entity_type="story_fact", entity_id=str(fact.id),
                    evidence=[fact.statement],
                    suggested_fix="Downgrade to hint_only/partial, or move the reveal past the constraint window",
                ))
            else:
                issues.append(self._issue(
                    "full_secret_reveal", Severity.warning,
                    "Plan fully reveals a secret fact; confirm this is intended",
                    entity_type="story_fact", entity_id=str(fact.id),
                    evidence=[fact.statement],
                ))
        return issues

    # -------------------------------------------------------------------- I

    async def _check_knowledge(self, plan: ChapterPlan, context: DirectorContext) -> list[PlanIssue]:
        issues: list[PlanIssue] = []
        novel_id = uuid.UUID(context.novel_id)
        all_facts = await self.facts.list_for_novel(novel_id)
        secrets = [
            f for f in all_facts
            if (f.secrecy.value if hasattr(f.secrecy, "value") else str(f.secrecy))
            in (SecrecyLevel.secret.value, SecrecyLevel.top_secret.value)
        ]
        by_name = {c.name: c for c in await self._characters_in_novel(context)}
        fields = _plan_text_fields(plan)

        for name, character in by_name.items():
            knowledge_rows = await self.characters.knowledge_of(character.id)
            aware_ids = {
                str(row.fact_id)
                for row in knowledge_rows
                if row.status != KnowledgeStatus.forgotten
            }
            for fact in secrets:
                if str(fact.id) in aware_ids:
                    continue
                for text, field_label in fields:
                    if _co_occurs(text, name, fact.statement):
                        issues.append(self._issue(
                            "knowledge_violation", Severity.critical,
                            f"Plan makes {name!r} act on a secret {name!r} does not know",
                            entity_type="character", entity_id=str(character.id),
                            evidence=[f"{field_label}: {text}"],
                            suggested_fix="Base the behavior on what the character knows/suspects, "
                                          "or have them learn the fact earlier in the chapter",
                        ))
                        break
        return issues

    # -------------------------------------------------------- plot threads

    async def _check_plot_threads(self, plan: ChapterPlan, context: DirectorContext) -> list[PlanIssue]:
        issues: list[PlanIssue] = []
        novel_id = uuid.UUID(context.novel_id)
        all_threads = {str(t.id): t for t in await self.threads.list_threads(novel_id)}
        for instruction in plan.plot_threads_to_progress:
            thread = None
            if instruction.plot_thread_id:
                thread = all_threads.get(instruction.plot_thread_id)
                if thread is None:
                    issues.append(self._issue(
                        "unknown_plot_thread_id", Severity.critical,
                        f"plot_thread_id {instruction.plot_thread_id} does not exist",
                        entity_type="plot_thread", entity_id=instruction.plot_thread_id,
                    ))
                    continue
            elif instruction.title:
                thread = next(
                    (t for t in all_threads.values() if t.title == instruction.title), None
                )
                if thread is None:
                    issues.append(self._issue(
                        "unknown_plot_thread", Severity.error,
                        f"Plot thread {instruction.title!r} does not exist",
                        entity_type="plot_thread",
                    ))
                    continue
            status = thread.status.value if hasattr(thread.status, "value") else str(thread.status)
            if status in ("resolved", "abandoned") and instruction.action != "resolve":
                issues.append(self._issue(
                    "resolved_thread_reopened", Severity.error,
                    f"Plan treats {thread.title!r} (status={status}) as an active mystery",
                    entity_type="plot_thread", entity_id=str(thread.id),
                    suggested_fix="Introduce a new thread, or plan a proper continuation with a new hook",
                ))
            if status == "active" and instruction.action == "resolve":
                truth = thread.truth or ""
                if thread.target_resolution_chapter and context.chapter_number < thread.target_resolution_chapter:
                    issues.append(self._issue(
                        "premature_thread_resolution", Severity.error,
                        f"Plan resolves {thread.title!r} at chapter {context.chapter_number}, "
                        f"planned resolution is chapter {thread.target_resolution_chapter}",
                        entity_type="plot_thread", entity_id=str(thread.id),
                        suggested_fix="Advance or echo the thread instead; keep the resolution for its planned chapter",
                    ))
        return issues

    # ----------------------------------------------------------- plot debts

    def _check_plot_debts(self, plan: ChapterPlan, context: DirectorContext) -> list[PlanIssue]:
        issues: list[PlanIssue] = []
        addressed_ids = {i.plot_debt_id for i in plan.plot_debts_to_progress if i.plot_debt_id}
        for debt in context.open_plot_debts:
            if debt.urgency == "overdue" and debt.id not in addressed_ids:
                issues.append(self._issue(
                    "overdue_plot_debt_ignored", Severity.warning,
                    f"Overdue plot debt not addressed: {debt.description}",
                    entity_type="plot_debt", entity_id=debt.id,
                    suggested_fix="Consider addressing it or explicitly defer it",
                ))
        return issues

    # ------------------------------------------------------------------ arc

    def _check_progression(self, plan: ChapterPlan, context: DirectorContext) -> list[PlanIssue]:
        issues: list[PlanIssue] = []
        if not plan.plot_threads_to_progress and not plan.foreshadowing_actions:
            issues.append(self._issue(
                "no_progression", Severity.warning,
                "Plan advances no plot thread and no foreshadowing",
                suggested_fix="Add at least one thread/foreshadowing instruction, unless a pure breather chapter is intended",
            ))
        return issues

    # ------------------------------------------------- prohibited statements

    def _check_prohibited_statements(self, plan: ChapterPlan) -> list[PlanIssue]:
        issues: list[PlanIssue] = []
        prohibited = [p.statement for p in plan.prohibited_revelations] + list(plan.must_not_reveal)
        if not prohibited:
            return issues
        for text, field_label in _plan_text_fields(plan):
            for statement in prohibited:
                if statement and statement in text:
                    issues.append(self._issue(
                        "prohibited_reveal_in_plan", Severity.critical,
                        "Plan's own text contains a statement marked must-not-reveal",
                        evidence=[f"{field_label}: {text}"],
                        suggested_fix="Rewrite the beat to hint instead of state the secret",
                    ))
        return issues


_OVERLAP_STOP = set("的了吗呢吧呀哦是在和与以及对于被将其把从向")


def _statements_overlap(statement: str, candidates: list[str]) -> bool:
    """True if any candidate shares a meaningful bigram with the statement."""
    from app.retrieval.scoring import _bigrams

    statement_bigrams = _bigrams(statement)
    if not statement_bigrams:
        return False
    for candidate in candidates:
        shared = statement_bigrams & _bigrams(candidate)
        # require >=2 shared bigrams to avoid trivial hits like "父亲"
        meaningful = {
            b for b in shared
            if all(ch not in _OVERLAP_STOP for ch in b)
        }
        if len(meaningful) >= 2:
            return True
    return False
