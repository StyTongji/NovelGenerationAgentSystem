"""DeterministicContinuityChecker: rule-based continuity verification.

Every rule is precise, documented and unit-testable. The LLM semantic
checker supplements but NEVER overrides these findings.

Rules implemented (TASKS Phase 6 / Phase 6 spec §9-§12):
  A  dead_character        dead canonical character acting normally in a scene
                           whose plan narrative_mode is normal
  B  item_ownership        item used by a scene without its owner present
  C  ability_ownership     technique used by a character who has not learned it
  D  realm_state           prose contradicts canonical realm
  E  illegal_breakthrough  breakthrough beyond adjacent rank or unauthorized
                           by the plan
  F  hard_constraint       protected character dies in prose
  G  knowledge_leak        POV-unknown secret statement appears verbatim
  H  reveal_policy         forbidden_information appears verbatim in prose
  I  plot_thread_state     resolved thread treated as an unsolved mystery
  J  proposed_entity_identity  contradictory identity markers for one ref
  T  timeline              explicit story_time regression between scenes
  L  location_travel       instant travel between distant locations
  S  scene_continuity      repeated "first meeting" / re-acquisition
"""

from __future__ import annotations

import re
import uuid

from app.continuity.models import ContinuityCategory, ContinuityIssue
from app.context.models import ContinuityContext
from app.models.enums import Severity
from app.schemas.agent_outputs import EntityRef, ScenePlan
from app.services.power import PowerSystemDomain, RealmPosition

DEATH_KEYWORDS = ("死亡", "战死", "毙命", "陨落", "身亡", "死了", "死去", "丧命", "命丧", "魂飞魄散")
ITEM_USE_KEYWORDS = ("取出", "使用", "催动", "祭出", "召出", "握着", "手持", "祭起")
ABILITY_USE_KEYWORDS = ("施展", "使出", "运转", "催动", "施展出来", "施法", "运起")
BREAKTHROUGH_KEYWORDS = ("突破", "晋升", "踏入", "晋入", "凝结金丹", "结丹")
INSTANT_TRAVEL_PATTERN = re.compile(r"(片刻|瞬间|一瞬|一息|十息|刹那|转眼)[^。]*万里|万里[^。]*(片刻|瞬间|一息|十息|刹那|转眼)")
TELEPORT_JUSTIFICATION = ("传送阵", "瞬移", "空间法宝", "挪移", "遁光", "御剑", "遁符")

_MALE_TERMS = ("老者", "老翁", "男子", "男人", "少年", "青年", "老者")
_FEMALE_TERMS = ("女子", "少女", "女人", "姑娘")
_ELDER_TERMS = ("老者", "老翁", "老妪")

_checker_version = "deterministic_v1"


def _issue(
    category: ContinuityCategory,
    severity: Severity,
    scene_id: str | None,
    explanation: str,
    *,
    evidence: str | None = None,
    entity_refs: list[EntityRef] | None = None,
    suggested: str | None = None,
    index: int = 0,
) -> ContinuityIssue:
    return ContinuityIssue(
        issue_id=f"det-{category.value}-{scene_id or 'chapter'}-{index}",
        category=category,
        severity=severity,
        scene_id=scene_id,
        entity_refs=entity_refs or [],
        evidence_excerpt=evidence[:300] if evidence else None,
        author_explanation=explanation,
        suggested_action=suggested,
        source="deterministic",
        blocking=severity in (Severity.error, Severity.critical),
    )


class DeterministicContinuityChecker:
    version = _checker_version

    def check(self, context: ContinuityContext) -> list[ContinuityIssue]:
        issues: list[ContinuityIssue] = []
        scene_plans = {sp.scene_id: sp for sp in context.scene_plans}
        scenes = _scene_texts(context, scene_plans)

        issues += self._check_dead_characters(context, scenes)
        issues += self._check_items(context, scenes)
        issues += self._check_abilities(context, scenes)
        issues += self._check_realm_state(context, scenes)
        issues += self._check_breakthroughs(context, scenes)
        issues += self._check_hard_constraints(context, scenes)
        issues += self._check_knowledge_leak(context, scenes)
        issues += self._check_reveal_policy(context, scenes)
        issues += self._check_plot_threads(context, scenes)
        issues += self._check_proposed_entities(context, scenes)
        issues += self._check_timeline(context)
        issues += self._check_travel(context, scenes)
        issues += self._check_scene_continuity(context, scenes)
        return issues


def _scene_texts(context: ContinuityContext, scene_plans: dict[str, ScenePlan]):
    """Map scene_id -> (text, ScenePlan | None). Falls back to draft scenes
    when plans are missing."""
    out: list[tuple[str, str, ScenePlan | None]] = []
    plan_by_index = {sp.scene_index: sp for sp in context.scene_plans}
    for scene in context.draft.scenes:
        plan = scene_plans.get(scene.scene_id) or plan_by_index.get(scene.scene_index)
        out.append((scene.scene_id, scene.text, plan))
    return out


class DeterministicContinuityCheckerMixin:
    pass


# --------------------------------------------------------------------- A

def _check_dead_characters(self, context, scenes):
    issues = []
    dead = [
        v for v in context.canonical_characters
        if v.state and v.state.alive_status == "dead"
    ]
    for index, (scene_id, text, plan) in enumerate(scenes):
        mode = (plan.narrative_mode if plan else None) or "normal"
        if mode != "normal":
            continue
        for character in dead:
            if character.name in text:
                issues.append(_issue(
                    ContinuityCategory.dead_character, Severity.critical, scene_id,
                    f"Dead character {character.name!r} appears as a normal living "
                    "person in a normal-narrative scene",
                    evidence=text[max(0, text.find(character.name) - 40): text.find(character.name) + 60],
                    entity_refs=[EntityRef(character_id=character.id, name=character.name)],
                    suggested="Remove the appearance or mark the scene narrative_mode as flashback/illusion/corpse/remnant",
                    index=index,
                ))
    return issues


# --------------------------------------------------------------------- B

def _check_items(self, context, scenes):
    issues = []
    for index, (scene_id, text, plan) in enumerate(scenes):
        for item in context.items:
            if item.owner_id is None:
                continue
            names = [item.name]
            if not any(name in text for name in names):
                continue
            if not any(kw in text for kw in ITEM_USE_KEYWORDS):
                continue
            owner = next(
                (v for v in context.canonical_characters if v.id == item.owner_id), None
            )
            owner_name = owner.name if owner else "?"
            # a non-owner canonical character appears in the text together
            # with a use keyword -> suspected unauthorized use
            user = next(
                (v.name for v in context.canonical_characters
                 if v.name != owner_name and v.name in text),
                None,
            )
            if user is not None and owner_name not in text:
                issues.append(_issue(
                    ContinuityCategory.item_ownership, Severity.error, scene_id,
                    f"{user!r} appears to use item {item.name!r} whose canonical "
                    f"owner is {owner_name!r}, with no transfer established",
                    evidence=text[:200],
                    entity_refs=[EntityRef(name=item.name)],
                    suggested="Remove the usage, or have the owner present, or establish a transfer first",
                    index=index,
                ))
    return issues


def _scene_participants(context, plan):
    """CharacterViews present in the scene (by plan participants or POV)."""
    if plan is None:
        return context.canonical_characters
    names = {plan.pov.name} if plan.pov.name else set()
    names |= {p.name for p in plan.participants if p.name}
    return [v for v in context.canonical_characters if v.name in names]


# --------------------------------------------------------------------- C

def _check_abilities(self, context, scenes):
    issues = []
    for index, (scene_id, text, plan) in enumerate(scenes):
        for ability_entry in context.abilities:
            # entries shaped "ability_name|owner_name"
            ability_name, _, owner_name = ability_entry.partition("|")
            if ability_name not in text:
                continue
            if not any(kw in text for kw in ABILITY_USE_KEYWORDS):
                continue
            participants = {v.name for v in _scene_participants(context, plan)}
            if owner_name in participants:
                continue
            user = next(
                (v.name for v in context.canonical_characters
                 if v.name != owner_name and v.name in text),
                None,
            )
            if user is None:
                continue
            issues.append(_issue(
                ContinuityCategory.ability_ownership, Severity.error, scene_id,
                f"{user!r} uses ability {ability_name!r} which is mastered by "
                f"{owner_name!r} and never learned by {user!r}",
                evidence=text[:200],
                suggested="Remove the usage or establish that the character learned it",
                index=index,
            ))
    return issues


# ----------------------------------------------------------------- D, E

def _check_realm_state(self, context, scenes):
    issues = []
    realms = _parse_realms(context.power_realms)
    for index, (scene_id, text, plan) in enumerate(scenes):
        for view in context.canonical_characters:
            if view.state is None or not view.state.realm:
                continue
            if view.name not in text:
                continue
            for realm_name, rank in realms.items():
                if realm_name == view.state.realm:
                    continue
                for pattern in (f"{realm_name}境的{view.name}", f"身为{realm_name}修士的{view.name}",
                                f"{view.name}已是{realm_name}", f"{view.name}身为{realm_name}"):
                    if pattern in text:
                        issues.append(_issue(
                            ContinuityCategory.realm_state, Severity.error, scene_id,
                            f"Prose describes {view.name!r} as {realm_name!r} but "
                            f"canonical state is {view.state.realm!r}",
                            evidence=pattern,
                            entity_refs=[EntityRef(name=view.name)],
                            suggested="Fix the prose to match canonical realm",
                            index=index,
                        ))
                        break
    return issues


def _parse_realms(entries: list[str]) -> dict[str, int]:
    realms = {}
    for entry in entries:
        name, _, rank = entry.rpartition("(")
        if name and rank:
            realms[name] = int(rank.rstrip(")"))
    return realms


def _check_breakthroughs(self, context, scenes):
    issues = []
    realms = _parse_realms(context.power_realms)
    current_by_name = {
        v.name: v.state.realm for v in context.canonical_characters if v.state and v.state.realm
    }
    # text where the plan declares a breakthrough is allowed
    plan_text = " ".join(
        [context.chapter_plan.expected_state_changes if context.chapter_plan else []][0]
        if context.chapter_plan else []
    )
    for scene in context.scene_plans:
        plan_text += " " + " ".join(scene.required_outcomes) + " " + " ".join(scene.required_actions)

    for index, (scene_id, text, plan) in enumerate(scenes):
        if not any(kw in text for kw in BREAKTHROUGH_KEYWORDS):
            continue
        for view in context.canonical_characters:
            if view.name not in text or not view.state or not view.state.realm:
                continue
            current_rank = realms.get(view.state.realm)
            if current_rank is None:
                continue
            for realm_name, rank in realms.items():
                if realm_name not in text or rank <= current_rank:
                    continue
                adjacent = rank - current_rank == 1
                authorized = view.name in plan_text and realm_name in plan_text
                if not adjacent:
                    issues.append(_issue(
                        ContinuityCategory.illegal_breakthrough, Severity.critical, scene_id,
                        f"Prose has {view.name!r} breaking through to {realm_name!r}, "
                        f"more than one rank above canonical {view.state.realm!r}",
                        evidence=text[:200],
                        suggested="Remove the breakthrough or plan a proper progression arc",
                        index=index,
                    ))
                elif not authorized:
                    issues.append(_issue(
                        ContinuityCategory.illegal_breakthrough, Severity.critical, scene_id,
                        f"Prose has {view.name!r} breaking through to {realm_name!r} "
                        "but the chapter plan never authorizes this breakthrough",
                        evidence=text[:200],
                        suggested="Remove the breakthrough or add it to the plan explicitly",
                        index=index,
                    ))
    return issues


# --------------------------------------------------------------------- F

def _check_hard_constraints(self, context, scenes):
    issues = []
    protected: set[str] = set()
    for constraint in context.hard_constraints:
        match = re.search(r"([\u4e00-\u9fa5]{2,6}?)(?:不能死亡|不得死亡|不能死|不许死)", constraint.statement)
        if match:
            protected.add(match.group(1))
    for index, (scene_id, text, plan) in enumerate(scenes):
        for name in protected:
            if name in text and any(kw in text for kw in DEATH_KEYWORDS):
                issues.append(_issue(
                    ContinuityCategory.hard_constraint, Severity.critical, scene_id,
                    f"Prose kills {name!r}, violating the hard constraint "
                    "that the character must not die",
                    evidence=text[:200],
                    suggested="Keep the character alive (injury/incapacitation is allowed)",
                    index=index,
                ))
    return issues


# ------------------------------------------------------------------ G, H

def _check_knowledge_leak(self, context, scenes):
    """Verbatim secret statement in a scene whose POV is unaware."""
    issues = []
    pov_name_by_scene = {}
    for scene_id, _text, plan in scenes:
        if plan is not None:
            pov_name_by_scene[scene_id] = plan.pov.name or plan.pov.temporary_ref
    aware_by_name: dict[str, set[str]] = {}
    for row in context.character_knowledge:
        aware_by_name.setdefault(row.character_name, set())
        if row.status != "forgotten":
            aware_by_name[row.character_name].add(row.statement)
    secret_statements = set(context.secret_fact_statements)

    for index, (scene_id, text, plan) in enumerate(scenes):
        pov = pov_name_by_scene.get(scene_id)
        if pov is None:
            continue
        aware = aware_by_name.get(pov, set())
        for statement in secret_statements:
            if statement in aware:
                continue
            # check the statement and its possessive-stripped core
            candidates = {statement}
            for view in context.canonical_characters:
                if statement.startswith(f"{view.name}的"):
                    candidates.add(statement[len(view.name) + 1:])
            offending = next((c for c in candidates if c in text), None)
            if offending is not None:
                from app.continuity.repair_redaction import _sentence_bounds

                position = text.find(offending)
                start, end = _sentence_bounds(text, position, position + len(offending))
                issues.append(_issue(
                    ContinuityCategory.knowledge_leak, Severity.critical, scene_id,
                    f"Scene prose states a secret verbatim while POV {pov!r} "
                    "does not know it",
                    evidence=text[start:end],
                    suggested="Remove the leak or have the POV learn the fact first",
                    index=index,
                ))
    return issues


def _check_reveal_policy(self, context, scenes):
    """hint_only / partial secrets stated verbatim in prose (deterministic
    layer; paraphrase detection belongs to the LLM checker)."""
    issues = []
    plan = context.chapter_plan
    if plan is None:
        return issues
    forbidden = []
    policy_by_statement = {}
    for revelation in plan.revelations:
        if revelation.forbidden_information:
            forbidden.append(revelation.forbidden_information)
            policy_by_statement[revelation.forbidden_information] = revelation.policy
    for index, (scene_id, text, _plan) in enumerate(scenes):
        for statement in forbidden:
            if statement in text:
                issues.append(_issue(
                    ContinuityCategory.reveal_policy,
                    Severity.critical if policy_by_statement[statement] == "hint_only" else Severity.error,
                    scene_id,
                    f"Prose states forbidden information verbatim while the plan "
                    f"policy is {policy_by_statement[statement]!r}",
                    evidence=text[:200],
                    suggested="Downgrade to a hint consistent with the policy",
                    index=index,
                ))
    return issues


# --------------------------------------------------------------------- I

def _check_plot_threads(self, context, scenes):
    issues = []
    for index, (scene_id, text, _plan) in enumerate(scenes):
        for thread in context.plot_threads:
            if thread.status not in ("resolved", "abandoned"):
                continue
            if thread.title in text and any(
                kw in text for kw in ("谜团", "未解", "悬案", "之谜", "真相不明")
            ):
                issues.append(_issue(
                    ContinuityCategory.plot_thread_state, Severity.error, scene_id,
                    f"Prose treats resolved thread {thread.title!r} as an unsolved mystery",
                    evidence=text[:200],
                    suggested="Reference the resolution, or introduce a new thread",
                    index=index,
                ))
    return issues


# --------------------------------------------------------------------- J

def _check_proposed_entities(self, context, scenes):
    issues = []
    for index, (scene_id, text, plan) in enumerate(scenes):
        if plan is None:
            continue
        for ref in plan.temporary_entities:
            info = next(
                (e for e in getattr(context, "draft").temporary_entities
                 if e.temporary_ref == ref),
                None,
            )
            if info is None or not info.name_hint:
                continue
            if info.name_hint not in text:
                continue
            has_female = any(term in text for term in _FEMALE_TERMS)
            has_elder = any(term in text for term in _ELDER_TERMS)
            if has_female and has_elder:
                issues.append(_issue(
                    ContinuityCategory.proposed_entity_identity, Severity.error, scene_id,
                    f"Temporary entity {ref!r} is described with contradictory "
                    "identity markers (female vs elder) in the same chapter",
                    evidence=text[:200],
                    suggested="Keep the entity's identity consistent with its first introduction",
                    index=index,
                ))
    return issues


# ----------------------------------------------------------------- T, L, S

def _check_timeline(self, context):
    """Explicit story_time regression between consecutive scenes that both
    declare one (ScenePlan.story_time). Minimal, documented ordering check."""
    issues = []
    ordered = sorted(context.scene_plans, key=lambda s: s.scene_index)
    for previous, current in zip(ordered, ordered[1:]):
        if previous.story_time and current.story_time and current.scene_index > previous.scene_index:
            # both are chapter-local labels like "正午" / "黄昏"
            order = ["清晨", "早上", "上午", "正午", "午后", "下午", "黄昏", "傍晚", "夜晚", "深夜"]
            if previous.story_time in order and current.story_time in order:
                if order.index(current.story_time) < order.index(previous.story_time):
                    issues.append(_issue(
                        ContinuityCategory.timeline, Severity.error, current.scene_id,
                        f"Scene {current.scene_index} story_time {current.story_time!r} "
                        f"is earlier than scene {previous.scene_index} ({previous.story_time!r})",
                        suggested="Fix the scene ordering or the declared times",
                    ))
    return issues


def _check_travel(self, context, scenes):
    issues = []
    ordered = sorted([(sp.scene_index, sp) for sp in context.scene_plans])
    for (prev_index, prev), (curr_index, curr) in zip(ordered, ordered[1:]):
        prev_loc = prev.location.name if prev.location and prev.location.name else None
        curr_loc = curr.location.name if curr.location and curr.location.name else None
        if not prev_loc or not curr_loc or prev_loc == curr_loc:
            continue
        scene = next((s for s in context.draft.scenes if s.scene_id == curr.scene_id), None)
        if scene is None:
            continue
        text = scene.text
        if INSTANT_TRAVEL_PATTERN.search(text) and not any(
            kw in text for kw in TELEPORT_JUSTIFICATION
        ):
            issues.append(_issue(
                ContinuityCategory.location_travel, Severity.error, curr.scene_id,
                f"Scene {curr_index} moves from {prev_loc!r} to {curr_loc!r} "
                "instantly without travel time or a teleportation justification",
                evidence=text[:200],
                suggested="Add travel time or an explicit teleport/flight device",
            ))
    return issues


def _check_scene_continuity(self, context, scenes):
    """Repeated 'first meeting' for known entities and re-acquisition of
    already-acquired items (deterministic subset)."""
    issues = []
    introduced: dict[str, str] = {}
    for scene in sorted(context.scene_plans, key=lambda s: s.scene_index):
        scene_text = next(
            (s.text for s in context.draft.scenes if s.scene_id == scene.scene_id), ""
        )
        for ref in scene.temporary_entities:
            if ref in introduced and re.search(
                r"第一次(见到|见面|相遇|相逢)", scene_text
            ):
                issues.append(_issue(
                    ContinuityCategory.scene_continuity, Severity.error, scene.scene_id,
                    f"Scene treats {ref!r} as a first meeting although the entity "
                    f"was already introduced in scene {introduced[ref]!r}",
                    evidence=scene_text[:200],
                    suggested="Rewrite as a re-encounter",
                ))
            if ref in scene_text and ref not in introduced:
                introduced[ref] = scene.scene_id

    # re-acquisition: "第一次见到 X" where X was already acquired earlier
    acquired: set[str] = set()
    acquisition_verbs = ("取得", "拍得", "获得", "收入囊中")
    for scene in sorted(context.scene_plans, key=lambda s: s.scene_index):
        scene_text = next(
            (s.text for s in context.draft.scenes if s.scene_id == scene.scene_id), ""
        )
        for outcome in scene.required_outcomes:
            for verb in acquisition_verbs:
                if verb in outcome:
                    for item in context.items:
                        if item.name in outcome:
                            acquired.add(item.name)
        for item in context.items:
            if item.name in acquired and item.name in scene_text and "第一次见到" in scene_text:
                issues.append(_issue(
                    ContinuityCategory.scene_continuity, Severity.error, scene.scene_id,
                    f"Prose re-introduces {item.name!r} as a first encounter although "
                    "it was already acquired earlier in this chapter",
                    evidence=scene_text[:200],
                    suggested="Rewrite to a continuation of the earlier acquisition",
                ))
    return issues


# attach methods (kept as module functions for testability)
DeterministicContinuityChecker._check_dead_characters = _check_dead_characters
DeterministicContinuityChecker._check_items = _check_items
DeterministicContinuityChecker._check_abilities = _check_abilities
DeterministicContinuityChecker._check_realm_state = _check_realm_state
DeterministicContinuityChecker._check_breakthroughs = _check_breakthroughs
DeterministicContinuityChecker._check_hard_constraints = _check_hard_constraints
DeterministicContinuityChecker._check_knowledge_leak = _check_knowledge_leak
DeterministicContinuityChecker._check_reveal_policy = _check_reveal_policy
DeterministicContinuityChecker._check_plot_threads = _check_plot_threads
DeterministicContinuityChecker._check_proposed_entities = _check_proposed_entities
DeterministicContinuityChecker._check_timeline = _check_timeline
DeterministicContinuityChecker._check_travel = _check_travel
DeterministicContinuityChecker._check_scene_continuity = _check_scene_continuity
