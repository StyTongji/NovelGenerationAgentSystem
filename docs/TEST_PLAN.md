# Test Plan

## 1. 单元测试

### Domain
- realm progression
- item ownership transfer
- death state
- relationship directionality
- knowledge visibility
- plot thread transitions
- debt deadline

### Structured Outputs
- malformed LLM JSON retry
- unknown enum reject
- missing evidence reject for destructive updates

## 2. Context Retrieval Tests

### POV 隔离
事实：师尊是魔教卧底。
- world_truth=true
- protagonist does not know

WriterContext(protagonist) 不能出现该事实正文。
DirectorContext 可出现。
ContinuityContext 可出现。

### Relevant retrieval
查询“拍卖会”：应召回相关城市、旧拍卖冲突、灵石状态，不应召回 30 个无关 NPC。

## 3. Continuity Regression Dataset

维护 `tests/fixtures/continuity_cases/`。

至少：
- dead_character_returns
- unknown_secret_leak
- item_double_owner
- illegal_realm_jump
- impossible_travel
- broken_hard_constraint
- unresolved_thread_misreported
- deadline_mismatch
- forgotten_injury
- technique_not_learned

## 4. End-to-End Scenario

世界：
- 林凡：筑基后期
- 苏青：筑基中期
- 陈海：敌人
- 轮回古镜：林凡持有，秘密
- 父亲失踪：active thread

Chapter N：苏青替林凡挡剑，并看到林凡使用古镜。

After finalize 必须得到：
- Event(rescue)
- 苏青 injury
- 苏青 knows 古镜存在
- 林凡仍为 owner
- relationship trust upward
- father thread 不应自动 resolved

Chapter N+1 WriterContext(Su Qing) 可以知道古镜；其他普通角色不可以。

## 5. Golden Tests

重要 prompt + fake deterministic LLM 输出做 golden test，避免重构后 schema/pipeline 意外变化。


## 6. Phase 3 Context / Isolation Tests（已实现）

- `tests/test_knowledge_isolation.py`：POV 防火墙（林凡排除 / 苏青 suspected 框架 / 魔教长老 known）、reader 已知≠角色已知、forgotten 排除、believed_false 错误认知框架、Event summary 掩码、author summary 不进 WriterContext、relationship hidden_description 不泄露、Director 可见 truth 而 Writer 永不可见
- `tests/test_retrieval.py`：recency decay、bigram 语义分、实体链接（含别名）、事件检索（participant/location/importance）、Hybrid top-K 与确定性顺序、source type 过滤
- `tests/test_context_builder.py`：thread 状态与 `chapters_since_last_progress`、debt urgency（overdue/due_soon/no_deadline）、constraint 章节窗口、hard constraint 永不被预算裁剪、budget top-K 确定性、上一章结尾截断、全量确定性（同一状态同一查询输出一致）、debug include/exclude


## 7. Phase 4 Plan Validation Tests（已实现）

- `tests/test_plan_validator.py`：非法 Plan case 1-10（死亡角色、未持有法宝、境界跳级、hard constraint 死亡、角色基于未知秘密行动、hint-only 被 full reveal、hallucinated UUID、temporary_ref 新角色 valid、逾期债务 warning、resolved 线索被重开/premature resolution）+ 验收场景（拍卖会+玉佩+新角色+hint-only → valid）+ 地点存在性 + must_not_reveal 自检 + no-progression warning
- `tests/test_director_agent.py`：happy path（provenance/context_hash/prompt_version）、writer-safe 投影排除 director_notes 与 secret、domain repair 成功（repair_count==1）、repair 耗尽返回 invalid（有界）、hard constraint repair（战死→重伤存活）、**生成不写库**、user_direction 分区（不进 system prompt）
- `tests/test_planning_api.py`：preview API（FakeLLM）、404、validate API（用户编辑后的 plan 再校验）


## 8. Phase 5 Draft Pipeline Tests（已实现）

- `tests/test_scene_planner.py`：A 合法批次、B 缺 beat repair、C 非法 POV、D 未声明 temporary_ref、reveal 升级拒绝、确定性、Scene Planner 不见 director_notes、scene_plan.schema.json 兼容
- `tests/test_writer_agent.py`：N prose + metadata、guard（fence/scaffolding/JSON/空输出）、prompt 分区与无 author secret
- `tests/test_chapter_draft.py`：全流水线 preview、L DB 零变化、I 上一场景结尾衔接（截断）、J 无 context 爆炸、K temporary_ref 一致、M 中途失败（错误对象携带已完成 scene、DB 不变）、invalid plan 拒绝（零 LLM 调用）、stale plan 409、O multi-POV 秘密 per-scene、§53 prompt capture（director_notes/forbidden_information/thread truth/未知 secret 不出现在最终 writer request）、P previous ending 过防火墙、G raw user_direction 结构性禁入
- `tests/test_draft_api.py`：scene-plan preview、chapter-draft preview、invalid plan 422


## 9. Phase 6 Continuity Tests（已实现）

- `tests/test_continuity_deterministic.py`：Case 1-6/8-10 全部确定性规则 + 未授权突破（额外）+ 跨两个境界突破 + 临时实体身份冲突 + 时间倒流 + 空间瞬移（含传送阵豁免）+ 干净正文零 issue
- `tests/test_continuity_llm.py`：Case 7 语义改写泄密（规则层漏检、LLM 层捕获）、hallucinated scene_id 丢弃、伪造 evidence 清空、未知 category 归一、merger（severity 不降级 / evidence 合并 / deterministic explanation 保留 / 排序）、hybrid 报告计数与 draft_hash、deterministic_only 模式零 LLM 调用
- `tests/test_continuity_repair.py`：knowledge_leak / reveal_policy / hard_constraint 三类 writer-safe 投影（§53 secret 只在待修复原文数据块内）、仅修复受影响 scene（§54）、preserve required outcomes（§55）、repair 前后 DB 零写入（§56）、两轮耗尽 exhausted（§57）、checker prompt capture（author-level 可见 secret + draft 数据块）、repair prompt capture（指令层无 secret）
- `tests/test_continuity_api.py`：check 端点、issue 检测、跨 novel 409、repair 无 agent 503


## 10. Phase 7 Memory Extraction Tests（已实现）

- `tests/test_memory_validator.py`（19）：missing/hallucinated/plan-as evidence、span 重定位与歧义、old_value mismatch、realm 合法/非法转换、hallucinated UUID、cross-novel critical、fingerprint 去重、knowledge-unknown-fact、duplicate story fact、suspicion→known 拒绝、believed_false 不改 world truth、thread advance/premature resolve、undeclared temporary ref、reader summary 泄密
- `tests/test_memory_extraction.py`（14）：blocking report 零 LLM 调用、stale report 拒绝、plan-only event 拒绝、canonical-secret contamination、**苏青挡剑核心验收**（event+injury+knowledge，无重复 fact，dry-run after-state）、repeat extraction 幂等、dry-run 全表零写入、temporary item access ≠ transfer、顾清璃 proposal（不 INSERT）、death candidate（requires_confirmation）、合法 realm 转换 after-state、visibility metadata、author/reader summary 隔离、extractor prompt capture（canonical=reference 标记）
- `tests/test_memory_redaction_api.py`（8）：redaction 单元（句级/保护语句 fallback/整场景保守/非 leak 不动）、repair prompt capture（marker 替换）、memory validate/dry-run/preview-gate API


## 11. Phase 7.5 Preview E2E Audit（已实现）

- `tests/e2e/test_preview_pipeline.py`（5）：happy path 全链路（planning→draft→continuity→memory→dry-run）、secret isolation（planner/writer/repair prompt 无 secret，Director/Extractor author-level 可见）、provenance 链（planning.context_hash == draft.story_context_hash == 状态哈希；chapter_plan_hash；draft_hash 链）、continuity repair 集成（leak→检测→redaction→repair→re-check→clean→extraction）、repair exhausted 阻断 memory pipeline（extractor 零调用）、hallucinated evidence 路径、staleness gate 在任何 LLM 调用前触发
- DB 零变更为**内容级深度指纹**（14 张表逐行序列化排序后 sha256，可检测 UPDATE），非仅行数
- Audit 发现并修复的架构缺口：`ChapterDraft.scene_plans` 未被 ChapterDraftService 填充 → Continuity per-scene POV 检查拿不到数据（knowledge_leak 漏检）；`context_hash` 在 planning 与 draft 间不可比 → 统一为 story_state_hash（`app/planning/provenance.py`）


## 12. Phase 7.6 Preview Artifact Export（已实现）

- `app/debug/preview_export.py`：纯 DTO→文件 debug 服务（无 session、不 import CanonicalCommitService）；输出 `artifacts/previews/<novel_id>/chapter_<n>/<run_id>/` 下 7 个文件；run_id 唯一且拒绝覆盖；`NOVEL_PREVIEW_ARTIFACT_DIR` 为空时禁用
- E2E happy path 集成：tmp_path 导出两次（run_id 唯一）、chapter.txt == draft.full_text 且含真实正文、metadata 哈希链/状态/prompt 版本断言、导出前后 DB 深度指纹不变
- ChapterDraft provenance 增强：scene planning 元数据并入 generation_metadata


## 13. Phase 8 Canonical Commit Tests（已实现）

- `tests/test_canonical_commit.py`（15）：happy commit（chapter/event/state/knowledge/thread/canonicalized entity/summary/snapshot/current_chapter_number/before-after hash/commit record + evidence provenance）、stale state、draft hash mismatch、modified memory batch、chapter number collision、duplicate commit（幂等返回已有 result）、fact+knowledge 同批顺序、same-chapter state collapse、item transfer chain、mid-transaction rollback（深度指纹不变）、proposed entity rollback、finalize 零 LLM、cross-novel 拒绝、下一章 context 看到已提交状态、两章 committed flow
- `tests/e2e/test_committed_pipeline.py`（1）：Chapter 5 → finalize → Chapter 6 preview（基于 after hash）→ finalize，状态跨章传播；previous chapter ending 来自 committed Chapter 5


## 14. Phase 8.5 Soak Audit（已实现，pytest -m soak）

- Fix A：MemoryUpdateValidator 校验 Event.location_id 等全部 canonical 引用（scope=novel）
- Fix B：finalize 强制重算 `preview_package_hash`（篡改 → `PreviewPackageTamperedError`，是完整性层而非授权层——细粒度验证独立保留）
- Fix C：`chapter_summaries.source_draft_hash` 独立 provenance 列（不再占用 summary 正文字段）
- Fix D：`chapter_commits.continuity_report_json` 持久化 commit 时最终报告
- `tests/soak/test_10_chapter_story.py`：10 章 deterministic committed soak（`pytest -m soak`，默认排除）
  - 覆盖：跨章状态链（苏青 injured→recovering→healthy append-only）、knowledge（unknown→suspected 不升级、跨章继承）、reader-visible secret 长程隔离（leak_count=0）、item 所有权链与消耗品、顾清璃单 UUID 不变量、父亲线推进/残卷线 dormancy（chapters_since_last_progress=9）、宗门大会 debt normal→due_soon→resolved、context budget 全程生效、ch13 旧 episodic event relevance 召回、duplicate finalize（ch8）/stale parallel preview（ch10）/rollback 注入（ch12）零副作用、snapshot+hash 链逐章相连、finalize 全程零 LLM、summary source_draft_hash 绑定
- `tests/soak/story_audit.py`：LongHorizonStoryAuditor（chapter/snapshot/hash/commit/summary binding/evidence integrity/knowledge 唯一性/plot lifecycle 审计）
- `tests/soak/soak_story.py`：确定性 10 章 scripted story（非 trivial fixture，每章有独立剧情与 canonical mutation）
