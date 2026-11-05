"""Shared world-building helpers used across test modules."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Chapter,
    Character,
    CharacterState,
    Novel,
)
from app.models.enums import AliveStatus
from app.schemas.story import BootstrapWorldRequest
from app.services.story_state import StoryStateService


async def create_novel(session: AsyncSession, title: str = "九霄问道") -> Novel:
    service = StoryStateService(session)
    return await service.create_novel(
        title=title, genre="玄幻", premise="少年追查父亲失踪之谜"
    )


async def create_world(session: AsyncSession, novel_id: uuid.UUID) -> dict:
    """Bootstrap the standard test world (mirrors examples/bootstrap_world.json).

    Returns a dict of name -> entity for assertions.
    """
    service = StoryStateService(session)
    data = BootstrapWorldRequest.model_validate(
        {
            "power_system": {
                "name": "青玄修炼体系",
                "rules": [
                    "常态下跨两个大境界正面击杀几乎不可能",
                    "禁术可短时提升战力，但必须支付已设定代价",
                ],
                "realms": [
                    {"name": "炼体", "rank": 1},
                    {"name": "练气", "rank": 2},
                    {"name": "筑基", "rank": 3, "sublevels": ["初期", "中期", "后期"]},
                    {"name": "金丹", "rank": 4},
                    {"name": "元婴", "rank": 5},
                ],
            },
            "characters": [
                {
                    "name": "林凡",
                    "is_protagonist": True,
                    "personality_traits": ["谨慎", "记仇", "表面低调"],
                    "realm_name": "筑基",
                    "subrealm": "后期",
                },
                {
                    "name": "苏青",
                    "personality_traits": ["冷静", "重承诺"],
                    "realm_name": "筑基",
                    "subrealm": "中期",
                },
                {
                    "name": "玄清真人",
                    "personality_traits": ["温和", "深藏不露"],
                    "realm_name": "金丹",
                    "subrealm": "后期",
                },
            ],
            "locations": [
                {"name": "青玄大陆"},
                {"name": "天剑城", "parent_name": "青玄大陆"},
                {"name": "黑风谷", "parent_name": "青玄大陆"},
            ],
            "initial_facts": [
                {
                    "statement": "玄清真人是魔教卧底",
                    "secrecy": "top_secret",
                    "known_by": ["玄清真人"],
                    "suspected_by": ["苏青"],
                }
            ],
            "plot_threads": [
                {
                    "title": "父亲失踪之谜",
                    "status": "active",
                    "truth": "林父主动进入轮回秘境寻找某人",
                    "public_summary": "林父数年前神秘失踪",
                    "priority": 0.9,
                }
            ],
            "arc": {
                "title": "青玄试炼",
                "start_chapter": 1,
                "planned_end_chapter": 40,
                "goal": "林凡在天剑城站稳脚跟并追查父亲线索",
            },
            "constraints": [
                {"type": "hard", "statement": "父亲真实去向在150章前不得完全揭露"},
                {"type": "hard", "statement": "苏青不能死亡"},
            ],
        }
    )
    result = await service.bootstrap_world(novel_id, data)
    return {
        "service": service,
        "result": result,
        "characters": {
            name: await service.characters.get(cid)
            for name, cid in result.character_ids.items()
        },
    }


async def create_character_with_state(
    session: AsyncSession,
    novel_id: uuid.UUID,
    name: str,
    *,
    chapter_number: int = 0,
    alive_status: AliveStatus = AliveStatus.alive,
) -> Character:
    character = Character(novel_id=novel_id, name=name)
    session.add(character)
    await session.flush()
    state = CharacterState(
        character_id=character.id,
        chapter_number=chapter_number,
        alive_status=alive_status,
    )
    session.add(state)
    await session.flush()
    return character


SECRET_STATEMENT = "玄清真人是魔教卧底"


async def create_rich_world(session: AsyncSession, novel_id: uuid.UUID) -> dict:
    """Standard world + events/chapters/summaries/foreshadowing/debts/items
    used by Phase 3 retrieval and knowledge-isolation tests."""
    base = await create_world(session, novel_id)
    service = base["service"]
    characters = base["characters"]
    lin_fan = characters["林凡"]
    su_qing = characters["苏青"]
    xuan_qing = characters["玄清真人"]

    # a cult elder who knows the secret
    elder = await create_character_with_state(session, novel_id, "魔教长老")
    facts = await service.facts.list_for_novel(novel_id)
    secret_fact = next(f for f in facts if f.statement == SECRET_STATEMENT)
    await service.grant_knowledge(elder.id, secret_fact.id)

    # the mirror item owned by 林凡
    from app.models import Item

    mirror = await service.items.add(
        Item(
            novel_id=novel_id,
            name="轮回古镜",
            aliases=["古镜"],
            type="artifact",
            grade="仙品",
            current_owner_character_id=lin_fan.id,
        )
    )

    # 林凡 moved to 黑风谷 by chapter 4
    await service.update_character_state(
        lin_fan.id, chapter_number=4, location_name="黑风谷",
        physical_condition="轻伤",
    )

    # relationships (directed, with hidden note that must never leak)
    from app.models import Relationship

    await service.characters.add_relationship(
        Relationship(
            novel_id=novel_id,
            source_character_id=su_qing.id,
            target_character_id=lin_fan.id,
            type="挚友",
            trust=80,
            affection=60,
            public_description="苏青与林凡曾在黑风谷并肩作战，互相信任",
            hidden_description=f"苏青受魔教指派接近林凡，{SECRET_STATEMENT}",
        )
    )
    await service.characters.add_relationship(
        Relationship(
            novel_id=novel_id,
            source_character_id=lin_fan.id,
            target_character_id=su_qing.id,
            type="挚友",
            trust=72,
            affection=55,
            public_description="林凡视苏青为少数可信之人",
        )
    )

    # events (ch1..ch3), one leaking the secret in its summary
    loc_tianjian = await service.locations.find_by_name(novel_id, "天剑城")
    loc_valley = await service.locations.find_by_name(novel_id, "黑风谷")
    ev_auction = await service.record_event(
        novel_id, chapter_number=1, event_type="auction",
        title="天剑城拍卖会", summary="林凡在天剑城参加拍卖会，见到玉佩残片。",
        participant_ids=[lin_fan.id], location_id=loc_tianjian.id,
        importance=0.6, happened_at_story_time="历532年春",
    )
    ev_rescue = await service.record_event(
        novel_id, chapter_number=2, event_type="combat",
        title="黑风谷遇袭", summary="苏青在黑风谷替林凡挡剑，两人负伤突围。",
        participant_ids=[lin_fan.id, su_qing.id], location_id=loc_valley.id,
        importance=0.9, happened_at_story_time="历532年春末",
    )
    ev_secret = await service.record_event(
        novel_id, chapter_number=3, event_type="conversation",
        title="密会", summary=f"魔教长老与玄清真人密会，确认{SECRET_STATEMENT}。",
        participant_ids=[xuan_qing.id, elder.id],
        importance=0.95,
        effects=[],
        extra_metadata={"secret_fact_ids": [str(secret_fact.id)]},
    )

    # chapters with bodies (long enough to exercise tail trimming)
    for number in (1, 2, 3):
        session.add(
            Chapter(
                novel_id=novel_id,
                number=number,
                version=1,
                status="final",
                title=f"第{number}章",
                body=("正文内容。" * 50) + f"第{number}章结尾：林凡望向远方。",
                user_direction=None,
            )
        )
    await session.flush()

    # author-level chapter summaries
    from app.models import ChapterSummary
    from app.repositories.chapter import ChapterRepository

    chapter_repo = ChapterRepository(session)
    for number, short in [
        (1, "林凡在天剑城参加拍卖会，见到玉佩残片。"),
        (2, f"黑风谷遇袭，苏青替林凡挡剑。幕后与{SECRET_STATEMENT}有关。"),
        (3, "魔教长老与玄清真人密会。"),
    ]:
        chapter = await chapter_repo.latest_for_number(novel_id, number, final_only=False)
        session.add(
            ChapterSummary(
                novel_id=novel_id,
                chapter_id=chapter.id,
                chapter_number=number,
                short_summary=short,
            )
        )
    await session.flush()

    # foreshadowing: one public, one hidden
    threads = await service.threads.list_threads(novel_id)
    father_thread = next(t for t in threads if t.title == "父亲失踪之谜")
    father_thread.introduced_chapter = 1
    from app.models import Foreshadowing

    await service.foreshadows.add(
        Foreshadowing(
            novel_id=novel_id,
            plot_thread_id=father_thread.id,
            setup="林凡发现父亲留下的半块玉佩",
            intended_payoff="揭示父亲进入轮回秘境的真正原因",
            status="planted",
            planted_chapter=1,
            visibility="public",
        )
    )
    await service.foreshadows.add(
        Foreshadowing(
            novel_id=novel_id,
            plot_thread_id=father_thread.id,
            setup=f"古镜真正的来历与{SECRET_STATEMENT}有关",
            intended_payoff="古镜来历揭晓",
            status="planted",
            planted_chapter=3,
            visibility="hidden",
        )
    )

    # plot debts: overdue / due_soon / normal / fulfilled
    from app.models import PlotDebt

    overdue = await service.debts.add(
        PlotDebt(novel_id=novel_id, debt_type="promise", description="与陈海的比武之约",
                 created_chapter=1, due_chapter=3)
    )
    due_soon = await service.debts.add(
        PlotDebt(novel_id=novel_id, debt_type="event", description="七日后宗门大会",
                 created_chapter=4, due_chapter=7)
    )
    normal = await service.debts.add(
        PlotDebt(novel_id=novel_id, debt_type="promise", description="欠药铺掌柜一个人情",
                 created_chapter=2, due_chapter=None)
    )
    fulfilled = await service.debts.add(
        PlotDebt(novel_id=novel_id, debt_type="promise", description="归还借来的灵石",
                 created_chapter=1, due_chapter=5, status="fulfilled", resolved_chapter=4)
    )

    return {
        **base,
        "elder": elder,
        "mirror": mirror,
        "events": {"auction": ev_auction, "rescue": ev_rescue, "secret": ev_secret},
        "secret_fact": secret_fact,
        "debts": {"overdue": overdue, "due_soon": due_soon, "normal": normal, "fulfilled": fulfilled},
    }
