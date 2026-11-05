"""Offline scripted demonstration through the actual chapter domain services.

This provider is deliberately labelled demo. It proves orchestration, evidence,
review and canonical inheritance, not the quality of a remote language model.
The scripted ten-chapter arc stops rather than pretending to be an unlimited
novel generator. Real providers can continue any approved story independently.
"""

from __future__ import annotations

import re
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.continuity_checker import LLMContinuityChecker
from app.agents.memory_extractor import MemoryExtractorAgent
from app.agents.scene_planner import ScenePlannerAgent
from app.agents.writer import WriterAgent
from app.agents.writer_repair import WriterRepairAgent
from app.llm.client import AgentLLMClient
from app.llm.fake import FakeLLMProvider
from app.llm.prompts import get_default_registry
from app.models import Character, CharacterKnowledge, CharacterState, Location, Novel, PlotThread, StoryFact
from app.planning.scene_validator import ScenePlanValidator
from app.services.canonical_commit import CanonicalCommitService
from app.services.chapter_draft import ChapterDraftService
from app.services.chapter_generation import ChapterGenerationService
from app.services.chapter_planning import ChapterPlanningService, build_director_agent
from app.services.continuity import ContinuityService
from app.services.memory_extraction import MemoryExtractionService


class StudioDemoProvider(FakeLLMProvider):
    """Scripted role outputs with evidence IDs selected from the actual prompt."""

    name = "studio-demo"
    default_model = "scripted-demo-not-a-real-llm"

    def __init__(self, *, plan: dict, scenes: dict, prose: list[str], memory: dict):
        super().__init__(simulate_latency_ms=80)
        self.plan, self.scenes, self.prose, self.memory = plan, scenes, prose, memory

    async def _complete(self, messages, **kwargs):
        role = kwargs.get("agent")
        if role == "director":
            self.enqueue_structured(self.plan)
        elif role == "scene_planner":
            self.enqueue_structured(self.scenes)
        elif role == "writer":
            prompt = "\n".join(message.content for message in messages)
            scene = re.search(r"^- scene (\d+) goal:", prompt, re.MULTILINE)
            if scene is None:
                raise ValueError("Demo Writer requires an explicit scene index")
            self.enqueue_text(self.prose[int(scene.group(1)) - 1])
        elif role == "continuity_checker":
            self.enqueue_structured({"issues": []})
        elif role == "memory_extractor":
            # Refs are derived from the server-built catalog in the production
            # prompt, so any changed draft must provide its own exact evidence.
            prompt = "\n".join(message.content for message in messages)
            entries = re.findall(r"^\[(ev_\d+)\] (.+)$", prompt, re.MULTILINE)
            operations = []
            for item in self.memory["operations"]:
                operation = dict(item)
                quote = operation.pop("_quote")
                ref = next((evidence_id for evidence_id, text in entries if quote in text), None)
                if ref is None:
                    raise ValueError("Demo draft no longer contains its scripted evidence")
                operation["evidence_refs"] = [{"evidence_id": ref}]
                operations.append(operation)
            self.enqueue_structured({**self.memory, "operations": operations})
        else:
            raise ValueError(f"The offline demonstration has no scripted response for {role!r}")
        return await super()._complete(messages, **kwargs)


async def build_demo_services(
    session: AsyncSession, novel_id: uuid.UUID, chapter_number: int, target_chars: int = 1800,
) -> ChapterGenerationService:
    """Build one demo chapter using the current novel's committed characters/state."""
    if not 1 <= chapter_number <= 10:
        raise ValueError("离线演示包含十个连续章节；更多章节请选择真实模型。")
    novel = await session.get(Novel, novel_id)
    if novel is None:
        raise ValueError("Novel not found")
    if chapter_number != novel.current_chapter_number + 1:
        raise ValueError("请先审阅并提交上一章，再生成下一章。")
    characters = list((await session.scalars(select(Character).where(
        Character.novel_id == novel_id).order_by(Character.is_protagonist.desc(), Character.created_at))).all())
    if not characters or not characters[0].is_protagonist:
        raise ValueError("请先设置本书主角。")
    hero = characters[0]
    companion = characters[1] if len(characters) > 1 else None
    location = (await session.scalars(select(Location).where(Location.novel_id == novel_id))).first()
    thread = (await session.scalars(select(PlotThread).where(PlotThread.novel_id == novel_id))).first()
    if location is None or thread is None:
        raise ValueError("离线演示需要已初始化的地点和主线。")
    old_state = (await session.scalars(select(CharacterState).where(
        CharacterState.character_id == hero.id).order_by(CharacterState.chapter_number.desc()))).first()
    previous_fact = (await session.scalars(select(StoryFact).join(
        CharacterKnowledge, CharacterKnowledge.fact_id == StoryFact.id).where(
        StoryFact.novel_id == novel_id, CharacterKnowledge.character_id == hero.id,
        StoryFact.category == "demo_discovery").order_by(StoryFact.introduced_chapter.desc()))).first()
    name = hero.name
    other = companion.name if companion else name
    discoveries = [
        "旧记录中的第三页被人换成了一张日期相同的空白页",
        "保管登记的编号与旧记录缺页处留下的编号一致",
        "被替换的原页保存在封存袋中，原页上有两次不同的签名",
        "第二次签名的时间晚于原页封存登记的时间",
        "交接表中有一份从未送达接收人的留底副本",
        "留底副本注明原件曾在同一天被送往另一处核验",
        "外部核验回执上的页数比现存原件多一页",
        "缺失的附页被装订在另一份日期相邻的卷宗末尾",
        "附页记录了一次经过双方签字确认的补充交接",
        "补充交接中的条目逐一对应此前缺失的登记，整条交接链已经核对完整",
    ]
    fact = discoveries[chapter_number - 1]
    feeling = ["警觉", "坚定", "冷静而有决心", "耐心", "专注", "沉着", "紧张但克制", "释然", "审慎而清醒", "平静"][chapter_number - 1]
    goals = ["发现被替换的记录", "通过编号交叉核对", "保存原始证据并作出选择",
             "厘清签名与封存的时间", "寻找遗漏的留底副本", "沿交接记录寻找核验去向",
             "查清核验回执的页数差异", "找到另卷中的缺失附页", "核验补充交接的双方签字",
             "闭合登记与交接的证据链"]
    endings = ["封套背后，还有一个没有登记的编号。", "那只封存袋的封口，从里面被划开过。",
               "门外的人轻敲了三下，说自己知道第二次签名是谁写的。", "签名旁边，一枚模糊的印章露出半个日期。",
               "留底副本最下方，写着一个从未出现过的接收地点。", "核验回执背面，多了一道被划掉的页码。",
               "相邻卷宗的装订线，比其他卷宗新了一截。", "附页最后一行，没有填写交接原因。",
               "两份签字之间，终于只剩下一个可以当面问清的问题。", "这一次，记录上的空格填完了；窗外的天也亮了。"]
    lead = (
        f"{name}把工作台清出一角，摊开旧记录。故事中的困境还没有答案，眼前这页纸却是可以亲手核对的东西。"
        if previous_fact is None else
        f"上一次留下的判断仍写在{name}的笔记上：{previous_fact.statement}。"
        f"{name}没有把已经确认的发现重新当成谜题，而是沿着它继续核查。"
        f"此前的心境是{old_state.mental_condition if old_state else '谨慎'}，这次至少知道该从哪里下手。"
    )
    exchange = (
        f"“你想证明什么？”{other}问。\n“先证明哪里不一样，再问是谁做的。”{name}把两张纸并排放好。"
        if companion else f"{name}在便签上写下问题：先证明哪里不一样，再问是谁做的。"
    )
    prose = [
        lead + "\n\n" + exchange +
        "\n\n桌沿压着一条细细的纸屑。窗外的声音时远时近，纸上的空格却把注意力一点点拉回来。"
        "凭直觉给人定罪很容易，把一次改动发生的先后说清楚却难得多。"
        f"{name}先核对日期，再把封面和内页的顺序逐一记下。墨迹的深浅不能直接说明年代，"
        "所以每个猜测旁边都留着空格。那些空格不是失败，只是提醒自己还缺少什么。"
        f"\n\n{name}逐项核验后确认：{fact}。"
        "记录中的差异终于有了边界，至于差异背后的人，此刻仍然不能断言。",
        f"{name}将已经核实的部分另抄了一份，原件留在桌上，没有带离现场。"
        + (f"{other}看完核对过程，同意把结论与猜测分开记录。" if companion else "空白便签被分成两列，一列写结论，另一列写尚待核查的问题。")
        + f"\n\n{name}的心情变得{feeling}。这份变化没有让难题消失，却让下一步有了顺序。"
        "如果现在只追求一个响亮的答案，最先丢掉的恰好会是答案凭什么成立。"
        "每一处签名、每一次交接，都得有能重新找到的原始位置。"
        "\n\n天色渐暗，工作台旁的影子挪过了半张纸。核对表被折好，待查的问题仍留在最上面。"
        f"{name}在记录末尾写下今日的进展，决定先保存证据，再寻找下一位能说明情况的人。"
        f"\n\n{endings[chapter_number - 1]}",
    ]
    participants = [{"character_id": str(person.id), "name": person.name}
                    for person in [hero, *([companion] if companion else [])]]
    plan = {
        "chapter_number": chapter_number, "chapter_goal": goals[chapter_number - 1],
        "chapter_role": "exploration", "participating_characters": participants,
        "primary_locations": [{"name": location.name}], "required_events": [fact],
        "scenes": [{"index": index, "goal": goal, "participants": [p["name"] for p in participants],
                    "location": location.name, "beats": [goal]}
                   for index, goal in enumerate([goals[chapter_number - 1], "保存证据并决定下一步"], 1)],
        "plot_threads_to_progress": [{"plot_thread_id": str(thread.id), "action": "advance",
                                      "instruction": goals[chapter_number - 1]}],
        "ending_hook": endings[chapter_number - 1],
        "expected_state_changes": [f"{name}的心情变得{feeling}"],
        "soft_preferences": ["离线演示脚本，仅展示多角色协作、审阅和记忆继承"],
    }
    scenes = {"chapter_number": chapter_number, "scenes": [
        {"scene_id": f"ch{chapter_number}_scene_{index}", "scene_index": index,
         "source_beat_ids": [str(index)], "pov": participants[0],
         "location": {"name": location.name}, "participants": participants,
         "scene_goal": plan["scenes"][index - 1]["goal"], "required_outcomes": [],
         "target_chars": max(100, min(1800, target_chars // 2))}
        for index in (1, 2)
    ]}
    def operation(kind, payload, quote, scene=1):
        return {"operation_id": f"ch{chapter_number}-{kind}", "update_type": kind,
                "source_scene_id": f"ch{chapter_number}_scene_{scene}", "_quote": quote, **payload}
    memory = {
        "contract_version": 3,
        "operations": [
            operation("create_event", {"event": {
                "event_type": "discovery", "title": goals[chapter_number - 1],
                "author_summary": f"{name}通过核对确认：{fact}",
                "reader_safe_summary": f"{name}通过核对确认：{fact}",
                "participant_ids": [p["character_id"] for p in participants],
                "location_id": str(location.id), "importance": 0.7,
            }}, fact),
            operation("story_fact_create", {"story_fact": {
                "statement": fact, "temporary_fact_ref": "new_fact_1", "secrecy": "limited",
                "reader_visible": True, "category": "demo_discovery",
                "visibility": {"reader_visible": True, "known_by_character_ids": [p["character_id"] for p in participants]},
            }}, fact),
            operation("character_knowledge_update", {"knowledge": {
                "character_id": str(hero.id), "fact_ref": {"temporary_fact_ref": "new_fact_1"},
                "status": "known",
            }}, fact),
            operation("character_state_update", {"character_state": {
                "character_id": str(hero.id), "field": "mental_condition",
                "expected_old_value": old_state.mental_condition if old_state else None,
                "proposed_new_value": feeling,
            }}, f"{name}的心情变得{feeling}", scene=2),
            operation("plot_thread_update", {"plot_thread": {
                "plot_thread_id": str(thread.id), "action": "advanced", "note": fact,
            }}, fact),
        ],
        "candidate_author_summary": f"{name}核实：{fact}；保存证据并决定继续调查。",
        "candidate_reader_summary": f"{name}核实：{fact}；保存证据并决定继续调查。",
    }
    if companion:
        memory["operations"].append({
            "operation_id": f"ch{chapter_number}-companion-knowledge",
            "update_type": "character_knowledge_update",
            "source_scene_id": f"ch{chapter_number}_scene_2",
            "_quote": f"{other}看完核对过程，同意把结论与猜测分开记录",
            "knowledge": {"character_id": str(companion.id),
                          "fact_ref": {"temporary_fact_ref": "new_fact_1"}, "status": "known"},
        })
    provider = StudioDemoProvider(plan=plan, scenes=scenes, prose=prose, memory=memory)
    prompts = get_default_registry()
    def client(agent):
        return AgentLLMClient(provider, agent_name=agent, model=provider.default_model)
    return ChapterGenerationService(
        session,
        ChapterPlanningService(session, director=build_director_agent(session, client("director"))),
        ChapterDraftService(session, ScenePlannerAgent(
            client("scene_planner"), prompts, ScenePlanValidator(session),
            chapter_target_chars=target_chars), WriterAgent(client("writer"), prompts)),
        ContinuityService(session, llm_checker=LLMContinuityChecker(client("continuity_checker"), prompts),
                          repair_agent=WriterRepairAgent(client("writer_repair"), prompts), mode="hybrid"),
        MemoryExtractionService(session, MemoryExtractorAgent(client("memory_extractor"), prompts)),
        CanonicalCommitService(session),
    )
