# AGENTS.md — 长篇玄幻小说生成引擎开发总指令

## 0. 你的身份

你是本仓库的首席软件工程师与 AI 系统工程师。你的任务不是做一个“把前文拼进 prompt 然后续写”的 Demo，而是构建一个可长期维护、可生成数百/上千章长篇小说的 **Narrative Engine（叙事引擎）**。

本系统核心目标：

1. 通过大模型 API 分阶段规划并生成章节；
2. 用结构化世界状态记录人物、关系、修炼体系、能力、物品、地点、势力、事件、伏笔、承诺、角色知识等；
3. 每章完成后自动抽取状态变化并更新数据库；
4. 生成下一章前按需检索相关记忆，而不是把整本小说塞入上下文；
5. 用户可随时指导下一章、锁定设定、调整剧情方向；
6. 自动检查时间线、人物知识、战力、物品归属、死亡状态、伏笔泄漏等连续性问题；
7. 数据库中的 Canonical Story State 是事实真相源，正文不是数据库。

---

## 1. 不可违反的架构原则

### 1.1 Canonical State First

小说正文是叙述层，结构化数据库是世界事实层。

绝不能通过“重新总结所有正文”来推断当前世界状态作为主流程。当前状态必须来自数据库 + 事件日志。

### 1.2 Event Sourcing

所有重要剧情变化都必须能表示为 Event，并保留：

- 发生在哪一章；
- 参与者；
- 地点；
- 事件摘要；
- 对人物、关系、物品、知识、任务、伏笔造成的变化；
- 重要度；
- 是否用户手工确认。

当前状态可以通过直接保存 snapshot 提速，但事件日志不能丢。

### 1.3 Character Knowledge Isolation

必须区分：

- world_truth：世界真实事实；
- reader_known：读者已知事实；
- character_known：某角色已知事实；
- character_suspected：角色怀疑但未确认的事实。

Writer 只能拿到对应 POV 角色合理知道的信息。

### 1.4 Minimal Necessary Context

不要把全部 Lore / 全人物 / 全历史塞给 Writer。

Context Builder 必须按：

- 当前章节计划；
- 当前场景参与者；
- 当前地点；
- 活跃剧情线；
- 最近章节；
- 向量检索相关事件；
- 强约束；

构造最小必要上下文。

### 1.5 Structured Output Everywhere

Director、Memory Extractor、Continuity Checker 等非文学写作 Agent 必须输出 Pydantic 可验证 JSON。

禁止使用脆弱的字符串解析。

### 1.6 Plan Before Prose

生成章节必须遵守：

User Direction
→ Chapter Plan
→ Plan Validation
→ Scene Plan
→ Context Build
→ Scene Writing
→ Continuity Check
→ Rewrite
→ Memory Extraction
→ Transactional State Update
→ Summary

不得默认直接调用 Writer 续写。

### 1.7 Human Override

用户指令优先级：

1. hard_constraints
2. 用户当前章节指令
3. Canonical World Rules
4. Arc Plan
5. Director 自由发挥

如果用户新指令与已有 hard constraint 冲突，不要静默覆盖，返回 conflict 给前端。

---

## 2. MVP 技术栈

优先实现：

- Python 3.12
- FastAPI
- Pydantic v2
- SQLAlchemy 2.x
- SQLite（本地 MVP）
- 数据模型保持 PostgreSQL 兼容
- VectorStore 抽象：MVP 可使用 FAISS；生产建议 pgvector
- Alembic
- pytest
- httpx
- 任意 OpenAI-compatible LLM API，通过统一 provider 抽象接入

前端可在后续阶段使用 React/Next.js。

不要在 Phase 1 为微服务、Kafka、复杂 K8s、分布式队列做过度设计。

---

## 3. 必须实现的核心领域实体

至少包括：

- Novel
- StorySnapshot
- Character
- CharacterState
- CharacterKnowledge
- Relationship
- PowerSystem
- Realm
- Ability / Technique
- Item
- Location
- Faction
- Event
- Chapter
- ChapterSummary
- Arc
- PlotThread
- Foreshadowing
- PlotDebt
- Constraint
- StoryFact

详细字段参考 `docs/DATA_MODEL.md`。

---

## 4. 核心 Agent

MVP 只实现以下 4 个核心 Agent，避免一开始多 Agent 爆炸：

### 4.1 DirectorAgent

职责：决定“本章发生什么”，不负责写文笔。

输入：
- story snapshot
- current arc
- active plot threads
- open plot debts
- hard/soft constraints
- recent summaries
- retrieved relevant events
- user direction

输出：ChapterPlan JSON。

### 4.2 WriterAgent

职责：根据 ScenePlan 写文学正文。

Writer 不允许更新数据库，不允许自行创造违反 Canon 的硬事实。

### 4.3 MemoryExtractorAgent

职责：从最终确认章节中抽取结构化变化。

输出：MemoryUpdateBatch。

MemoryUpdateBatch 必须先校验，再在数据库事务中 apply。

### 4.4 ContinuityCheckerAgent

职责：检查并报告：
- 时间线矛盾；
- 地理移动矛盾；
- 角色知识泄漏；
- 死亡/伤势矛盾；
- 境界和战力违规；
- 技能/法宝所有权错误；
- 伏笔提前揭示；
- hard constraints 违反；
- 角色性格严重漂移。

输出：ContinuityReport JSON。

---

## 5. 每章生成的事务边界

生成章节过程中不要修改 Canonical State。

只有最终章节通过验证后：

1. 保存 FinalChapter；
2. MemoryExtractor 提取变化；
3. 验证变化；
4. 在同一 DB transaction 中：
   - append events；
   - 更新实体当前状态；
   - 更新 plot thread/debt/foreshadowing；
   - 保存 chapter summary；
   - 创建新的 StorySnapshot；
5. commit。

任何一步失败都 rollback，不允许出现“正文已保存但状态没更新一半”的情况。

---

## 6. 开发顺序

严格按 `TASKS.md` 实现。

第一阶段不要写完整前端。

完成一个 Phase 后：

- 运行测试；
- 更新 README；
- 输出当前完成项、剩余风险；
- 不要自行跳过验收标准。

---

## 7. 编码规范

- 所有领域对象使用明确类型；
- service 层禁止返回任意 dict，优先 Pydantic model；
- repository 与 service 分离；
- LLM provider 与业务逻辑分离；
- Prompt 文件放 `prompts/`，不得散落硬编码；
- 数据库更新必须由 domain/service 层控制，Agent 不能直接执行 SQL；
- 每个复杂业务规则至少一个测试；
- 对外 API 使用版本前缀 `/api/v1`；
- ID 优先 UUID；
- 所有模型带 created_at / updated_at；
- 事件、章节、snapshot 永不原地覆盖，修订使用 version。

---

## 8. LLM 调用规范

统一实现：

```python
class LLMProvider(Protocol):
    async def generate_text(...): ...
    async def generate_structured(..., response_model: type[BaseModel]): ...
```

应支持：
- base_url
- api_key
- model
- temperature
- max_tokens
- timeout
- retry

不要在具体 Agent 中绑定某一家模型厂商 SDK。

默认建议：
- Director: temperature 0.4~0.6
- MemoryExtractor: 0~0.2
- ContinuityChecker: 0~0.2
- Writer: 0.7~1.0

---

## 9. 禁止事项

不要：

- 把整个前文直接塞进每次 prompt；
- 只用向量数据库作为唯一真相源；
- 只保存自然语言 summary 而没有结构化状态；
- 把“读者知道”和“人物知道”混在一起；
- 让 Writer 直接改人物境界/物品所有权数据库；
- 每个动作都起一个 Agent；
- 第一版引入复杂 workflow framework，除非现有代码明确需要；
- 在没有测试的情况下实现自动 retcon；
- 自动接受 MemoryExtractor 的任何更新而不校验。

---

## 10. Definition of Done

MVP 完成时必须能演示：

1. 创建一本小说；
2. 初始化玄幻世界设定、境界体系和 3 个角色；
3. 用户输入“下一章剧情指导”；
4. Director 生成结构化 ChapterPlan；
5. 系统按 ScenePlan 生成章节；
6. ContinuityChecker 检查；
7. MemoryExtractor 抽取本章事件和状态改变；
8. 更新人物、物品、关系、知识和伏笔；
9. 下一章能检索并正确使用这些历史信息；
10. 对一个故意制造的矛盾测试能成功报警，例如“已死亡角色再次出场”或“角色知道尚未公开秘密”。
