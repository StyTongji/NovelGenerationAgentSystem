"""Deterministic 10-chapter scripted story for the committed soak test.

Each chapter spec carries its user direction, two fake-writer scene texts,
and the canonical mutations the fake memory extractor proposes. Evidence
quotes are verbatim substrings of the scene texts by construction.
"""

from __future__ import annotations

from dataclasses import dataclass, field

PROTECTED_READER_SECRET = "魔教教主已下令铲除林凡"
RELIC_FACT_STATEMENT = "轮回宗曾在青云遗迹活动"


@dataclass
class SoakChapter:
    number: int
    direction: str
    scene_texts: list[str] = field(default_factory=list)
    # op payload dicts (MemoryUpdateOperation shape, minus evidence which is
    # derived from scene texts via `evidence_from`)
    memory_ops: list[dict] = field(default_factory=list)
    author_summary: str = ""
    reader_summary: str = ""


def _op(op_id: str, update_type: str, scene_id: str, quote: str, payload_field: str, payload: dict, **extra) -> dict:
    op = {
        "operation_id": op_id,
        "update_type": update_type,
        "source_scene_id": scene_id,
        "evidence": [{"scene_id": scene_id, "exact_quote": quote}],
        payload_field: payload,
    }
    op.update(extra)
    return op


def build_story(world, mirror_fact_id: str, cult_secret_fact_id: str) -> list[SoakChapter]:
    su = str(world["characters"]["苏青"].id)
    lin = str(world["characters"]["林凡"].id)
    elder = str(world["elder"].id)
    father_id = str(world["result"].plot_thread_ids["父亲失踪之谜"])

    chapters: list[SoakChapter] = []

    # ---------------- Chapter 5: 拍卖会 / 苏青受伤 / 顾清璃出现 ----------------
    ch5 = SoakChapter(
        number=5,
        direction=(
            "下一章让林凡和苏青进入天剑城拍卖会。让林凡发现一枚与轮回古镜有关的"
            "玉佩，并遇到一个来自中州的神秘女子。推进父亲失踪伏笔，但只能给出模糊"
            "线索。中途让苏青为了保护林凡受伤。不要让林凡在本章突破。"
        ),
        scene_texts=[
            "天剑城拍卖场人声鼎沸。林凡在雅间落座，注意到角落里一位来自中州的"
            "神秘女子。她报上名号：顾清璃，目光却始终停留在展台方向。",
            "玉佩作为第七件拍品被呈上展台。一名散修突然暴起，伸手抓向玉佩。"
            "苏青一步挡在林凡身前，硬受了一记掌劲，肩头顿时渗出血迹。混乱中，"
            "林凡怀中的轮回古镜滑落出来，苏青看见了那面古镜。护卫赶到，散修被制服。",
        ],
        memory_ops=[
            _op("ch5-event", "create_event", "ch5_scene_2",
                "苏青一步挡在林凡身前，硬受了一记掌劲，肩头顿时渗出血迹。",
                "event", {
                    "event_type": "combat",
                    "author_summary": "天剑城拍卖会争夺玉佩，苏青替林凡挡下一击，"
                                      "轮回古镜在混乱中暴露给苏青",
                    "reader_safe_summary": "拍卖会起冲突，苏青替林凡挡下一击受了伤",
                    "participant_ids": [lin, su],
                    "importance": 0.8,
                    "visibility": {"reader_visible": True},
                }),
            _op("ch5-injury", "character_state_update", "ch5_scene_2",
                "肩头顿时渗出血迹", "character_state", {
                    "character_id": su, "field": "physical_condition",
                    "proposed_new_value": "injured",
                    "new_value_description": "肩头受掌劲，出血", "requires_confirmation": True,
                }),
            _op("ch5-know", "character_knowledge_update", "ch5_scene_2",
                "苏青看见了那面古镜", "knowledge", {
                    "character_id": su, "fact_id": mirror_fact_id, "status": "known",
                }),
            _op("ch5-thread", "plot_thread_update", "ch5_scene_2",
                "轮回古镜滑落出来", "plot_thread",
                {"plot_thread_id": father_id, "action": "advanced"}),
            _op("ch5-entity", "propose_entity", "ch5_scene_1",
                "她报上名号：顾清璃，目光却始终停留在展台方向。",
                "proposed_entity", {
                    "temporary_ref": "new_character_1", "entity_type": "character",
                    "proposed_name": "顾清璃",
                    "attributes": {"origin": "中州", "gender": "女"},
                }),
        ],
        author_summary="拍卖会争夺玉佩；苏青挡剑受伤；古镜秘密暴露给苏青；顾清璃登场。",
        reader_summary="拍卖会上玉佩引出争夺，苏青为林凡受伤，玉佩纹路与古镜相似。",
    )
    chapters.append(ch5)

    # ---------------- Chapter 6: 回客栈调查 / 古镜暂时移交 ----------------
    chapters.append(SoakChapter(
        number=6,
        direction=(
            "第六章林凡和苏青回到客栈调查玉佩。苏青仍带伤。顾清璃提供一条模糊线索。"
            "让林凡把轮回古镜交给苏青查看后再收回。"
        ),
        scene_texts=[
            "回到客栈，林凡把轮回古镜交到苏青手中。苏青借着烛光仔细查看古镜"
            "与玉佩的纹路，确认二者同源。查看完毕，林凡将轮回古镜收回怀中。",
            "顾清璃夜里来访，只留下一句“去查查青云遗迹”，便飘然离去。林凡"
            "记下了这个名字。苏青的伤势在简单包扎后安静下来。",
        ],
        memory_ops=[
            _op("ch6-item1", "item_update", "ch6_scene_1",
                "林凡把轮回古镜交到苏青手中", "item", {
                    "item_id": "@mirror@", "change": "owner_transfer",
                    "expected_old_owner_id": lin, "new_owner_id": su,
                }),
            _op("ch6-item2", "item_update", "ch6_scene_1",
                "林凡将轮回古镜收回怀中", "item", {
                    "item_id": "@mirror@", "change": "owner_transfer",
                    "expected_old_owner_id": su, "new_owner_id": lin,
                }),
            _op("ch6-event", "create_event", "ch6_scene_2",
                "去查查青云遗迹", "event", {
                    "event_type": "investigation",
                    "author_summary": "顾清璃留下青云遗迹线索；林凡调查玉佩与轮回古镜",
                    "reader_safe_summary": "顾清璃留下线索，指向青云遗迹",
                    "participant_ids": [lin, su],
                    "importance": 0.6,
                    "visibility": {"reader_visible": True},
                }),
        ],
        author_summary="客栈调查：古镜玉佩同源；顾清璃指向青云遗迹；古镜短暂移交后收回。",
        reader_summary="林凡与苏青调查玉佩，顾清璃留下青云遗迹的线索。",
    ))

    # ---------------- Chapter 7: 前往遗迹 / 轮回宗事实 ----------------
    chapters.append(SoakChapter(
        number=7,
        direction=(
            "第七章林凡与苏青动身前往城外的青云遗迹，发现轮回宗曾在那里活动"
            "的痕迹。推进父亲失踪线。"
        ),
        scene_texts=[
            "林凡与苏青抵达青云遗迹。残破的石门上刻着轮回宗的徽记，遗迹深处"
            "隐约有灵光流动。",
            "苏青在石壁上辨认出一段记载：轮回宗曾在此炼制秘宝。林凡把这一发现"
            "与玉佩上的归墟纹联系在一起，父亲失踪之谜露出了一角。",
        ],
        memory_ops=[
            _op("ch7-entity", "propose_entity", "ch7_scene_1",
                "林凡与苏青抵达青云遗迹", "proposed_entity", {
                    "temporary_ref": "new_location_1", "entity_type": "location",
                    "proposed_name": "青云遗迹",
                }),
            _op("ch7-fact", "story_fact_create", "ch7_scene_2",
                "轮回宗曾在此炼制秘宝", "story_fact", {
                    "statement": RELIC_FACT_STATEMENT, "secrecy": "limited",
                    "reader_visible": True,
                }),
            _op("ch7-know", "character_knowledge_update", "ch7_scene_2",
                "苏青在石壁上辨认出一段记载", "knowledge", {
                    "character_id": lin, "status": "known",
                    "fact_statement": RELIC_FACT_STATEMENT,
                }),
            _op("ch7-thread", "plot_thread_update", "ch7_scene_2",
                "父亲失踪之谜露出了一角", "plot_thread",
                {"plot_thread_id": father_id, "action": "advanced"}),
            _op("ch7-event", "create_event", "ch7_scene_1",
                "残破的石门上刻着轮回宗的徽记", "event", {
                    "event_type": "discovery",
                    "author_summary": "林凡与苏青进入青云遗迹，发现轮回宗徽记，"
                                      "玉佩与轮回古镜的线索进一步汇聚",
                    "reader_safe_summary": "两人进入青云遗迹，发现轮回宗的痕迹",
                    "participant_ids": [lin, su],
                    "importance": 0.7,
                    "visibility": {"reader_visible": True},
                }),
        ],
        author_summary="进入青云遗迹；确认轮回宗活动事实；父亲线推进。",
        reader_summary="两人进入青云遗迹，发现轮回宗的痕迹。",
    ))

    # ---------------- Chapter 8: 遭遇陈家弟子（duplicate finalize 注入章） ----
    chapters.append(SoakChapter(
        number=8,
        direction=(
            "第八章在遗迹外遭遇陈家弟子，发生战斗但不突破。苏青带伤助战。"
        ),
        scene_texts=[
            "陈家弟子拦在遗迹出口，指责林凡染指陈家猎物。双方剑拔弩张。",
            "战斗爆发。林凡与带伤的苏青联手击退陈家弟子，但林凡压制住突破的"
            "冲动，保留实力。两人撤入遗迹深处。",
        ],
        memory_ops=[
            _op("ch8-event", "create_event", "ch8_scene_2",
                "林凡与带伤的苏青联手击退陈家弟子", "event", {
                    "event_type": "combat",
                    "author_summary": "遗迹外击退陈家弟子；林凡克制未突破",
                    "reader_safe_summary": "林凡与苏青击退了陈家弟子",
                    "participant_ids": [lin, su],
                    "importance": 0.6,
                    "visibility": {"reader_visible": True},
                }),
            _op("ch8-thread", "plot_thread_update", "ch8_scene_2",
                "两人撤入遗迹深处", "plot_thread",
                {"plot_thread_id": father_id, "action": "advanced"}),
        ],
        author_summary="击退陈家弟子；林凡克制未突破。",
        reader_summary="林凡与苏青击退陈家弟子，撤入遗迹。",
    ))

    # ---------------- Chapter 9: 苏青恢复 / 疗伤丹消耗 / reader-visible secret
    chapters.append(SoakChapter(
        number=9,
        direction=(
            "第九章苏青服用疗伤丹，伤势部分恢复，获得进入遗迹核心区域的方法。"
        ),
        scene_texts=[
            "苏青服下疗伤丹，盘膝调息。肩头的伤势恢复大半，她活动了一下手臂，"
            "重新握紧了长剑。",
            "遗迹核心区域的机关需要双人生脉共振才能开启。两人以血脉之力同时"
            "催动石门，遗迹核心缓缓打开。",
        ],
        memory_ops=[
            _op("ch9-state", "character_state_update", "ch9_scene_1",
                "肩头的伤势恢复大半", "character_state", {
                    "character_id": su, "field": "physical_condition",
                    "proposed_new_value": "recovering",
                }),
            _op("ch9-consume", "item_update", "ch9_scene_1",
                "苏青服下疗伤丹", "item", {
                    "item_id": "@pill@", "change": "consumed",
                }),
            _op("ch9-fact", "story_fact_create", "ch9_scene_2",
                "遗迹核心缓缓打开", "story_fact", {
                    "statement": PROTECTED_READER_SECRET, "secrecy": "secret",
                    "reader_visible": True,
                    "visibility": {"reader_visible": True, "known_by": [elder],
                                   "author_only": False},
                }),
            _op("ch9-event", "create_event", "ch9_scene_2",
                "遗迹核心缓缓打开", "event", {
                    "event_type": "discovery",
                    "author_summary": "苏青伤势恢复并服下疗伤丹；遗迹核心区域开启",
                    "reader_safe_summary": "苏青伤势好转，遗迹核心开启",
                    "participant_ids": [lin, su],
                    "importance": 0.6,
                    "visibility": {"reader_visible": True},
                }),
        ],
        author_summary="苏青服用疗伤丹恢复；遗迹核心开启；幕后魔教教主下令针对林凡。",
        reader_summary="苏青伤势好转，两人打开了遗迹核心。",
    ))

    # ---------------- Chapter 10: 遗迹中父亲痕迹 ----------------
    chapters.append(SoakChapter(
        number=10,
        direction=(
            "第十章进入遗迹核心，林凡发现父亲过去留下的痕迹。推进父亲失踪线。"
        ),
        scene_texts=[
            "遗迹核心的石台上留着一行熟悉的字迹——那是林凡父亲的笔迹。旁边"
            "压着半枚同样的玉佩。林凡的手微微颤抖。",
            "苏青按住他的肩膀：“叔叔一定还活着。”两人把半枚玉佩收好，继续"
            "向遗迹更深处探去。",
        ],
        memory_ops=[
            _op("ch10-event", "create_event", "ch10_scene_1",
                "那是林凡父亲的笔迹", "event", {
                    "event_type": "discovery",
                    "author_summary": "遗迹中发现父亲的笔迹与半枚玉佩，"
                                      "父亲失踪之谜与轮回古镜的关联加深",
                    "reader_safe_summary": "林凡发现了父亲留下的痕迹",
                    "participant_ids": [lin, su],
                    "importance": 0.9,
                    "visibility": {"reader_visible": True},
                }),
            _op("ch10-thread", "plot_thread_update", "ch10_scene_1",
                "林凡的手微微颤抖", "plot_thread",
                {"plot_thread_id": father_id, "action": "advanced"}),
            _op("ch10-state", "character_state_update", "ch10_scene_2",
                "苏青按住他的肩膀", "character_state", {
                    "character_id": su, "field": "physical_condition",
                    "proposed_new_value": "healthy",
                }),
        ],
        author_summary="发现父亲笔迹与半枚玉佩；苏青伤愈；父亲线大幅推进。",
        reader_summary="林凡在遗迹中发现父亲留下的痕迹。",
    ))

    # ---------------- Chapter 11: 魔教异常 / 林凡产生怀疑 ----------------
    chapters.append(SoakChapter(
        number=11,
        direction=(
            "第十一章魔教相关异常出现，林凡只能产生怀疑，不能确认任何身份。"
        ),
        scene_texts=[
            "遗迹外出现了魔教探子的脚印，方向直指天剑城。林凡想起玄清真人"
            "最近的种种反常举动，心里生出一丝疑虑。",
            "苏青劝他不要妄下判断。两人决定先回宗门汇报，再作打算。",
        ],
        memory_ops=[
            _op("ch11-know", "character_knowledge_update", "ch11_scene_1",
                "心里生出一丝疑虑", "knowledge", {
                    "character_id": lin, "fact_id": cult_secret_fact_id,
                    "status": "suspected",
                }),
            _op("ch11-event", "create_event", "ch11_scene_1",
                "遗迹外出现了魔教探子的脚印", "event", {
                    "event_type": "suspicion",
                    "author_summary": "魔教探子现身；林凡开始怀疑玄清真人",
                    "reader_safe_summary": "林凡发现了魔教活动的迹象",
                    "participant_ids": [lin],
                    "importance": 0.7,
                    "visibility": {"reader_visible": True},
                }),
            _op("ch11-thread", "plot_thread_update", "ch11_scene_2",
                "两人决定先回宗门汇报", "plot_thread",
                {"plot_thread_id": father_id, "action": "advanced"}),
        ],
        author_summary="魔教探子现身；林凡对玄清真人产生怀疑（未确认）。",
        reader_summary="林凡注意到魔教活动的迹象，心生疑虑。",
    ))

    # ---------------- Chapter 12: 顾清璃身份信息（rollback 注入章） ----------
    chapters.append(SoakChapter(
        number=12,
        direction=(
            "第十二章顾清璃再度现身，透露自己与轮回宗有关，但核心秘密仍不揭露。"
        ),
        scene_texts=[
            "顾清璃再次出现在遗迹入口，承认自己来自中州顾氏，与轮回宗有着"
            "千丝万缕的联系，但拒绝透露更多。",
            "林凡试探再三，顾清璃只留下一句“时机未到”，便转身离去。",
        ],
        memory_ops=[
            _op("ch12-event", "create_event", "ch12_scene_1",
                "承认自己来自中州顾氏", "event", {
                    "event_type": "conversation",
                    "author_summary": "顾清璃承认与轮回宗的关联；核心身份仍未揭露",
                    "reader_safe_summary": "顾清璃透露了更多背景",
                    "participant_ids": [lin],
                    "importance": 0.6,
                    "visibility": {"reader_visible": True},
                }),
            _op("ch12-thread", "plot_thread_update", "ch12_scene_2",
                "顾清璃只留下一句“时机未到”", "plot_thread",
                {"plot_thread_id": father_id, "action": "advanced"}),
        ],
        author_summary="顾清璃透露顾氏与轮回宗的关联；核心秘密保持沉默。",
        reader_summary="顾清璃再次现身，透露了更多背景。",
    ))

    # ---------------- Chapter 13: 返回宗门 / PlotDebt due_soon ----------------
    chapters.append(SoakChapter(
        number=13,
        direction=(
            "第十三章林凡返回宗门，重新检查轮回古镜，宗门大会即将召开。"
        ),
        scene_texts=[
            "林凡回到青玄宗，把轮回古镜与两枚玉佩并排放在案上。纹路相合，"
            "指向同一个答案。宗门大会的钟声已近，他必须先交代遗迹之行。",
            "苏青帮他整理行装。两人约定，大会之后立刻动身追寻父亲的足迹。",
        ],
        memory_ops=[
            _op("ch13-event", "create_event", "ch13_scene_1",
                "把轮回古镜与两枚玉佩并排放在案上", "event", {
                    "event_type": "investigation",
                    "author_summary": "林凡回宗门整理轮回古镜与玉佩线索；"
                                      "宗门大会在即",
                    "reader_safe_summary": "林凡回宗门整理线索，宗门大会将至",
                    "participant_ids": [lin, su],
                    "importance": 0.5,
                    "visibility": {"reader_visible": True},
                }),
            _op("ch13-thread", "plot_thread_update", "ch13_scene_2",
                "大会之后立刻动身追寻父亲的足迹", "plot_thread",
                {"plot_thread_id": father_id, "action": "advanced"}),
        ],
        author_summary="回宗门整理线索；宗门大会在即。",
        reader_summary="林凡回到宗门，宗门大会即将召开。",
    ))

    # ---------------- Chapter 14: 宗门大会 / PlotDebt resolve ----------------
    chapters.append(SoakChapter(
        number=14,
        direction=(
            "第十四章宗门大会正式召开，林凡汇报遗迹之行。第一阶段的宗门篇"
            "接近尾声。"
        ),
        scene_texts=[
            "宗门大殿之上，林凡向诸位长老完整陈述了遗迹之行与玉佩之谜。"
            "玄清真人坐在上首，神色如常。",
            "大会结束，掌门宣布宗门将正式调查遗迹。林凡知道，真正的旅途"
            "才刚刚开始。",
        ],
        memory_ops=[
            _op("ch14-event", "create_event", "ch14_scene_1",
                "林凡向诸位长老完整陈述了遗迹之行与玉佩之谜", "event", {
                    "event_type": "assembly",
                    "author_summary": "宗门大会召开，林凡汇报遗迹之行；"
                                      "宗门正式介入调查",
                    "reader_safe_summary": "宗门大会召开，林凡做了汇报",
                    "participant_ids": [lin, su],
                    "importance": 0.8,
                    "visibility": {"reader_visible": True},
                }),
            _op("ch14-debt", "plot_debt_update", "ch14_scene_2",
                "大会结束", "plot_debt", {
                    "plot_debt_id": "@sect_debt@", "action": "resolved",
                }),
            _op("ch14-thread", "plot_thread_update", "ch14_scene_2",
                "真正的旅途才刚刚开始", "plot_thread",
                {"plot_thread_id": father_id, "action": "advanced"}),
        ],
        author_summary="宗门大会召开；林凡汇报；宗门介入遗迹调查；宗门篇收束。",
        reader_summary="宗门大会召开，林凡汇报了遗迹之行。",
    ))

    return chapters
