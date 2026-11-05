"""Shared fixtures for Phase 5 draft pipeline tests."""

from __future__ import annotations

from tests.helpers import create_novel, create_rich_world


def build_valid_plan_dict(world) -> dict:
    """Acceptance plan (auction + jade pendant + mysterious woman)."""
    lin_fan = world["characters"]["林凡"]
    su_qing = world["characters"]["苏青"]
    father_id = str(world["result"].plot_thread_ids["父亲失踪之谜"])
    return {
        "chapter_number": 5,
        "chapter_goal": "通过拍卖会引入玉佩与神秘女子，推进轮回古镜/父亲失踪线",
        "participating_characters": [
            {"character_id": str(lin_fan.id), "name": "林凡"},
            {"character_id": str(su_qing.id), "name": "苏青"},
            {"temporary_ref": "new_character_1", "name": "神秘女子"},
        ],
        "primary_locations": [{"name": "天剑城"}],
        "proposed_entities": [
            {
                "entity_type": "character",
                "temporary_ref": "new_character_1",
                "name_hint": "神秘女子",
                "role": "来自中州的神秘人物，对玉佩有异常兴趣",
            }
        ],
        "required_events": [
            "林凡在拍卖会遇到神秘女子",
            "苏青为保护林凡挡下攻击并受伤",
            "林凡获得玉佩",
        ],
        "scenes": [
            {
                "index": 1,
                "goal": "林凡到达天剑城拍卖会，神秘女子初现",
                "participants": ["林凡", "神秘女子"],
                "location": "天剑城",
                "beats": ["林凡入场", "神秘女子坐在角落"],
            },
            {
                "index": 2,
                "goal": "玉佩登场引发争夺，苏青为保护林凡挡下攻击受伤",
                "participants": ["林凡", "苏青", "神秘女子"],
                "location": "天剑城",
                "beats": ["玉佩作为拍品展示", "散修抢夺，苏青挡下攻击受伤", "林凡决定出手"],
            },
        ],
        "plot_threads_to_progress": [
            {"plot_thread_id": father_id, "action": "advance", "instruction": "玉佩带来父亲失踪线新线索"}
        ],
        "revelations": [
            {
                "policy": "hint_only",
                "allowed_information": "玉佩与父亲过去的经历可能存在联系",
                "forbidden_information": "父亲仍然活着并进入了轮回秘境",
            }
        ],
        "prohibited_revelations": [{"statement": "父亲真实去向"}],
        "must_not_reveal": ["父亲真实去向"],
        "expected_state_changes": ["林凡获得玉佩"],
        "ending_hook": "玉佩与轮回古镜短暂共鸣",
        "conflicts": [],
        "director_notes": {
            "strategic_rationale": "为下卷引出中州势力",
            "secret_context": ["父亲仍然活着"],
        },
    }


def build_scene_batch_dict(world, *, pov_ids: list[str] | None = None) -> dict:
    """A valid ScenePlanBatch matching build_valid_plan_dict beats."""
    lin_fan_id = str(world["characters"]["林凡"].id)
    if pov_ids is None:
        pov_ids = [lin_fan_id, lin_fan_id]
    scenes = []
    for index, pov_id in enumerate(pov_ids, start=1):
        scenes.append(
            {
                "scene_id": f"scene_{index}",
                "scene_index": index,
                "source_beat_ids": [str(index)],
                "pov": {"character_id": pov_id, "name": "林凡" if pov_id == lin_fan_id else "魔教长老"},
                "location": {"name": "天剑城"},
                "participants": [
                    {"character_id": lin_fan_id, "name": "林凡"},
                    {"temporary_ref": "new_character_1", "name": "神秘女子"},
                ],
                "scene_goal": f"场景{index}目标",
                "conflict": "试探与戒备",
                "opening_state": "拍卖会进行中" if index == 1 else "竞拍进入尾声",
                "required_actions": ["观察", "出价"],
                "required_outcomes": [f"场景{index}完成既定结果"],
                "allowed_information": ["玉佩与父亲过去的经历可能存在联系"],
                "allowed_hints": ["玉佩纹路似曾相识"],
                "emotional_target": "克制的好奇",
                "pacing": "balanced",
                "transition_in": "承接入场" if index == 1 else "承接上一场景竞价",
                "transition_out": "转向下一件拍品",
                "target_chars": 800,
                "temporary_entities": ["new_character_1"],
            }
        )
    return {"chapter_number": 5, "scenes": scenes, "planner_notes": ["中州线开场"]}


async def make_world_and_plan(session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    plan_dict = build_valid_plan_dict(world)
    from app.schemas.agent_outputs import ChapterPlan

    plan = ChapterPlan.model_validate(plan_dict)
    return novel, world, plan
