"""ChapterPlanValidator tests: illegal plan cases 1-10 + reveal policy."""

from __future__ import annotations

import uuid

from app.context.builder import ContextBuilder
from app.planning.validator import ChapterPlanValidator
from app.schemas.agent_outputs import ChapterPlan
from tests.helpers import (
    SECRET_STATEMENT,
    create_character_with_state,
    create_novel,
    create_rich_world,
)


async def _context(session, novel, direction=None, chapter_number=5):
    builder = ContextBuilder(session)
    return await builder.build_director_context(
        novel.id, chapter_number=chapter_number, user_direction=direction
    )


def _base_plan(**overrides) -> dict:
    plan = {
        "chapter_goal": "推进剧情",
        "scenes": [
            {
                "index": 1,
                "goal": "场景目标",
                "participants": ["林凡"],
                "location": "天剑城",
                "beats": ["林凡抵达"],
            }
        ],
        "ending_hook": "留下悬念",
        "conflicts": [],
    }
    plan.update(overrides)
    return plan


async def _validate(session, novel, plan_dict):
    context = await _context(session, novel)
    validator = ChapterPlanValidator(session)
    return await validator.validate(ChapterPlan.model_validate(plan_dict), context)


# --------------------------------------------------------------- acceptance #30


async def test_acceptance_scenario_auction_plan_is_valid(session):
    """完整验收：拍卖会 + 玉佩 + hint-only + 新角色 + 不揭露父亲去向。"""
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    lin_fan = world["characters"]["林凡"]
    threads = await world["service"].threads.list_threads(novel.id)
    father = next(t for t in threads if t.title == "父亲失踪之谜")

    context = await _context(
        session, novel,
        direction="下一章让林凡去天剑城拍卖会，发现一枚和轮回古镜有关的玉佩，"
                  "可以推进父亲失踪伏笔，但不要揭露父亲真正去了哪里，"
                  "也不要让林凡这一章突破。加入一个来自中州的神秘女子。",
    )
    plan = ChapterPlan.model_validate(
        _base_plan(
            chapter_goal="通过拍卖会引入玉佩与神秘女子，推进轮回古镜/父亲失踪线",
            participating_characters=[
                {"character_id": str(lin_fan.id), "name": "林凡"},
                {"temporary_ref": "new_character_1", "name": "神秘女子"},
            ],
            primary_locations=[{"name": "天剑城"}],
            proposed_entities=[
                {
                    "entity_type": "character",
                    "temporary_ref": "new_character_1",
                    "name_hint": "神秘女子",
                    "role": "来自中州的神秘人物，对玉佩表现出异常兴趣",
                }
            ],
            scenes=[
                {"index": 1, "goal": "林凡到达拍卖会", "participants": ["林凡"], "location": "天剑城", "beats": ["入场"]},
                {"index": 2, "goal": "玉佩出现", "participants": ["林凡"], "location": "天剑城", "beats": ["拍品展示玉佩"]},
                {"index": 3, "goal": "发现纹路相似", "participants": ["林凡"], "location": "天剑城", "beats": ["玉佩纹路与轮回古镜相似"]},
                {"index": 4, "goal": "双方试探", "participants": ["林凡", "神秘女子"], "location": "天剑城", "beats": ["竞价试探但不交恶"]},
                {"index": 5, "goal": "模糊线索", "participants": ["林凡"], "location": "天剑城", "beats": ["玉佩提供一条模糊线索"]},
            ],
            plot_threads_to_progress=[
                {"plot_thread_id": str(father.id), "action": "advance", "instruction": "玉佩为父亲失踪线增加新线索"}
            ],
            revelations=[
                {
                    "policy": "hint_only",
                    "allowed_information": "玉佩与父亲旧物存在关联的迹象",
                    "forbidden_information": "父亲真实去向；父亲是否存活",
                }
            ],
            prohibited_revelations=[
                {"statement": "父亲真实去向", "reason": "hard constraint 到150章"},
                {"statement": "父亲仍然活着的确定结论"},
            ],
            character_state_expectations=[],
            must_not_reveal=["父亲真实去向"],
            ending_hook="玉佩与轮回古镜产生短暂共鸣",
        )
    )
    validator = ChapterPlanValidator(session)
    report = await validator.validate(plan, context)
    assert report.valid, f"验收场景应有效: {[(i.code, i.message) for i in report.errors()]}"


# ------------------------------------------------------------ illegal cases


async def test_case1_dead_character_normal_appearance_invalid(session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    dead = await create_character_with_state(
        session, novel.id, "陈铁山", alive_status=__import__("app.models.enums", fromlist=["AliveStatus"]).AliveStatus.dead
    )

    report = await _validate(
        session, novel,
        _base_plan(scenes=[{
            "index": 1, "goal": "重逢", "participants": ["陈铁山"], "location": "天剑城", "beats": ["他走上前来"],
        }]),
    )
    assert not report.valid
    assert any(i.code == "dead_character_active" and i.severity.value == "critical" for i in report.issues)

    # flashback narrative mode is legal
    ok = await _validate(
        session, novel,
        _base_plan(scenes=[{
            "index": 1, "goal": "回忆", "participants": ["陈铁山"], "location": "天剑城",
            "beats": ["林凡回忆故人"], "narrative_mode": "flashback",
        }]),
    )
    assert not any(i.code == "dead_character_active" for i in ok.issues)


async def test_case2_item_not_owned_invalid(session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    from app.models import Item

    await world["service"].items.add(
        Item(novel_id=novel.id, name="诛仙剑", type="artifact", grade="仙品",
             current_owner_character_id=world["characters"]["玄清真人"].id)
    )
    report = await _validate(
        session, novel,
        _base_plan(required_events=["林凡在拍卖会使用诛仙剑斩杀敌人"]),
    )
    assert not report.valid
    assert any(i.code == "item_ownership_violation" for i in report.issues)


async def test_case3_illegal_realm_jump_invalid(session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    lin_fan = world["characters"]["林凡"]
    report = await _validate(
        session, novel,
        _base_plan(
            character_state_expectations=[
                {"character_id": str(lin_fan.id), "expectation": "林凡在结尾直接突破元婴"}
            ],
        ),
    )
    assert not report.valid
    assert any(i.code == "realm_jump_violation" for i in report.issues)


async def test_case4_hard_constraint_death_invalid(session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    report = await _validate(
        session, novel,
        _base_plan(required_events=["苏青为掩护林凡战死沙场"]),
    )
    assert not report.valid
    assert any(i.code == "hard_constraint_violation" and i.severity.value == "critical" for i in report.issues)


async def test_case5_acting_on_unknown_secret_invalid(session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    report = await _validate(
        session, novel,
        _base_plan(required_events=[f"林凡根据『{SECRET_STATEMENT}』提前设下埋伏"]),
    )
    assert not report.valid
    assert any(i.code == "knowledge_violation" for i in report.issues)


async def test_case5b_acting_on_suspicion_allowed(session):
    """苏青 suspected -> 基于怀疑的调查是合法的。"""
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    report = await _validate(
        session, novel,
        _base_plan(required_events=["苏青因为玄清真人最近行为可疑而暗中观察"]),
    )
    assert not any(i.code == "knowledge_violation" for i in report.issues)


async def test_case6_hint_only_secret_full_reveal_invalid(session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    secret_fact = world["secret_fact"]
    # add a protecting hard constraint that overlaps the secret statement
    from app.models import Constraint

    await world["service"].constraints.add(
        Constraint(novel_id=novel.id, type="hard",
                   statement="第150章前不得揭露玄清真人的真实身份", start_chapter=1)
    )
    context = await _context(session, novel)
    plan = ChapterPlan.model_validate(
        _base_plan(revelations=[{
            "fact_id": str(secret_fact.id), "policy": "full",
            "allowed_information": "全部真相",
        }])
    )
    validator = ChapterPlanValidator(session)
    report = await validator.validate(plan, context)
    assert not report.valid
    assert any(i.code == "reveal_policy_violation" for i in report.issues)

    # hint_only is fine
    plan2 = ChapterPlan.model_validate(
        _base_plan(revelations=[{
            "fact_id": str(secret_fact.id), "policy": "hint_only",
            "allowed_information": "玄清真人近期行为有异常",
            "forbidden_information": SECRET_STATEMENT,
        }])
    )
    report2 = await validator.validate(plan2, context)
    assert not any(i.code == "reveal_policy_violation" for i in report2.issues)


async def test_case7_hallucinated_ids_invalid(session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    bogus = str(uuid.uuid4())
    report = await _validate(
        session, novel,
        _base_plan(
            participating_characters=[{"character_id": bogus, "name": "不存在的角色"}],
            plot_threads_to_progress=[
                {"plot_thread_id": str(uuid.uuid4()), "action": "advance", "instruction": "推进"}
            ],
        ),
    )
    assert not report.valid
    codes = {i.code for i in report.issues}
    assert "unknown_character_id" in codes
    assert "unknown_plot_thread_id" in codes


async def test_case8_proposed_entity_with_temporary_ref_valid(session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    report = await _validate(
        session, novel,
        _base_plan(
            participating_characters=[{"temporary_ref": "new_character_1", "name": "神秘女子"}],
            proposed_entities=[{
                "entity_type": "character", "temporary_ref": "new_character_1",
                "name_hint": "神秘女子", "role": "来自中州",
            }],
            scenes=[{
                "index": 1, "goal": "相遇", "participants": ["林凡", "神秘女子"],
                "location": "天剑城", "beats": ["擦肩而过"],
            }],
        ),
    )
    assert report.valid, f"{[(i.code, i.message) for i in report.errors()]}"


async def test_case9_overdue_debt_ignored_is_warning(session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    context = await _context(session, novel, chapter_number=6)  # 比武之约 due ch3 -> overdue
    plan = ChapterPlan.model_validate(_base_plan())  # no debt progress
    validator = ChapterPlanValidator(session)
    report = await validator.validate(plan, context)
    assert report.valid, "warning 不应阻断"
    assert any(i.code == "overdue_plot_debt_ignored" and i.severity.value == "warning" for i in report.issues)


async def test_case10_resolved_thread_reopened_invalid(session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    threads = await world["service"].threads.list_threads(novel.id)
    father = threads[0]
    father.status = "resolved"
    await session.flush()

    report = await _validate(
        session, novel,
        _base_plan(plot_threads_to_progress=[
            {"plot_thread_id": str(father.id), "action": "advance", "instruction": "当作未解之谜继续"}
        ]),
    )
    assert not report.valid
    assert any(i.code == "resolved_thread_reopened" for i in report.issues)

    # premature resolution of a far-future thread
    father.status = "active"
    father.target_resolution_chapter = 150
    await session.flush()
    report2 = await _validate(
        session, novel,
        _base_plan(plot_threads_to_progress=[
            {"plot_thread_id": str(father.id), "action": "resolve", "instruction": "本章揭晓全部真相"}
        ]),
    )
    assert not report2.valid
    assert any(i.code == "premature_thread_resolution" for i in report2.issues)


async def test_location_existence_checked(session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    report = await _validate(
        session, novel,
        _base_plan(scenes=[{
            "index": 1, "goal": "抵达", "participants": ["林凡"], "location": "不存在的仙宫",
            "beats": ["x"],
        }]),
    )
    assert not report.valid
    assert any(i.code == "unknown_location" for i in report.issues)


async def test_prohibited_statement_in_plan_text_invalid(session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    report = await _validate(
        session, novel,
        _base_plan(
            must_not_reveal=["父亲真实去向"],
            scenes=[{
                "index": 1, "goal": "真相时刻", "participants": ["林凡"], "location": "天剑城",
                "beats": ["林凡得知父亲真实去向"],
            }],
        ),
    )
    assert not report.valid
    assert any(i.code == "prohibited_reveal_in_plan" for i in report.issues)


async def test_no_progression_warning(session):
    novel = await create_novel(session)
    world = await create_rich_world(session, novel.id)
    report = await _validate(session, novel, _base_plan())
    assert report.valid
    assert any(i.code == "no_progression" for i in report.issues)
