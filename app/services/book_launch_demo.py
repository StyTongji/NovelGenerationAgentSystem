"""Synthetic demo outputs, explicitly separate from real market evidence."""
from datetime import datetime, UTC

from app.schemas.book_launch import BookBlueprint, MarketBook, MarketSample, MarketResearch


def demo_sample():
    return MarketSample(synthetic=True, coverage="离线合成样本，非真实榜单", books=[MarketBook(
        id="demo_1", platform="离线演示", title="虚构样本：修复者的旅程", synopsis="能力有代价，靠选择推进的玄幻冒险。",
        source_url="https://example.invalid/demo", observed_at=datetime.now(UTC),
    )])


def demo_research(source_id):
    return MarketResearch.model_validate({
        "findings": [{"observation": "演示样本强调能力代价", "source_ids": [source_id], "hypothesis": "可探索技能限制带来的持续冲突；不是市场结论"}],
        "topics": [{"id": f"topic_{i+1}", "title": title, "protagonist": name,
                    "logline": logline, "reader_promise": "从小处解决危机，逐步揭开制度困境",
                    "differentiation": diff, "opening_hook": "第一份委托将能力代价变成现实选择",
                    "risks": ["需避免重复接单升级"], "source_ids": [source_id]}
                   for i, (title, name, logline, diff) in enumerate([
                       ("借火人", "林照野", "修炉少年只能借别人的余火，每次借火都需偿还一段人生时间", "修复技艺与时间债务"),
                       ("听山契", "陆闻溪", "测绘学徒能听见山脉的旧誓，却必须亲自履行未竟承诺", "地理探索与誓约责任"),
                       ("雨骨城", "顾青禾", "制伞少女发现雨中残留着失踪者的记忆，读取会失去自己的片段", "城市悬疑与记忆取舍"),
                   ])],
        "limitations": ["全部内容为离线合成，用于测试流程，未运行真实模型或市场评估。"],
    })


def demo_blueprint(topic):
    beats = ["接下修复旧灯的委托", "辨认旧灯的裂纹", "与送灯人核对来历", "寻找合适的补材", "修复时发现人为破坏",
             "在集市追查材料流向", "面临违约赔偿", "与竞争者交换线索", "排除一条错误线索", "交付修复的灯并收到新委托"]
    return BookBlueprint.model_validate({
        "title": topic.title, "genre": "玄幻", "public_premise": topic.logline,
        "author_outline": "第一卷从修复委托建立人物关系；第二卷调查旧工坊；第三卷围绕能力代价和公平交易作出最终选择。所有转折均是未来计划。",
        "world": {"power_system": {"name": "火契", "rules": ["借火必须偿还，不能无代价越阶"],
                   "realms": [{"name": "凡人", "rank": 0}, {"name": "引火", "rank": 1}, {"name": "铸心", "rank": 2}]},
                  "locations": [{"name": "溪桥镇"}, {"name": "旧工坊", "parent_name": "溪桥镇"}],
                  "characters": [{"name": topic.protagonist, "is_protagonist": True, "realm_name": "凡人", "location_name": "旧工坊",
                                  "personality_traits": ["谨慎", "重诺"], "current_goals": ["挣够工坊租金"]},
                                 {"name": "温拾", "realm_name": "引火", "location_name": "溪桥镇", "personality_traits": ["爽直"], "current_goals": ["追回货款"]},
                                 {"name": "贺叔", "realm_name": "引火", "location_name": "旧工坊", "personality_traits": ["寡言"], "current_goals": ["守住旧工坊"]}],
                  "factions": [{"name": "溪桥工坊", "leader_name": "贺叔", "goals": ["维持工坊生计"]}],
                  "items": [{"name": "修炉锤", "owner_name": topic.protagonist, "location_name": "旧工坊"}],
                  "initial_facts": [{"statement": "借火不能免除偿还义务", "secrecy": "public"},
                                    {"statement": "旧工坊地窖里封存着一份旧契", "secrecy": "secret", "known_by": ["贺叔"]}],
                  "plot_threads": [{"title": "工坊生计", "public_summary": "如何保住工坊", "status": "active"}],
                  "arc": {"title": "溪桥旧火", "planned_end_chapter": 10, "goal": "完成第一单并建立信任"}},
        "volumes": [{"title": "溪桥旧火", "start_chapter": 1, "end_chapter": 10, "goal": "站稳脚跟", "turning_points": ["发现破坏痕迹", "选择诚信交付"]},
                    {"title": "契约之城", "start_chapter": 11, "end_chapter": 60, "goal": "调查能力代价与行业秩序", "turning_points": ["发现旧契", "共同承担代价"]}],
        "opening_chapters": [{"chapter": i+1, "title": beat, "goal": beat, "conflict": "委托期限与材料不足",
                              "ending_hook": "一个具体的新问题", "forbidden_reveals": ["旧契全貌"]} for i, beat in enumerate(beats)],
    })
