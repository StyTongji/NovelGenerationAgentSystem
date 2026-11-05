# Novel Narrative Engine

长篇玄幻小说自动生成与管理系统。核心原则：**数据库中的 Canonical Story State 是世界事实的唯一真相源，小说正文只是叙述层**。

## 当前进度

2026-11-02：修复 `item_update.status_change` 接受自由文本导致“预演通过但正式提交失败”的问题；校验与 dry-run 现在只接受真实物品状态枚举，`consumed` 预演与提交统一为 `lost`。实际失败事务已回滚；针对该章另存的启封事实候选在数据库副本中通过事务与幂等验证，仍需作者重新审阅并确认。最终离线回归 386 passed / 2 deselected。

2026-11-02：修复连续性检查器未返回场景 ID 时无法启动定向修复的问题。检查 prompt 现在包含场景边界；服务端仅在正文精确引用全章唯一时定位场景，无法确认的阻断问题仍要求人工处理。未分类问题在修复提示中会遮蔽被指出的片段，避免泄露可能的秘密。真实 SCNet 阻塞候选已另存局部修订版并通过连续性、记忆及 dry-run，仍等待作者语义审阅和批准。

2026-11-02 真实 SCNet 复测：Flash 生成市场分析；GLM-5.3-Flash 在测试过的流式与 `enable_thinking=false` 配置下完成设定和 2,492 字第一章。原始设定与记忆需要定向修订，文学评审发现重复和提前揭露，当前仅为隔离测试预览，未正式提交；实际总用量因超时请求未知。[测试结果与正文](docs/SCNET_BOOK_LAUNCH_REAL_20261102.md)。

2026-11-02：主项目新增“选题研究与 Agent 开书”，入口 `/book-launch`：公开番茄小样本或起点/其他来源文件导入 → 有引用的市场分析与候选 → 人工选择 → 世界/人物/分卷/开篇十章 → 人工确认事务开书 → 现有 LangGraph 逐章生成与审阅。复用 fanqie-radar 的小型 MIT 字体解码工具，保留许可；无需运行参考仓库。新增 10 项离线测试通过，全量 **376 passed / 2 deselected**；隔离浏览器流程与真实公开 20 条来源采集通过，新增规划 Agent 尚未调用收费模型，未批准任何正式小说。[启动、测试与 CLI 用法](docs/BOOK_LAUNCH.md)。

2026-10-31：已用 DeepSeek 官方 `deepseek-flash` 完成《择道者》第 2—5 章隔离续写（19,600 字符），Agent 测试审阅后推进状态，**非人工批准、主库不变**。修复了审阅上下文缺项、临时地点映射、未知境界字段静默忽略等实际故障；最终离线回归 366 passed / 2 deselected。包含定向编辑和新库重放，不能算无人干预十章验收。[阅读路径、成本与审计报告](docs/ISOLATED_FOUR_CHAPTERS_20261031.md)。

2026-10-30 状态：新增「叙事工坊」前端、工作流与世界状态可视化，以及真实执行的 LangGraph 章节工作流。支持创建故事、按角色选模型、连续安排 1–10 章、逐章审阅提交、局部修订、记忆修正、停止和恢复。用户本次明确要求前端和可视化，替代此前暂停 UI 的约束；没有扩展外部 literary skills。使用说明见 [Studio 指南](docs/STUDIO.md)。

本轮实现与离线演示不等于真实模型十章验收。已有真实原稿 `stream32_flash_1/2/3` 的 Memory valid、dry-run applicable（23/21/15 operations）属于 Flash-1017 同稿结构验收，仍不代表语义批准。首次真实 Canonical 提交、真实三章和十章闭环尚未完成；历史失败记录见 [10-22 报告](docs/REAL_CLOSURE_20261022.md)。

- [x] **Phase 0 — 仓库骨架**：`app/` 包结构（models / repositories / services / api / schemas / agents / llm / retrieval 预留）、配置系统（env + 可选 config.yaml）、FastAPI `/api/v1/health`、pytest 就绪
- [x] **Phase 1 — Canonical Story State**：22 张表的 SQLAlchemy 2.x 数据模型 + Alembic 迁移 + Repository / Service 分层 + 基础 API + 19 个测试
- [x] **Phase 2 — LLM Runtime / Provider Abstraction**：统一 Provider 抽象、强类型 DTO、结构化输出校验与有界修复循环、FakeLLMProvider、Provider Registry、Prompt Registry、agent_outputs Pydantic 模型（integration 测试 opt-in）
- [x] **Phase 3 — Story Context Builder / Memory Retrieval**：retrieval 层（实体链接 + 事件检索 + 混合评分 + NoOp 向量协议）、KnowledgeFilter 信息防火墙、DirectorContext/WriterContext 双权限 DTO、ContextBudget、debug 报告
- [x] **Phase 4 — Story Director / Chapter Planning**：ChapterPlan v2 契约（reveal policy、ID-first 实体引用、proposed entities、director_notes 分层）、ChapterPlanValidator（A–I + thread/debt/arc 校验）、DirectorAgent（分区 prompt 组装 + 有界 domain repair）、preview/validate API
- [x] **Phase 5 — Scene Planner + WriterAgent + Chapter Draft Pipeline**：ScenePlan 契约（per-scene POV + temporary_ref）、ScenePlanValidator、ChapterWorkingMemory（draft-local）、WriterAgent（writer_v2 分区 prompt + output guard）、ChapterDraft preview（逐 scene 流水线，DB 零写入）、per-scene 知识防火墙、prompt capture 测试
- [x] **Phase 6 — Hybrid Continuity Checker + Targeted Scene Repair**：Writer-safe restriction 结构化修复、DeterministicContinuityChecker（13 类规则）、LLMContinuityChecker（语义层 + hallucination 防护）、Issue Merger、Writer-safe Repair Projection、targeted scene repair（有界 ≤2 轮 + re-check）、continuity API
- [x] **Phase 7 — Evidence-Grounded Memory Extraction + Validation + Dry Run**：MemoryUpdate v2 契约（统一 envelope + EvidenceSpan + MemoryVisibility + candidate summaries）、Evidence 验证（逐字存在/歧义/重定位）、MemoryUpdateValidator（12 层）、operation fingerprint 幂等、纯域 dry-run、repair redaction
- [x] **Phase 8.6 — Real-LLM 3-Chapter Authoring Trial（基础设施就绪）**：`scripts/real_llm_trial.py`（init/preview/finalize/state/report，只调生产 service）、trial 世界模板、review 模板自动生成、artifact 导出；真实运行需 `NOVEL_LLM_API_KEY`/`NOVEL_LLM_BASE_URL` 环境变量
- [~] **Phase 8.7 — Real Memory Closure**：真实失败回放、EvidenceCatalog、FactRef、Memory Extractor v3.2、deadline；Flash-1017 同稿三次结构验收通过，待语义审阅与首次真实 finalize
- [x] **Phase 8.5 — Long-Horizon Committed Story Soak Audit**：10 章 deterministic committed soak（含 duplicate finalize / stale parallel preview / rollback 注入）、LongHorizonStoryAuditor、Fix A–D 加固（canonical 引用校验、preview_package_hash 强制、summary source_draft_hash 绑定、commit 审计 continuity_report_json）
- [x] **Phase 9 — Local Authoring Studio（代码实现）**：本地前端、七角色说明、实时事件与工作流图、世界状态查看、逐章审阅、修订/恢复与导出；live 实际质量验收待完成
- [ ] Phase 10 — 高级特性（角色模拟 / Retcon / 分支时间线）

测试：2026-10-30 最新全量 `pytest -m 'not integration'` **360 passed, 2 deselected**，包含离线十章 soak、自定义服务、运行参数恢复、显式物品声明、预演/提交投影一致性和辅助审阅回归。默认 `pytest` 排除 soak/integration。真实模型试验单独记录，不把 fake soak 或 Studio demo 视为真实十章验收。此前工作台验收详见[记录](docs/STUDIO_VALIDATION_20261030.md)。

完成状态：**代码已实现**（LangGraph、七角色、Studio、v3.2、预算与 checkpoint）；**离线已验证**（360 项全量回归、隔离数据库浏览器交互）；**真实模型已验证**（历史 Flash-1017 同稿三次结构验收；新增 SCNet《择道者》Chapter 1，Flash 正文、GLM-5.2 检查/评审、本地记忆修订后 valid/applicable）；**人工已批准**（真实章节无）。P2/P3 真实创作验收保持未完成。见[实际续跑报告](docs/SCNET_CONTINUATION_20261030.md)。

## 前端与连续章节创作

在项目环境安装依赖后，使用单 worker 启动：

```bash
pip install -e ".[dev]"
uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

打开 [叙事工坊](http://127.0.0.1:8000/studio)，不要直接打开 `file://.../studio.html`。主页 `/` 同样进入工坊，旧试验台保留在 `/trial`。

- **离线演示**：无需 API key，使用明确标记的脚本化短篇素材，每章约 500–800 字，走实际规划/写作接口、校验、审阅与事务提交。目标字数不能把 demo 变成长篇生成，不代表模型写作质量。
- **真实模型**：已内置「DeepSeek 官方」和「SCNet 超算互联网」。在服务端环境或 `.env` 分别设置 `NOVEL_DEEPSEEK_API_KEY`、`NOVEL_LLM_API_KEY`，重启后在页面选服务与模型；`config.yaml` 可覆盖地址/默认模型并增加其他服务。`Auto` 仅作为 SCNet 的显式候选，不作失败 fallback。
- **自定义 API**：真实模型的服务选项中选择「自定义」，直接填写 Base URL、API Key 和模型名称，支持 OpenAI-compatible Chat Completions。密钥按任务独立保存在进程内存，不写入运行文件或浏览器存储；服务重启后会提示重新输入。
- **逐章推进**：每章生成后停在审阅。作者核对正文、证据和每条 memory operation 后批准；当前章成功提交，才启动下一章。不是无人值守批量生成。
- **辅助填写**：审阅页可点击「让 Agent 填写」，生成逐条记忆判断、漏提项和五项评分，可以修改；最终确认仍由作者勾选，模型不能自动提交。demo 建议明确标为离线预设。
- **三个视图**：正文与审阅、Agent 协作与事件时间线、已提交故事世界。支持分场景编辑、记忆 JSON 修正和正文导出。
- **运行恢复**：保存完整阶段与场景 checkpoint；停止、服务重启或最后一个运行监看连接断开后，可显式恢复。已提交章节不会因停止被撤回。
- **结束记录与新建**：点击「结束此记录」保留正文并归档；也可在新建窗口勾选「结束旧记录并新建」。新记录从最后已提交章节继续，不再被旧审阅记录阻塞；另写新世界请选择「创建新故事」。
- **Token 上限**：新建时可选每章/任务总 token、每章请求次数和单次最大输出。已有记录可暂停后「调整预算 → 保存 → 恢复创作」，保留用量和已完成阶段。页面区分已知实际 token 与未知预留，预算不足不是 API 余额不足。说明见 [Studio 指南](docs/STUDIO.md)。

Studio 使用当前 App 配置的数据库（默认 `novel_engine.db`），产物写入 `studio_runs/`。历史 `real_trial_runs/<run>/trial.db` 是独立试验数据库，工坊不会自动导入或修改它。当前产品按本机单用户设计，没有鉴权，不应直接对公网开放。完整配置、API 与产物路径见 [Studio 指南](docs/STUDIO.md)。

## 作者审阅与修改（CLI 同样保留）

先生成 preview，再使用以下命令。`inspect` 和 `memory-edit` 不需要密钥，也不调用 API；`revise` 需要密钥，会运行 hybrid Continuity 和 MemoryExtractor，但不会重跑 Director、ScenePlanner、Writer。

```bash
# 生成可读报告、分场景可编辑副本、Memory Batch 可编辑副本
python scripts/real_llm_trial.py inspect --run <run> --chapter 1 --attempt <source>

# 编辑 reading/scenes/scene_01.txt；scene ID 从 reading/scenes/index.json 获取
python scripts/real_llm_trial.py revise --run <run> --chapter 1 \
  --source-attempt <source> --attempt revision_1 --scene-id ch1_s1 \
  --text-file real_trial_runs/<run>/chapter_01/attempts/<source>/reading/scenes/scene_01.txt \
  --capabilities real_trial_runs/<probe>/capabilities.json --memory-max-tokens 16384

# 如果只需修正抽取：编辑 reading/memory_batch.json，然后本地重验
python scripts/real_llm_trial.py memory-edit --run <run> --chapter 1 \
  --source-attempt <source> --attempt memory_corrected_1 \
  --batch-file real_trial_runs/<run>/chapter_01/attempts/<source>/reading/memory_batch.json

# 中断后恢复同一尝试；使用已保存的修订输入，不重新读取编辑中的文件
python scripts/real_llm_trial.py resume --run <run> --chapter 1 --attempt revision_1
```

每个新候选均保留 `reading/review.md`，展示操作内容、原文证据和验证问题；`review.json` 默认未批准，不能把读取报告当作批准。导出的阅读副本可能包含作者层秘密，不适合直接发布。重新 inspect 不覆盖你编辑中的副本。

局部正文修订保留原规划及其余场景；Continuity 有阻塞问题时保存 `stage_draft.json` / `stage_continuity.json` 并停止，不自动改写作者正文，也不继续 Memory。修正文本后从原 source 创建另一个新 attempt。当前修订入口要求 source 有完整 `preview_package.json`；尚未写完的初次生成应先 resume。

Memory 编辑仍使用 v3 的 EvidenceCatalog ID 和 FactRef，resolved quotes/offsets 由服务器重建；无效结果可保存供查看，但不能 finalize。每次正文或记忆修改都需要新的人工确认。所有新尝试沿用 run 原有预算台账，不能靠修改或重启重置用量。

## Real-LLM Trial（Phase 8.6）

```bash
export NOVEL_LLM_API_KEY=...          # 仅从环境变量读取，绝不入库/入 Git
export NOVEL_LLM_BASE_URL=https://api.scnet.cn/api/llm/v1

python scripts/scnet_probe.py --output real_trial_runs/<unique_probe_dir>
python scripts/real_llm_trial.py init --model DeepSeek-V4.1-Flash-1017
python scripts/real_llm_trial.py preview --run <run_id> --attempt initial --capabilities real_trial_runs/<probe_dir>/capabilities.json
python scripts/real_llm_trial.py memory-retry --run <run_id> --chapter 1 --memory-model DeepSeek-V4.1-Flash-1017 --attempt flash_1 --capabilities real_trial_runs/<probe_dir>/capabilities.json
python scripts/real_llm_trial.py resume --run <run_id> --chapter 1 --attempt flash_1
python scripts/trial_comparison.py --run <run_id>
# 人工逐项审阅 attempts/<attempt>/review.json；无效候选不得批准。
python scripts/real_llm_trial.py finalize --run <run_id> --chapter 1 --attempt <reviewed_attempt> --approval <reviewed_receipt.json>
python scripts/real_llm_trial.py audit --run <run_id>
# 首次提交、立即重复提交审计通过后，才生成下一章。
```

每次产物保存在 `real_trial_runs/<run>/chapter_01/attempts/<attempt>/`，含正文、memory preview、validation、dry-run、review 模板和带 hash 的阶段 checkpoint。原历史 preview 不覆盖。失败也保存；恢复时 state/input/prompt/code/model/capability 变化会拒绝复用。`review.json` 默认未批准，不应自动填成通过。

运行级 `llm_calls.jsonl` 记录每次 HTTP 状态、耗时、模型、prompt 版本、request ID、usage；`usage_ledger.json` 在请求前预留预算。默认每章 30 次 HTTP/150k tokens、run 2M；未知 usage 保留预留，不计为零。Auto 不作 fallback，未披露的响应模型不猜测。能力探测只证明已测试参数，不证明大上下文质量。

## 快速开始

```bash
# 1. 环境（conda 或 venv 均可，Python 3.12）
conda create -n novel-engine python=3.12 -y
conda activate novel-engine
pip install -e ".[dev]"

# 2. 配置
cp .env.example .env          # 按需修改数据库 URL / LLM key
cp config.example.yaml config.yaml  # App 模型配置；环境变量用 NOVEL_LLM__... 双下划线覆盖

# 3. 数据库迁移（默认 SQLite）
alembic upgrade head

# 4. 启动
uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1

# 5. 测试（不依赖任何真实 LLM / 网络）
pytest
```

## 架构要点（已落地部分）

### LangGraph 与七个 Agent 角色

`ChapterGenerationService.generate_preview()` 实际运行 `StateGraph`：Director → Draft（ScenePlanner 与逐场景 Writer）→ Continuity → 有问题时定向 Repair → Memory → Package。Repair 内沿用有界修复与复查，未清除阻塞问题就停止；作者修订直接进入检查阶段。

七个 LLM 角色是 Director、ScenePlanner、Writer、ContinuityChecker、WriterRepair、MemoryExtractor、LiteraryReviewer。一个角色可多次调用，同一模型可服务多个角色；工作流编排器、Validator、ContextBuilder 和事务提交都是确定性程序，不计入 LLM Agent 数量。

LangGraph 负责执行节点和分支，`TrialAttempt` 负责带输入/hash 的阶段与场景 checkpoint，没有额外的 LangGraph checkpoint 数据库。Studio 在 live 模式候选生成后调用文学评审，demo 不调用该角色；五项评分与场景原文建议只是辅助作者判断，不替代 Continuity、Memory 校验或人工批准。

### LLM Runtime（Phase 2）

- **统一 Provider 抽象**：Agent 只依赖 `BaseLLMProvider`（`generate_text` / `generate_structured`），业务代码零厂商 SDK 依赖。已实现 `OpenAICompatibleProvider`（httpx 直连，兼容 OpenAI / DeepSeek / Qwen / vLLM）与 `FakeLLMProvider`。
- **结构化输出不是"解析成功即可"**：raw output → JSON 提取（容忍 code fence / 前后噪声）→ Pydantic 校验 → 带错误反馈的有界 repair 循环（`max_repair_attempts`，默认 2，绝不无限重试）→ 校验通过的模型实例；每次 raw output 全部保留在 `StructuredResult.raw_outputs`。
- **强类型 DTO**：`TextRequest` / `StructuredRequest` / `TextResult` / `StructuredResult` / `GenerationMetadata`（provider、model、agent、prompt_version、temperature、latency、token usage、finish_reason、attempts、success/error）——Phase 4/5 章节表可直接落 provenance。
- **密钥安全**：内置服务从环境变量/`.env` 读取；Studio 自定义服务接受本次输入的运行时密钥，按任务隔离且不落盘。异常与日志经 `scrub_secrets` 脱敏，有测试断言 key 不进入输出或产物。
- **Prompt Registry**：`prompts/*.md` 按文件名自动发现与版本化（`writer.md`→v1，`writer_v2.md`→v2，最高版本默认生效），支持 `{{variable}}` 渲染；prompt 一律不写死在 Python。
- **Agent 输出契约**：`app/schemas/agent_outputs.py` 中的 `ChapterPlan` / `MemoryUpdateBatch` / `ContinuityReport` 与 `schemas/*.json` 三份 JSON Schema 双向兼容（jsonschema 测试验证）。
- **边界**：`app/llm/` 不 import 任何数据库 / 领域模块——Story State、Knowledge、Event 检索一律属于 Phase 3 Context Builder。

### Context / Retrieval（Phase 3）

- **分层边界**：`repositories → retrieval → ContextBuilder → agent-safe DTO → agents`。Agent 永远不直接查 Repository；`app/context/`、`app/retrieval/` 永远不调用 LLM。
- **无万能 StoryContext**：`DirectorContext`（规划视图，可含 thread truth 与 planning secrets）、`WriterContext`（信息安全视图，结构上不存在 truth / author summary / hidden_description 字段）、`ContinuityContext` / `MemoryExtractionContext`（Phase 6/7 预留 DTO）。
- **Knowledge Firewall 位于最终装配边界**：所有进入 WriterContext 的文本（StoryFact、Event summary、PlotThread public_summary、Foreshadowing hint）统一经过 `KnowledgeFilter`——POV 未知的 secret 语句被掩码替换、被标记 `secret_fact_ids` 的事件整体剔除；ChapterSummary 等 author-level summary 整体不进入 WriterContext。suspected 只以"怀疑（未确认）"框架呈现，believed_false 以角色错误认知框架呈现，forgotten 默认排除。
- **实体优先检索**：`EntityLinker` 先按名称/别名精确匹配（林凡 / 轮回古镜…），再由 `HybridRetriever` 混合评分：`0.50*semantic(bigram 词法) + 0.25*recency(1/(1+distance/scale)) + 0.25*importance`，权重可配置；事件检索在 SQL 层缩小候选集；确定性 tie-breaking（分数降序 → type → id）。
- **Context Budget**：事件/线索/物品/摘要数量与字符数上限；裁剪优先级保证 hard constraint 永不因预算被裁。
- **Debug**：`builder.debug` 记录每条信息的 include/exclude 及原因，用于排查"Writer 为什么知道这个秘密"。

### Scene Planning / Draft（Phase 5）

- **双重防火墙**：`ChapterPlan → WriterSafePlan（drop director_notes + forbidden_information→restriction）→ ScenePlanner（只看 safe plan）→ WriterSafeScenePlan → WriterContext`。Writer 收到的任何内容都源自 writer-safe 投影。
- **per-scene 知识防火墙**：每个 scene 独立调用 `build_writer_context(pov=…)`，Firewall 按 POV 重新计算（multi-POV 测试：林凡→魔教长老→林凡，secret 只在中间 scene 的最终 prompt 出现）。
- **raw user_direction 结构性禁入**：`build_writer_context` 无该参数，`WriterContext` 无该字段；Writer 只获得 `writer_instruction` / `chapter_guidance`（来自 safe plan，且 must_not_reveal 语句经 POV 防火墙 scrub）。
- **ChapterWorkingMemory ≠ Canonical State**：draft-local 临时连续性（已完成 scene、上一场景结尾截断、planned outcomes、temporary entity 登记）；丢弃 draft 即全部消失，DB 不变。
- **temporary_ref 跨 scene 一致**：`EphemeralEntityRegistry`（WorkingMemory.temporary_entities）保持同一 ref/描述，不产生 UUID、不写库，canonicalize 留给 Phase 7/8。
- **previous chapter ending 过防火墙**：other-POV 上一章结尾进入当前 POV 前经 KnowledgeFilter scrub（reader 已知 ≠ 角色已知）。
- **Draft = deterministic merge**：scene 文本直接拼接，无二次 LLM 重写；`status="preview"`；失败抛 `ChapterDraftGenerationError`（携带已完成 scene，DB 仍不变）；`StalePlanError` 基于 context_hash。

### Canonical Commit（Phase 8）

- **唯一 mutation 入口**：`CanonicalCommitService.commit_chapter`；Agent/API route/Extractor 均不得写库。
- **finalize = 确定性事务，零 LLM**：用户批准的是这份 draft，不是让模型再发挥一次。
- **提交前全链重验**（不信任客户端 valid 字段）：story_state_hash（staleness）、continuity/memory draft_hash、chapter_plan_hash、deterministic continuity recheck、MemoryUpdateValidator 重验、**fresh dry-run**（client dry-run before_hash 不匹配当前状态 → 拒绝）。
- **幂等**：`(novel_id, draft_hash)` 唯一约束 + ChapterCommit 记录；重复提交返回已有 result（duplicate=true），零新状态。
- **单事务**：canonicalize entities → facts → events → knowledge → items → character states（同章 collapse 为一条 chapter-end row，过程进 Event Log）→ relationships → threads → foreshadowing → debts → chapter（版本化，旧 canonical → superseded）→ summaries（author/reader 分列）→ novel.current_chapter_number → snapshot（mutation 之后）→ commit record。任一步异常整体 rollback（深度指纹测试证明）。
- **canonicalization**：proposed entity 在 commit 时获得真实 UUID 并返回 `temporary_entity_mapping`；同名实体去重（第二章再次 propose 同一人 → 映射到已有 UUID）。
- **Event provenance**：author summary 为 canonical log，reader_safe_summary + 结构化 visibility 存于 `extra_metadata`（Phase 3 防火墙可 filter-before-serialization）。
- **API**：`POST /chapters/generate-preview`（返回完整 preview package，不 commit）与 `POST /chapters/finalize` 分离；错误映射 409（stale/duplicate/collision）/422（validation）。

### Memory Extraction（Phase 7）

- **Phase 6 Fix A（redaction）**：leak 类 issue 的 evidence 在进入 repair prompt 前句级 redact 为 `<REDACTED_PROTECTED_INFORMATION>`（span 不可定位时逐句扫保护语句、最后整场景 redact）——错误正文中的秘密不再重新进入新 prompt。
- **Phase 6 Fix B（merge key）**：category+scene+primary entity+normalized evidence；LLM 无稳定 ref 时用 evidence overlap（包含或 ≥8 字共享窗口）保守去重——宁可保留两条真实 issue。
- **MemoryUpdate v2**：统一 envelope（operation_id / update_type / source_scene_id / evidence / confidence / planned / unplanned_change）+ 10 类 payload（create_event … propose_entity）；`confidence` 仅供 UI，不跳过验证。
- **Evidence 规则**：exact_quote 必须逐字存在于对应 SceneDraft；plan/canonical 文本天然不是 draft 文本 → 伪造证据自动失效；position 不可信（重定位 + warning）；歧义记录全部候选偏移。
- **Validator 12 层**：schema / draft binding / evidence / 实体引用 / cross-novel critical / old-state precondition / 域转换（realm 复用 PowerSystemDomain、owner 前置）/ 知识与世界事实分离（existing fact 不得重复创建、suspicion 不得升 known、believed_false 无 payload 可改 truth）/ plot 生命周期（premature resolve 拒绝）/ fingerprint 去重 / 计划-揭示授权 / temporary ref 声明。
- **Dry-run = 纯域模拟**：before/after 状态投影 + 哈希 + per-operation 结果 + requires_confirmation（死亡/伤势等高风险）；零事务、零 savepoint、零 DB 写入。
- **候选 summaries**：author/reader 分离；reader summary 含 canonical secret → invalid；均不持久化。
- **Gate**：continuity report `valid=false` 或 `draft_hash` 不匹配 → 拒绝 extraction（零 LLM 调用）。无 domain repair（宁可失败让用户重跑）。

### Continuity（Phase 6）

- **双层权限**：Checker 是 author-level verifier（可见 canonical truth / secrets / knowledge rows / thread truth）；Repair Writer 只接收 truth-free 的 `ContinuityRepairInstruction`（`author_explanation` 在类型上不可达）。
- **规则优先**：`DeterministicContinuityChecker` 13 类规则（死亡/物品/功法/境界/突破/硬约束/知识泄漏/揭示政策/线索状态/临时实体一致性/时间/空间/场景连续性）；LLM 语义检查只补规则层不足（改写泄密、人设漂移、plan adherence），**永不覆盖或降级 deterministic finding**。
- **Issue Merger**：按 (category, scene_id) 去重合并，severity 取 max，evidence 合并，explanation 保留 deterministic 版本。
- **LLM 输出不可信**：hallucinated scene_id 丢弃、未知 category 归为 other、evidence 不在 draft 中则清空。
- **Draft 绑定**：`ContinuityReport.draft_hash` = 正文 sha256；draft 修改后旧 report 失效（Phase 8 commit 时校验）。
- **Targeted Repair**：只修 blocking（error/critical）且只修受影响 scene；repair prompt 中 secret 只能出现在待修复原文数据块内，指令层全部为 truth-free 模板；每轮 repair 后 re-check；两轮后仍有 blocking → `exhausted=true` 返回（不 commit）。全程 DB 零写入。
- **Prompt Injection 防护**：draft 以 `<<<DRAFT ... DRAFT>>>` 数据块进入 checker prompt。

### Director / Planning（Phase 4）

- **ChapterPlan 本身是防火墙的一部分**：`ChapterPlan.to_writer_safe()` 投影 `WriterSafePlan`，`director_notes`（战略理由、secret_context）结构上不存在于 Writer 视图。
- **Reveal Policy**：`revelations[].policy ∈ hint_only | partial | full`，每条带 allowed/forbidden_information；`prohibited_revelations` + `must_not_reveal` 在 plan 文本中自我检查。
- **ID-first + proposed_entities**：已有实体引用必须用真实 ID（validator 检查 hallucinated UUID）；新实体用 `temporary_ref`，真正 canonicalize 留给 commit/Memory Extractor。
- **两层 repair 分离**：LLM structured repair（JSON/schema，Phase 2）≠ Domain Plan Repair（剧情逻辑非法 → issues 反馈给 Director 重新规划，`max_plan_repairs=2` 有界）。
- **Validator 检查**：角色存在性/死亡(narrative_mode 豁免)/地点/物品归属/功法归属/境界跳跃(PowerSystemDomain)/hard constraint(死亡关键词)/reveal policy/角色知识/线索状态/逾期债务 warning/推进缺失 warning。
- **Prompt Injection 边界**：user_direction 以 `<<<USER_DIRECTION ... >>>` 数据块进入 user 消息，绝不拼接进 system prompt。
- **Preview only**：plan 生成不产生任何 canonical 写入（测试断言 Chapter/Event/Summary/Snapshot 计数不变），并携带 `context_hash` 供 Phase 8 commit 时做 staleness 验证。

### Canonical State（Phase 0/1）

- **Canonical State / Event Log 双层**：当前状态存于版本化表（如 `character_states` 按 `chapter_number` 版本化、不可变），历史因果存于 `events` + `event_effects`（append-only）。
- **Character Knowledge Isolation**：可被隐藏的事实一律表达为 `story_facts`（带 `secrecy` 等级）；角色认知是独立表 `character_knowledge`（known / suspected / believed_false / forgotten）。`StoryStateService.visible_facts_for(pov)` 返回 POV 角色可见事实，Writer（Phase 5 起）只能拿到这里授权的信息。
- **Plot 结构独立实体**：`plot_threads`（含 truth 与 public_summary 分离）、`foreshadowings`（planned→planted→reinforced→paid_off 生命周期）、`plot_debts`（含 due_chapter / due_story_time）、`constraints`（hard/soft，支持章节生效区间）。
- **版本不可变**：章节采用 `(novel_id, number, version)` 唯一约束，重写生成新版本并置旧版为 superseded；状态行不允许同章覆盖。
- **PostgreSQL 兼容**：UUID 主键（`sqlalchemy.Uuid`）、显式命名约定、VARCHAR 存储的枚举（`native_enum=False`）、JSON 列。迁移可用 `NOVEL_SYNC_DATABASE_URL` 指向 PostgreSQL。

## 目录结构

```text
app/
  api/routes/     # FastAPI 路由（story.py）
  models/         # SQLAlchemy ORM（Base / mixins / 22 张表 / 枚举）
  repositories/   # 数据访问层
  services/       # 领域服务（story_state / power）
  schemas/        # Pydantic v2 API 模型 + agent_outputs（LLM 输出契约）
  llm/            # Provider 抽象 / OpenAI-compatible / Fake / Registry / Prompt Registry
  context/        # ContextBuilder / KnowledgeFilter / budget / DTO / debug
  continuity/     # deterministic checker / merger / repair projection / redaction
  memory/         # MemoryUpdate 契约 / evidence / validator / dry-run
  commit/         # ChapterCommitRequest / Result / PreviewPackage / 错误契约
  draft/          # ChapterWorkingMemory / SceneDraft / ChapterDraft（preview-only）
  retrieval/      # 实体链接 / 事件检索 / 混合评分 / 向量协议
  agents/ vector/                   # Phase 4+ 预留
alembic/          # 迁移
prompts/ schemas/ # LLM Agent prompt 与 JSON Schema
tests/            # pytest 套件（in-memory SQLite + FakeLLM，无需网络）
```

## 文件说明（原 Agent Pack）

- `AGENTS.md`：最高优先级工程规范
- `TASKS.md`：分阶段开发路线和验收标准
- `docs/`：架构 / 数据模型 / Pipeline / API / 测试 / 评估
- `skills/`：各子任务工作方法
- `examples/bootstrap_world.json`：世界初始化示例

# Novel Engine Agent Pack

这是给 coding agent 的项目构建说明包，用来实现一个可长期写玄幻小说的 Narrative Engine。

## 推荐使用方式

把整个目录内容复制到新仓库根目录，然后对 coding agent 下达：

> 阅读 `AGENTS.md`、`TASKS.md` 和 `skills/novel-engine/SKILL.md`。从 Phase 0 开始实现，严格按阶段推进。每完成一个 Phase 运行测试并汇报验收结果。不要跳过 Canonical Story State、Character Knowledge、Event Log 或 Structured Output。未经要求不要提前实现未来增强功能。

## 文件说明

- `AGENTS.md`：最高优先级工程规范
- `TASKS.md`：分阶段开发路线和验收标准
- `docs/ARCHITECTURE.md`：总体架构
- `docs/DATA_MODEL.md`：领域模型
- `docs/PIPELINE.md`：章节生成流水线
- `docs/API_SPEC.md`：MVP API
- `docs/TEST_PLAN.md`：测试规范
- `skills/*/SKILL.md`：coding agent 在不同子任务中应遵循的工作方法
- `prompts/`：四个核心 LLM Agent 的系统 prompt
- `schemas/`：关键 structured output JSON Schema
- `examples/`：初始化世界示例


