# 给 Coding Agent 的首轮指令

请完整阅读并遵守本仓库中的：

1. `AGENTS.md`
2. `TASKS.md`
3. `skills/novel-engine/SKILL.md`
4. 当具体任务涉及 story memory、章节生成或一致性检查时，再读取对应 `skills/*/SKILL.md`

你的目标是从零构建一个用于超长篇中文玄幻小说生成的 Narrative Engine。

## 核心要求

这不是普通的 RAG 续写应用。必须实现：

- Canonical Story State；
- Event Log；
- Character Knowledge isolation；
- 人物/能力体系/法宝/地点/势力/伏笔/剧情债务；
- Chapter Plan → Scene Plan → Writer；
- Continuity Checker；
- Memory Extractor；
- Transactional state update；
- 最近剧情 + 相关历史的按需 Context Retrieval；
- 用户对下一章剧情的自然语言指导；
- hard / soft constraints。

## 执行方式

从 `TASKS.md` 的 Phase 0 开始，不要一次性实现全部系统。

每个 Phase：

1. 先检查已有代码与相关规范；
2. 给出简短实现计划；
3. 实现；
4. 编写/更新测试；
5. 运行测试；
6. 修复失败；
7. 更新 README 中的当前进度；
8. 汇报：完成内容、测试结果、设计决策、下一 Phase。

## 重要限制

- 不得跳过测试；
- 不得把全文或全数据库 dump 给 Writer；
- 不得让 LLM 直接修改数据库；
- 非文学 Agent 必须 structured output；
- MemoryExtractor 只产出 proposal delta，Domain Service 验证后才能 apply；
- Writer 不得知道 POV 角色不应知道的隐藏事实；
- Critical continuity error 存在时不得 finalize chapter；
- Chapter regeneration 使用新 version，不覆盖旧版本；
- 第一版优先简单可靠，不引入不必要的 workflow framework 和微服务。

## 首次任务

现在只执行 Phase 0 和 Phase 1。

完成后暂停扩展功能，向我展示：

- 项目目录；
- 数据模型；
- migration；
- 关键测试；
- `pytest` 结果；
- 你认为 Phase 2 前仍需解决的风险。
