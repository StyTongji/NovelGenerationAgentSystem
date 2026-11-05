# Real Memory Closure — 2026-10-22 实施与真实试验记录

## 结论与完成状态

安全加固已实现并经离线回归；SCNet 小样本探测已实测。原稿 Memory 闭环尚未通过，首次真实提交尚未发生。没有开发 UI、扩展 literary skill、替换 Agent 框架，也没有重跑 Director / ScenePlanner / Writer。

最终测试：`python -m pytest -q -m 'not integration'` → **283 passed, 2 deselected in 21.90s**。包含 fake 十章 soak，不含收费集成测试。此前默认全量为 280 passed，随后新增 2 个回归并纳入 1 个 soak。

| 阶段 | 代码已实现 | 离线已验证 | 真实模型已验证 | 人工已批准 |
|---|---|---|---|---|
| P0 契约、提交安全、deadline、恢复、预算 | 是 | 是 | Flash 调用和 Auto 读超时退出已观察 | 不适用 |
| P1 能力探测、memory-retry、全样本比较 | 是 | 是 | 文本/JSON 探测成功；Memory 对照未完成 | 否 |
| P1 各模型 3 次 Memory + 两固定场景文笔盲评 | 未全部完成 | 不适用 | 1 个 Flash 候选、1 次 Auto 超时 | 否 |
| P2 原稿验收、修订、首次/重复 finalize、三章闭环 | 安全提交入口就绪 | 合成样本通过 | 未通过，未提交 | 否 |
| P3 滚动规划、检索 30 例、文学评估、真实十章 | 按前置门槛暂不实施 | 仅既有 fake 十章 soak | 未开始 | 否 |

## Real Failure Regression

`tests/fixtures/real_memory_closure/chapter_01/` 保存原稿、原 batch、旧报告、plan、preview 及脱敏 `before_state.sql`。测试恢复 SQL 状态并实际调用 Validator，稳定得到 8 × `evidence_not_in_draft`、3 × `knowledge_unknown_fact`；不是读取旧报告计数。

原始 run：`real_trial_runs/ui_20260831_034841_b348a6`。
原稿 SHA256：`a76f492b7352c501e844638979c9a1159b629315ef106dd8d7ab74293ed58918`。
before/current state hash：`0df9904dfcb49ace0abefd70fac779072f5d49e5129c007c264798983c96011d`。

## EvidenceCatalog

服务器从最终 draft 重新构建 catalog，校验正文与场景拼接、唯一 scene ID、draft hash、逐字文本和 scene-local `[start_char,end_char)` 偏移。v3 必须提供非空 evidence refs；删空 refs、改成旧契约、跨场景引用、未知 ID、伪造 catalog/offsets 均不能进入正式提交。验证后服务器解析的同一 batch 用于 dry-run、commit 和 provenance。不使用 embedding/fuzzy matching。

## Fact Dependency

FactRef 强制 canonical `fact_id` / `temporary_fact_ref` 二选一。新 fact 显式使用唯一 `new_fact_N`，同批 knowledge 引用对应 temp。正式 v3 路径不再按自由文本猜测依赖。提交时先创建事实 UUID，再解析 knowledge；provenance 保存 resolved fact ID。旧契约只在显式 legacy 离线回放中开放。

## Memory Extractor v3

FactCatalog 只输入一次；正文通过有序 EvidenceCatalog 输入一次。严格 wire schema 限制每个 operation 的 payload、refs 与 FactRef。所有 Agent 经统一 AgentLLMClient；生产 finalize 传递完整 preview hash，重新做领域验证，并要求绑定 attempt/draft/memory/preview 的人工 review receipt。修改任何内容都会使旧批准失效。

批准 receipt 是本地审阅记录，不是身份认证令牌；API 并未因此获得多人身份认证能力。工具没有替用户签署批准。

## SCNet Capability Record

Endpoint：`https://api.scnet.cn/api/llm/v1`。
报告：`real_trial_runs/scnet_probe_20261022/capabilities.json`。

| requested model | 列表可见 | 小文本 | JSON object | 响应模型 |
|---|---|---|---|---|
| DeepSeek-V4.1-Flash-1017 | 是 | 成功 | 成功 | 同名 |
| Auto | 是 | 成功 | 成功 | Auto，实际模型未披露 |
| DeepSeek-V4-Flash | 否 | 未调用 | 未调用 | 不适用 |

两模型均接受 `enable_thinking=false` 参数，但 Auto 仍报告 reasoning tokens，因此只能认定参数未报错，不能断言已关闭思考。256-token 输出上限小样本成功；8192 上限仅 Flash 大请求实测。没有发送未经确认的其他思考参数，也没有静默模型 fallback。key 仅注入进程环境，未写入仓库或报告。

## Flash Result

Attempt：`chapter_01/attempts/flash1017_1/`，requested/response model 均为 DeepSeek-V4.1-Flash-1017，不冒充旧 Flash 基线。

- 20 个 operations。
- `evidence_not_in_draft=0`；`knowledge_unknown_fact=0`。
- `evidence_ref_wrong_scene=1`：op_0019 用 ch1_s1 的 ev_0028 支撑 source ch1_s3。
- `undeclared_temporary_ref=2`：op_0005/new_character_1、op_0008/new_item_2 未在原 draft 声明。
- `MemoryValidationReport.valid=false`；dry-run 未执行，applicable 验收失败。
- 首次输出 finish_reason=length（8192 tokens）；一次结构化修复后输出完整 JSON。2 次 HTTP，58,665 tokens（输入 45,136，输出 13,529）。

辅助语义预审（AI 标记，不能代替人工批准）：

| 位置 | 初步标记 | 需要审阅 |
|---|---|---|
| op_0001 | wrong / 原稿问题 | 将正文中的青玄宗写入事件，但 Canonical 是青云宗；原稿连续性需先解决 |
| op_0002 | wrong 风险 | source scene 1 的证据支持当场纹路相似，statement 却混入“后来所得”碎片 |
| op_0005、op_0008 | wrong（契约已拒绝） | 不能让模型自行新增未声明 temporary entity |
| op_0015–17 | correct 候选 | 保留了“猜测”措辞和 suspected；仍须判断是否值得长期 fact、是否重复 |
| op_0019 | wrong（契约已拒绝） | 一个 operation 引用不同场景，须拆分/选取对应场景证据 |
| 全章 | missing 风险 | 所得碎片只落在自然语言 attributes/事实中，未形成可靠 typed ownership 更新；不能以合法非空 batch 代替高风险变化覆盖审查 |
| op_0006 等 | over-extracted 风险 | 一次性事件同时建长期 StoryFact，需逐项判断是否重复和是否有长期价值 |

未改原稿、未手工删错 operations 伪造验收通过。

## Auto / Pro A/B Result

Auto attempt：`chapter_01/attempts/auto_1/`。同一 source package、before state、catalog、prompt、temperature 和 max_tokens；Auto 在 120 秒 HTTP read timeout 后退出，没有 batch，没有 usage。超时不是“模型质量低”的证据。

比较文件：`comparison_20261022.json`，包含成功解析但无效的 Flash 和失败的 Auto，不只筛选最佳候选。计划各 3 次，当前各 1 次尝试，剩余 4 次未执行。样本不足，不能排名。

未调用 Pro。旧 DeepSeek-V4-Flash 未列出，因此没有旧版真实验收结果。文笔 A/B 未运行，不覆盖原稿。

## First Real Canonical Commit

**未发生。** 只读 `audit` 核实 Chapter=0、Commit=0、Snapshot=0、Event=0，当前 hash 与 before 一致。离线正式 service 入口用合成批准验证过 v3 提交、事实依赖映射、resolved provenance；不能称为真实提交。

## Duplicate Finalize

离线已验证 duplicate=true、全库 fingerprint 不变、state hash 不变；并行两个正式 finalize 只有一个原子成功，另一个回滚无额外记录。真实 duplicate 验收必须等首次人工批准提交后执行。

## Provider Deadline / Cancellation

- connect 10s、read 120s、Agent operation 总 deadline 240s。
- 默认最多 2 次 HTTP retry、2 次 JSON repair；Director/ScenePlanner 的 domain repair 同享 Agent deadline。
- CancelledError 立即传播，取消后不启动后续生成阶段、不写 Canonical。
- SSE 真断连测试使用忽略 SIGTERM 的真实子进程，验证 terminate → 有界等待 → kill → await，无子进程遗留。
- 阶段 checkpoint 带 state/input/model/prompt/code/capability 签名及 payload hash；不覆盖旧 attempt。失效输入拒绝 resume。产物最后发布 preview completion marker，中断保存可以补齐。
- finalize 的批准、原 hybrid report 和最终 deterministic recheck 均保留在 commit 审计 JSON。无需在 finalize 重新调用模型。

## Cost Record / Budget Stop

| 范围 | HTTP 次数 | 已知 tokens | 未知 usage 的保守预留 |
|---|---:|---:|---:|
| 小探测 | 4 | 293 | 0 |
| 原稿 Flash | 2 | 58,665 | 0 |
| 原稿 Auto | 1 | 未知 | 73,015 |

本章台账占用 **131,680 / 150,000**，剩余 18,320，低于同规模下一次调用预留。已停止收费实验，未通过改 chapter ID/新建 run 绕过限额。总已知用量 58,958 tokens；Auto 的实际计费未知，不能记成 0，也不能把预留当成真实消耗。价格未核实，不报告金额。本地超时不能保证供应商停止已接收请求的计算/计费。

预算台账只涵盖本轮启用 ledger 后的请求，原历史生成的费用没有被冒充纳入统计。

## Chapter 2/3 Readiness

尚不具备真实第二章起跑条件。必须先修好/验证原稿 Memory，再另存受影响场景修订稿并重新 hybrid Continuity + Catalog + Memory + dry-run，人工批准后提交。三章真实提交通过前，不实施 P3、不进入 Phase 9 UI。

## Remaining Blockers / 下一步决策

1. 预算门槛：本轮不再调用。需用户决定是否增加明确的额外实验预算；不得清空未知 usage 预留。先离线压缩 schema/重复 envelope、减少无意义输出字段，测算预留，再提出新一轮金额/用量范围。
2. 原稿 Memory：明确 declared temporary entity 白名单、source-scene 证据边界，保持 Validator 严格。新的 prompt/参数试验必须单列 cohort，不能合并冒充当前 3×A/B。
3. Auto 在大上下文中超时；小 JSON probe 成功不代表能胜任大 batch。检查账户实际能力/路由或另批有界实验，不能猜测其底层模型或提高 timeout 假装通过。
4. 人工语义门：需覆盖 correct/missing/wrong/over-extracted；原稿宗门名、古镜位置、重复场景问题仍待 hybrid 审查和局部修订。当前不应批准 finalize。
5. 后续尚未交付：完成 3×Memory A/B、两场景文笔盲评、真实首次/重复提交、真实三章、30 条固定 retrieval 验收、真实十章与逐章文学评分。

代码、离线验证、真实验证、人工批准四种状态不可混用；当前结果不证明真实三章或十章可靠性。
