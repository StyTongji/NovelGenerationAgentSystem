# TASKS.md — 实现路线图与验收标准

## Phase 0 — 仓库骨架

### 任务
- 建立 `app/agents`, `app/models`, `app/repositories`, `app/services`, `app/api`, `app/llm`, `app/vector`, `tests`。
- 配置 `pyproject.toml`。
- 配置 `.env.example`。
- FastAPI `/health`。

### 验收
- `pytest` 可运行；
- app 可启动；
- `/health` 返回 ok。

---

## Phase 1 — Canonical Story State

### 任务
实现 SQLAlchemy + Pydantic：
- Novel
- Character / CharacterState
- Relationship
- CharacterKnowledge
- PowerSystem / Realm
- Ability
- Item
- Location
- Faction
- StoryFact
- Event
- PlotThread
- Foreshadowing
- PlotDebt
- Constraint
- Arc
- Chapter
- ChapterSummary
- StorySnapshot

实现 migration。

### 关键要求
- 当前状态与事件日志共存；
- CharacterKnowledge 必须指向具体 character；
- StoryFact 需要 visibility/secret level；
- Chapter 不可覆盖旧 version。

### 验收测试
- 创建角色与境界；
- 更新人物状态；
- 添加事件；
- 记录一个只有反派知道的秘密；
- 创建 plot thread 和 unresolved foreshadowing；
- snapshot 可序列化。

---

## Phase 2 — LLM Provider + Structured Output

### 任务
建立统一 LLMProvider。

支持：
- OpenAI-compatible endpoint；
- text generation；
- structured generation；
- retry；
- timeout；
- JSON/Pydantic validation error retry。

### 验收
使用 FakeLLMProvider 完成所有单元测试，不依赖真实 API。

---

## Phase 3 — Story Context Builder

### 任务
实现 ContextBuilder：

输入：
- novel_id
- user_direction
- scene/chapter plan（可选）

输出 `StoryContextBundle`：
- current snapshot
- relevant characters
- POV-safe character knowledge
- relevant locations
- current power rules
- active plot threads
- open debts
- hard/soft constraints
- recent chapter summaries
- relevant historical events
- previous chapter tail

### 检索策略
1. deterministic retrieval 优先；
2. vector retrieval 补充历史事件；
3. recency + relevance + importance 混合排序。

建议评分：

`score = 0.50 * semantic + 0.25 * recency + 0.25 * importance`

### 验收
给定“去天剑城拍卖会”，Context 中应包含：
- 当前同行人物；
- 天剑城；
- 拍卖相关历史事件；
- 当前持有灵石/重要物品；
- 活跃相关伏笔；
而不包含无关人物的全部档案。

---

## Phase 4 — DirectorAgent

### 任务
根据 `prompts/director.md` 与 `schemas/chapter_plan.schema.json` 实现 Director。

实现 plan validator：
- hard constraints；
- dead character；
- location feasibility；
- realm rules；
- unrevealed secrets；
- arc boundaries。

### 验收
用户指令：
“下一章去拍卖会，让主角看到一枚和父亲有关的残缺玉佩，但不要揭露父亲去向。”

输出应：
- 规划 3~6 个 scene；
- 设置 chapter hook；
- 父亲真相仍在 must_not_reveal；
- 至少推进一次相关 plot thread。

---

## Phase 5 — WriterAgent

### 任务
- 按 scene 逐段生成；
- 每个 scene 使用 SceneContext，不使用全库 dump；
- 保留上一 scene 的末尾用于衔接；
- 支持 writing style profile。

### 验收
- 生成完整章节；
- scene 之间连续；
- 不出现 plan 未授权的重大事实揭示。

---

## Phase 6 — Continuity Checker

### 任务
实现规则检查 + LLM 检查混合模式。

先 deterministic：
- dead/alive
- inventory owner
- realm ordering
- hard constraint
- time ordering

再 LLM：
- 人设漂移
- 语义级知识泄漏
- 隐性逻辑冲突

### 验收
至少构造 10 个 regression tests：
1. 死者复活；
2. 未拥有法宝被使用；
3. 角色知道秘密；
4. 境界跳级；
5. 地点瞬移；
6. hard constraint 违反；
7. 已回收伏笔又当未解；
8. 同一物品双 owner；
9. 时间期限错误；
10. 明显性格反转无原因。

---

## Phase 7 — Memory Extractor + Transactional Apply

### 任务
MemoryExtractor 输出 `MemoryUpdateBatch`。

实现：
- create_event
- character_state_update
- relationship_update
- knowledge_update
- item_transfer
- item_state_update
- plot_thread_update
- foreshadowing_update
- plot_debt_update
- story_fact_create

所有 apply 使用 transaction。

### 防护
- realm update 必须验证 progression；
- item transfer 要检查当前 owner；
- knowledge update 不能自动修改 world_truth；
- 删除/死亡等高风险变化需要 evidence span。

### 验收
生成一章“苏青替林凡挡剑并得知古镜存在”，数据库应正确产生：
- rescue event；
- 苏青 injury；
- 苏青→林凡 relationship 改变；
- 苏青新增 knowledge；
- 古镜 owner 不变化。

---

## Phase 8 — End-to-End Chapter Service

实现：

```text
prepare_context
→ plan
→ validate_plan
→ generate_scenes
→ continuity_check
→ conditional_rewrite
→ final_check
→ extract_memory
→ transactional_apply
→ summarize
→ snapshot
```

### API
- POST `/api/v1/novels`
- GET `/api/v1/novels/{id}`
- POST `/api/v1/novels/{id}/chapters/generate`
- GET `/api/v1/novels/{id}/chapters/{number}`
- GET `/api/v1/novels/{id}/state`
- POST `/api/v1/novels/{id}/constraints`
- GET `/api/v1/novels/{id}/plot-threads`

### 验收
FakeLLM 情况下 e2e 测试完整通过。

---

## Phase 8.7 — Real Memory Closure

### 目标
- 用同一真实 Chapter 1 Draft 重跑 Memory Extraction，不重跑 Director / ScenePlanner / Writer；
- 证据只允许引用服务器生成的 EvidenceCatalog ID；事实引用通过 canonical fact_id 或 batch-local temporary_fact_ref；
- 通过 MemoryValidation 与 DryRun 后，完成第一次真实 Canonical finalize，并验证 duplicate finalize 零副作用；
- Provider 内部 timeout 与 Agent 总 deadline 共存，取消必须立即传播。

### 当前实现
- 真实失败样本与 8 个 `evidence_not_in_draft`、3 个 `knowledge_unknown_fact` 已固化到 `tests/fixtures/real_memory_closure/`；
- EvidenceCatalog / FactRef / FactCatalog / Memory Extractor v3 / `memory-retry` 已实现；
- 2026-10-22：before-state SQL 恢复后实际运行 Validator 复现 8＋3；v3 正式入口拒绝旧契约降级、空 refs、伪造 catalog/offsets、未知/重复 temp ref；提交绑定人工 approval。
- 所有 Agent 走统一 deadline；阶段 checkpoint、resume/audit、SSE 子进程清理、保守预算台账已实现。
- **离线已验证**：`pytest -m 'not integration'` 283 passed，2 deselected（含 fake 十章 soak）。正式 service 并发/篡改/重复提交、取消零后续 mutation 已验证。
- **真实模型已验证**：SCNet Flash-1017/Auto 小文本和 JSON 探测成功；原稿 Flash Memory 20 ops，旧两类错误为 0，但另外 3 个 critical 导致 valid=false；Auto 大请求 120s read timeout。
- **人工已批准**：无。真实 Chapter/Commit/Snapshot/Event 均为 0。没有首次真实 finalize。
- **阻塞**：本章预算占用 131,680/150,000（其中 73,015 是未知 usage 预留），不足以下一次同规模请求；各 3 次 A/B、文笔对照、原稿验收均未完成。不得清空预留或更换 run 绕过预算。
- [实施报告与成本/阻塞项](docs/REAL_CLOSURE_20261022.md)。

### 用户批准的后续顺序（不得跳过门槛）
- [ ] P1：先离线缩减输入/输出冗余并测算；获明确追加预算后新 cohort 完成 Flash/Auto 各 3 次对照，保留所有失败。
- [ ] P2：原稿 Memory 合法且语义合格 → hybrid 审查 → 仅受影响场景另存修订 → 全部重新验证 → 人工批准 → 首次/重复提交审计。
- [ ] P2：Chapter 2、3 逐章确认和提交，验证继承、知识隔离、归属、债务、previous ending 和 retrieval。
- [ ] P3（三章真实闭环后）：十章剧情段/三章窗口、正文实际场景交接、30 条检索用例、独立文学报告、真实十章审计。
- [ ] 十章标准：至少 8 章无需人工改代码；每章最多两轮自动修复；确认的硬设定冲突/知识泄漏为 0；文学单项不得低于 3 分。

### 2026-10-25 — 作者审阅/修订可用性（历史阶段记录）

- **代码已实现**：`inspect` 导出可读报告、分场景正文与 Memory Batch 编辑副本；`revise` 沿用规划、仅替换指定场景，重新检查整章与提取记忆；`memory-edit` 本地重验手改记忆，不调用模型。
- 新尝试保留原稿与来源、保存修订输入；resume 复用已完成阶段；未改场景不重写；旧 approval 不用于新候选。不新增 Agent、UI 或额外验证框架，复用既有服务。
- **离线已验证**：新增 7 个作者工作流用例，覆盖真实 fixture 的 v3 重验、原稿不变、未改场景不变、数据库零 mutation、失败停止与 checkpoint、旧确认失效、恢复不重抽、阅读副本不覆盖及 Memory 编辑不调用 API。`pytest -m 'not integration'` 最终 **305 passed, 2 deselected**，包含 fake 十章 soak。
- **真实模型已验证（已有产物复核）**：`stream32_flash_1/2/3` 均 Memory valid、dry-run applicable，operations 数 23/21/15。不能写成原 DeepSeek-V4-Flash 或 Auto 验收通过；不能以结构合法代替语义合格。
- **人工已批准**：仍无。本轮没有真实模型调用、真实 finalize 或启动上次被拦截的后台任务。
- **只读审计**：原真实 run 的 Chapter/Commit/Snapshot/Event 各 0；state hash 仍为 `0df9904dfcb49ace0abefd70fac779072f5d49e5129c007c264798983c96011d`，与 state 文件一致。
- **下一步**：人工查看真实候选 `reading/review.md`，核对命名、知识确定性及过度抽取；必要时局部修订/记忆编辑；全部检查通过后再批准首次 finalize。P2/P3 与真实三章/十章仍未完成。

## Phase 9 — Minimal UI

2026-10-30：用户明确要求完善 multi-agent system、前端交互与可视化，本次授权替代此前暂停 UI 的约束。保留真实三章/十章质量验收门槛，不因 UI 或离线演示完成而将它们标为通过；不扩展外部 literary skills。

### 已实现

- [x] `ChapterGenerationService` 使用真实 LangGraph `StateGraph`；Director → Draft → Continuity → 条件 Repair → Memory → Package；保留原 service 和事务语义。
- [x] 七角色职责与模型覆盖：Director、ScenePlanner、Writer、ContinuityChecker、WriterRepair、MemoryExtractor、LiteraryReviewer；编排器不计作 LLM Agent。
- [x] 独立文学评审契约：五项 1–5 分、场景 ID、逐字证据、建议、draft hash；live 模式执行，demo 不执行，评分不替代作者判断。
- [x] `/` 与 `/studio` 前端：新故事设定、正文与分场景修订、记忆操作核对与编辑、逐章批准、继续生成、停止/恢复、文本导出。
- [x] Agent 工作流图、角色说明、阶段/API 事件时间线、已提交人物/物品/知识/线索等世界状态查看。
- [x] 单机单 worker 持久任务；`TrialAttempt` 沿用阶段/场景文件 checkpoint，恢复时核对状态、输入、prompt 与模型配置；新候选使旧批准失效。
- [x] 演示数据与真实模型模式显式分离；demo 每章约 500–800 字脚本素材，不是目标字数的模型创作或真实十章验收。
- [x] 内置服务密钥由服务端环境配置；自定义服务支持页面输入并按任务保存在内存。原 `real_trial_runs` 独立数据库不自动导入；Studio 产物保存在 `studio_runs/<run>/`。
- [x] 使用说明与本地无鉴权边界：[docs/STUDIO.md](docs/STUDIO.md)。

### 验证状态

- **代码已实现**：上述工作流、角色、前端与服务接口。
- **离线已验证**：全量 `pytest -m 'not integration'` 329 passed、2 deselected；最终 SSE 竞态修正及新增用例定向回归 12 passed。覆盖图分支、失败停止、取消、checkpoint 恢复、文学证据、deadline、十章脚本提交和失败稿修订；浏览器已验证创建、审批推进、局部修订与可视化。[验收记录](docs/STUDIO_VALIDATION_20261030.md)。
- **真实模型已验证**：本轮无新增验证；历史 Flash-1017 同稿 Memory 结构验收不等于 Studio 连续创作验收。
- **人工已批准**：无新增真实章节批准。自动化测试中的批准仅为测试输入。

### 剩余验收

- [ ] 固定模型完成真实 Chapter 1 审阅与首次/重复提交审计。
- [ ] 三章逐章审阅和跨章继承验收。
- [ ] 真实十章连续性、文学质量、成本与失败率报告。
- [ ] 增强章节/剧情段规划与实际场景交接，依据真实使用结果决定，不靠增加角色数量代替质量验证。

### 2026-10-30 · DeepSeek 官方 / SCNet 服务预设

- **代码已实现**：内置两家服务地址和各自模型候选；支持保留自定义配置、独立密钥及 `.env` 加载；前端展示服务地址和密钥就绪状态，切换服务同步模型候选并清空角色模型覆盖。
- **离线已验证**：相关测试 44 passed，最终预设与初始化复验 6 passed；覆盖两家实际 Agent 请求的模拟 HTTP URL/model/key 路由、密钥不混用、dotenv 与环境优先级、配置覆盖及工作台既有行为。浏览器已确认两项服务可选、切换同步模型/地址/密钥提示；不访问模型 API。
- **真实模型已验证**：本轮无调用，不把模拟接入测试视为账户连通性或创作质量验收。

### 2026-10-30 · 页面自定义 API

- **代码已实现**：新增自定义服务入口，可填写 Base URL、API Key、精确模型 ID，沿用角色模型覆盖；完整 completion URL 自动规范化。密钥按任务保存在内存，不进入任务配置、checkpoint、事件或响应；重启后可补充密钥继续。用户本次要求覆盖此前仅环境变量输入密钥的限制。
- 修复真实服务构建器漏导入 `ContinuityService`，测试覆盖真实服务工厂及两个自定义任务的独立凭据。
- **离线已验证**：相关 42 项测试通过，最终签名恢复与工作台复验 13 passed；涵盖自定义 HTTP 路由、凭据脱敏、不落盘、环境 key 不串用、重启补充 key、内置服务和工作台回归。浏览器已验证自定义填写/提交到本机测试地址、重启后密码框补充与恢复，产物中未发现测试密钥。未调用外部付费模型。

### 2026-10-30 · 结束创作记录与 Agent 辅助填写

- **代码已实现**：区分暂停（可恢复）与结束归档；新增 `finish` 与显式 `replace_existing` 新建选项，先取消并 await 旧任务，保留原正文及 Canonical 提交，新记录从已提交章节继续。
- **代码已实现**：复用 LiteraryReviewer 为审阅表单建议 operation verdict/reason、漏提项和五项评分；生成建议不写 Canonical，采用后仍需人工确认。每次建议单独保存并绑定候选，过期建议不能用于批准，采用建议 hash 进入批准审计；新辅助 prompt 不使旧生成 checkpoint 失效。
- **离线已验证**：全量 `pytest -m 'not integration'` **346 passed, 2 deselected**；相关定向测试 30 passed，覆盖待审阅记录取消/归档/替换、悬挂任务清理、建议绑定、低分/问题保留、伪造 ID、过期建议、人工确认与批准审计。浏览器使用隔离数据库验证辅助填写、未自动勾选确认、结束旧记录并新建；截图及使用说明见 `docs/STUDIO.md`。
- **真实模型已验证 / 人工已批准**：本轮均无新增。浏览器采用离线预设，未读取或调用真实账户密钥，未修改原真实小说数据库。

---

### 2026-10-30 · Token 预算设置与精简重试

- **代码已实现**：新建任务支持预算预设/自定义每章 tokens、任务 tokens、每章请求上限及单次输出 tokens；现有记录可暂停后更新总预算，保留 ledger 与原生成签名，恢复复用已完成 checkpoint。预算面板展示已知实际、未知预留与请求数；错误指明具体超限项和下一次预留。
- **代码已实现**：压缩 JSON/schema 格式但保留描述与约束；结构化 repair 仅传原任务与最近失败，全部 raw outputs 留在审计；空内容且 `finish_reason=length` 时不重复相同额度请求。没有删除证据、降低 Validator 严格度或静默修改思考参数。
- **诊断证据**：用户这条 blocked Chapter 1 的 ledger 为 13 次、108,042 tokens、未知 usage 0，默认章上限 150k；被下一次保守预留拦截，而非达到 30 次。trace 中 ScenePlanner 四次（两次 length），Continuity 两次 length，后者输出 8,192 中 reasoning 7,957；此轮只读历史记录，未修改用户 ledger/run/数据库。
- **离线测量**：ChapterPlan schema 8,624→7,969 字符，ScenePlanBatch 3,308→3,037；固定三次 repair 的第三次输入 1,653→452 字符。这是格式/样例输入压缩，不是实际模型 token/费用降幅。
- **离线已验证**：全量 `pytest -m 'not integration'` **355 passed, 2 deselected**。浏览器隔离服务模拟不足→调额→恢复至待审阅，Director cached、历史用量保留；截图 `docs/studio-token-budget.jpg`、`docs/studio-budget-recovery.jpg`。
- **真实模型已验证 / 人工已批准**：本轮无新增。没有调用外部收费模型、读取真实密钥、自动提高用户任务预算或恢复其收费请求；真实节省比例与创作质量待后续试验。

### 2026-10-30 · 用户授权 SCNet 自主续跑与故障修复

- **代码已实现**：`recheck` + `scripts/studio_continue.py`，可在原稿/修订稿上另存运行参数恢复候选，保留规划、正文与生成元数据；显式角色模型切换进入配置，不静默 fallback。Provider 按探测标记支持 DeepSeek 原生 thinking 格式，但不把参数被接受当作语义生效。
- **代码已实现**：Memory 编辑支持带同场景精确证据、逐字物品名的显式物品声明，不能覆盖已有声明，不自动扩大 Extractor 白名单；新候选 hash/旧批准失效。修复 dry-run 心理旧值遗漏，并使预览/提交共用同一 before-state 投影。
- **真实模型已验证**：实际继续《择道者》5 章计划至 Chapter 1 待审阅；候选 `54d8010f3fa3`，5,253 字符/5 场景/32 operations；Continuity valid、Memory valid（0 error/0 warning）、dry-run applicable；模型文学评分 4/4/4/4/5。Flash 原稿不重生成，scene 4/5 小范围修订；GLM-5.2 承担后续检查/记忆/评审，本地按证据编辑记忆后验收，不宣称纯 Flash 自动成功。
- **调用实测**：两种思考开关被 Flash 接受但仍产生思考，16k 输出仍不稳定；停止盲目增额后明确换检查角色。Auto 当时未列出，不调用或宣称免费。本轮新增 11 请求/143,405 tokens；累计 25 请求/265,308 tokens，无未知 usage；本章限 300k/50，任务限 2M。
- **离线已验证**：最终全量 360 passed、2 deselected，包含预览/提交投影一致性、幂等、回滚与新候选恢复。所有中间失败和修订保留。[完整实际报告](docs/SCNET_CONTINUATION_20261030.md)。
- **人工已批准**：无。Chapter/Commit/Snapshot/Event 各 0，before/after state hash 不变；不代用户 approve/finalize。Chapter 2–5 等当前候选人工批准，真实三章/五章/十章闭环仍未完成。

## 2026-10-31 隔离四章实测补充

- **代码已实现**：自动测试批准类型与正式提交隔离、完整审阅 before-state、事件临时地点解析、未知人物状态字段/位置拒绝、新人物合法境界落库、结构化失败诊断。
- **真实模型已验证**：DeepSeek 官方 Flash 完成第 2—5 章，19,600 字符；测试副本含种子共 5 Chapter/Commit/Snapshot，验证、预演、重复提交、物品归属、灰雾知识隔离和 state hash 审计通过。
- **Agent 干预**：存在定向正文/记忆编辑及重放，旧失败实验保留；不标为一键无人干预或十章验收。
- **离线已验证**：最终完整回归 366 passed / 2 deselected。
- **人工已批准**：无；正式源库仍为 0 Chapter/Commit/Snapshot/Event，主候选仍待审阅。
- [具体结果、预算与剩余阻塞](docs/ISOLATED_FOUR_CHAPTERS_20261031.md)。

## 2026-11-02 · 来源研究与开书工作流

- **提交故障修复**：真实候选将“封条已割、棺盖启封”写入物品状态枚举，旧验证与 dry-run 放过，提交时 ValueError；事务回滚，0 新 Chapter/Commit/Snapshot/Event。补充状态枚举校验、dry-run 一致性与抽取提示；兼容读取旧候选。
- **真实候选恢复**：只将 `op_017` 替换为引用原文的启封事实，正文与其他 19 条操作不变，另存新候选，旧审批因 hash 变化失效。以该候选在数据库副本中验证提交成功、重复提交零新增、状态 hash 不变；正式数据库仍待作者重新确认。完整离线测试 386 passed / 2 deselected。

- **连续性修复**：真实章节出现 LLM 报告三条 error 均未带 scene_id，导致 repair_history 为空。为检查器输入提供场景 ID，服务端仅按全文唯一的精确引用恢复场景；未分类问题的证据在传给修复 Writer 前遮蔽。对真实失败报告离线回放，3 条阻断问题均定位到正确场景。
- **真实候选恢复**：《旧友皆敌：我出关后，故人皆成反派》第 1 章只定向修改第 5 场 3 处，未重跑 Director 或 Writer；重检后 continuity valid、memory valid、dry-run applicable，仍待人工审阅。复检与记忆、文学评审新增 3 次 SCNet 请求、72,446 tokens；未提交章节。
- **离线验证**：连续性、保密、工作流及 Studio 恢复相关测试 44 项通过；人工抽查原报告及新候选。

- **真实 SCNet 新增验证**：Flash 市场研究成功，故事架构在 Flash 与 GLM 非流式各超时一次、GLM 流式长推理一次触发 240 秒总截止；GLM 在实测接受的非思考参数下完成架构及 2,492 字第一章。记忆抽取与重试仍有跨场景引用，人工定向修订后结构 valid/applicable；文学评审发现重复、提前揭露，剧情推进评分 2。隔离自动测试初始化，0 章节正式提交；关联调用部分 usage 未知。[真实报告](docs/SCNET_BOOK_LAUNCH_REAL_20261102.md)。
- **代码/离线验证**：增加绑定原研究 hash 的换模型 fork 与不可覆盖蓝图修订；最终全量 **378 passed, 2 deselected**。

- **代码已实现**：公开番茄小样本工具、JSON/CSV/历史快照导入、MarketResearchAgent 与 StoryArchitectAgent、选题/设定 hash 确认、事务新书初始化、十章方向交给现有 Studio；`/book-launch` 页面与 `scripts/book_launch.py` CLI。
- **代码已实现**：统一 AgentLLMClient / deadline / structured repair / provider / trace / budget；输入与 prompt 绑定的 checkpoint；取消与关闭 await；预算调额保留 ledger；规划不写 Canonical，秘密与公开前提分离，重复开书不新增。
- **离线已验证**：新增 10 项；最终全量 **376 passed, 2 deselected**（含离线 soak）。浏览器隔离 demo 从市场分析到第一章 valid/applicable 待审阅，服务重启后恢复，链接直达正确章节记录；主库未修改。
- **真实来源已验证**：最终 CLI 采集两页公开榜单、20 条元数据；起点仍使用人工导入，不声称自动采集。
- **真实模型已验证 / 人工已批准**：新增规划流程本轮均无；待用实际模型验证选题差异化、设定质量与计划落实。此前章节模型实测保持原报告口径。
- **复用边界**：novel-agent 的分阶段机制；fanqie-radar 字体解码代码保留 MIT 许可；不移植外部记忆或提交系统，不增加新依赖。详见 [用法和验收](docs/BOOK_LAUNCH.md)。

## Phase 10 — 未来增强（MVP 之后）

- Character autonomous simulation
- background faction simulation
- retcon impact analyzer
- branch timelines
- volume/arc auto planning
- pacing controller
- economy simulation
- map/travel-time simulation
- multi-POV
- style fine-tuning / author profile
- prompt/evaluation dashboard
