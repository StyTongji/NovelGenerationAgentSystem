"""Writer-safe repair projection (§24-§28).

Maps checker-level issues (author_explanation may reference secret truth)
to ``ContinuityRepairInstruction`` objects that never carry the truth.
Category-generic instruction templates keep the projection deterministic;
the writer never learns WHY beyond what it is allowed to know.
"""

from __future__ import annotations

from app.continuity.models import ContinuityIssue, ContinuityRepairInstruction
from app.schemas.agent_outputs import ScenePlan

_INSTRUCTIONS: dict[str, str] = {
    "knowledge_leak": (
        "当前 POV 对相关谜团只能保持不确定。删除角色已经确认答案的描写，"
        "改为怀疑、困惑或模糊线索；不要增加新的确定结论。"
    ),
    "reveal_policy": (
        "相关信息的呈现程度超出了本章允许范围。删除确定性的身份/结论描写，"
        "保留行为异常或值得怀疑的表现作为暗示即可。"
    ),
    "hard_constraint": (
        "保留相关角色存活。可以改为重伤、失去行动能力或被救走，"
        "但不要让该角色死亡。"
    ),
    "dead_character": (
        "该角色在当前叙事中不能以正常活人的状态出现。"
        "删除相关描写，或将其明确处理为回忆/幻境/遗物/残魂。"
    ),
    "item_ownership": (
        "删除该角色使用此物品的描写，或改写为其确实拥有的物品。"
    ),
    "ability_ownership": (
        "删除该角色使用此能力的描写，或改写为其已掌握的能力。"
    ),
    "realm_state": (
        "删除与角色当前修为状态矛盾的描写，保持其既有境界。"
    ),
    "illegal_breakthrough": (
        "删除越界的突破描写，保持角色当前境界；本章不应发生该突破。"
    ),
    "plot_thread_state": (
        "该线索已经解决，不要把它写成仍未解开的谜团。"
    ),
    "proposed_entity_identity": (
        "保持该人物的身份、外貌与来历与首次介绍一致，删除矛盾描写。"
    ),
    "timeline": (
        "修正时间连续性：补充合理的时间经过，或调整场景顺序描述。"
    ),
    "location_travel": (
        "修正空间连续性：补充合理的路程/时间，或删除瞬移式移动描写。"
    ),
    "scene_continuity": (
        "修正场景连续性：该事件/相遇已在此前场景发生，改写为延续而非重复首次。"
    ),
    "plan_adherence": (
        "偏离了本章计划的核心目标，请将场景拉回计划要求的走向。"
    ),
    "personality_drift": (
        "角色言行与其既定性格不符，请调整为符合人物性格的表现。"
    ),
    "other": (
        "重写被标记的问题语句，使之符合本场景计划、人物已知信息与相邻场景；"
        "不要推断或还原被隐藏的信息，保持其余内容不变。"
    ),
}

GENERIC = _INSTRUCTIONS["other"]


def project_issue_to_writer_safe(
    issue: ContinuityIssue, scene_plan: ScenePlan | None
) -> ContinuityRepairInstruction:
    preserve: list[str] = []
    if scene_plan is not None:
        preserve = list(scene_plan.required_outcomes) + list(scene_plan.required_actions)
    return ContinuityRepairInstruction(
        issue_id=issue.issue_id,
        scene_id=issue.scene_id or "",
        category=issue.category.value,
        instruction=_INSTRUCTIONS.get(issue.category.value, GENERIC),
        preserve=preserve,
        allowed_information=[],
    )
