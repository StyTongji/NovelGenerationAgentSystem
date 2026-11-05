# Chapter Generation Pipeline

## 输入

```json
{
  "novel_id": "...",
  "user_direction": "下一章去天剑城拍卖会……",
  "target_length": 4500,
  "pov_character_id": "...",
  "style_profile": "default-xuanhuan"
}
```

## Step 1 — Load State

读取：
- current snapshot
- current arc
- active constraints
- protagonist state
- open debts
- active plot threads

## Step 2 — Retrieve Memory

确定性检索：
- 当前参与人物
- 当前地点/目标地点
- 相关 item/faction/fact

语义检索：
- 与 user_direction 相关事件
- 与活跃 plot threads 相关历史

补充：最近 3~5 章 summary + 上一章末尾 500~1500 字。

## Step 3 — Director

输出 ChapterPlan。

Plan 必须明确：
- chapter goal
- emotional direction
- scenes
- plot threads advanced
- debts touched
- facts revealed
- must_not_reveal
- expected state changes
- ending hook

## Step 4 — Plan Validation

先规则检查。

Critical violation → repair plan，不进入 Writer。

## Step 5 — Scene Writing

每 Scene：
1. 构建 scene-specific context；
2. Writer 生成；
3. 轻量 scene check；
4. 保存临时 draft。

SceneContext 必须包含：
- scene goal
- participants + current state
- POV knowledge
- location
- relevant items/abilities
- must include
- must avoid
- previous scene tail

## Step 6 — Full Continuity Review

整个 chapter 合并后检查。

严重度：
- info
- warning
- error
- critical

critical 必须修复；error 默认修复；warning 可记录。

## Step 7 — Rewrite

只把问题定位 + 原段落 + canon facts 发给 rewrite，不要整章无限重写。

若全局结构问题，再整章/scene 重写。

## Step 8 — Finalization Gate

满足：
- no critical issue
- hard constraints pass
- plan required beats covered

才能 finalize。

## Step 9 — Memory Extraction

从 FINAL 文本抽取 MemoryUpdateBatch。

所有 update 带：
- evidence_text
- evidence_scene
- confidence

## Step 10 — Validate & Apply

验证后事务更新：
- events
- current state
- knowledge
- relationship
- items
- plot threads
- foreshadowing
- debts
- summaries
- snapshot

## Step 11 — Return

API 返回：
- final chapter
- summary
- notable state changes
- unresolved warnings
- suggested directions（可选）


## Phase 4 实现说明

Step 3-4（Director + Plan Validation）已落地：

- `app/agents/director.py`：DirectorContext → 分区 prompt（SYSTEM RULES 由 `prompts/director.md` 提供；AUTHOR CONTEXT / HARD CONSTRAINTS / USER DIRECTION / OUTPUT CONTRACT 分区）→ structured ChapterPlan → `ChapterPlanValidator` → 有界 domain repair（≤2 次）→ `PlanGenerationResult`。
- `app/planning/validator.py`：A 角色存在、B 死亡/叙事模式、C 地点、D 物品归属、E 功法归属、F 境界、G hard constraints、H reveal policy、I 角色知识，另加 plot thread/debt/arc 推进检查。
- Step 5 起（Scene Writing）尚未实现；generate_preview 到 validated plan 为止，不调用 Writer、不写库。
- `schemas/chapter_plan.schema.json` 由 Pydantic 模型直接生成，保证契约零漂移。


## Phase 5 实现说明

Step 5（Scene Writing）已落地为 preview 流水线：

- `ChapterPlanningService`（validated plan）→ `to_writer_safe()` → `ScenePlannerAgent`（只消费 WriterSafePlan，structured 输出 + `ScenePlanValidator` + 有界 domain repair）→ 逐 scene：`build_writer_context(pov=scene.pov)` → `WriterAgent.write_scene`（generate_text + 输出 guard）→ `ChapterWorkingMemory` 更新 → 确定性拼接为 `ChapterDraft(status="preview")`。
- 每个场景独立重建 WriterContext（per-scene knowledge firewall）；`previous_scene_ending` 为上一场景末尾截断，不回传全文。
- 新契约 `schemas/scene_plan.schema.json` 由 Pydantic 生成。
- Step 6（Continuity Review）起尚未实现；draft 不触发 continuity check、不 commit。


## Phase 6 实现说明

Step 6-7（Continuity Review + Rewrite）已落地为 preview 流水线：

- `ContinuityService.check_draft`：`ContinuityContext`（author-level、相关性过滤）→ deterministic 13 类规则 → hybrid 模式下 LLM 语义检查（structured output + post-validation）→ `ContinuityIssueMerger` → `ContinuityReport`（含 draft_hash / context_hash / 来源计数）。
- `ContinuityService.repair_preview`：blocking（error/critical）按 scene 分组 → `project_issue_to_writer_safe`（truth-free 指令模板 + preserve=required outcomes）→ `WriterRepairAgent` 仅重写受影响 scene → 重新 check；`max_repairs=2` 有界，耗尽返回 `exhausted=true`。
- 两层 repair 分离保持：LLM structured repair（JSON）≠ domain repair（剧情逻辑）≠ continuity repair（正文 vs canonical）。
- API：`POST /novels/{id}/chapter-draft/check`、`POST /novels/{id}/chapter-draft/repair-preview`。服务端永远重新检查，不信任客户端 continuity 状态。
- `schemas/continuity_report.schema.json` 由新 `ContinuityReport` 模型重新生成。


## Phase 7 实现说明

Step 9-10 的前半（Memory Extraction + Validation）已落地为 preview：

- `MemoryExtractionService.extract_preview`：continuity gate（valid + draft_hash 一致，否则 409/422 且零 LLM 调用）→ `MemoryExtractionContext`（canonical = reference, draft = evidence）→ `MemoryExtractorAgent.generate_structured` → fingerprint 去重 → `MemoryUpdateValidator`（12 层，无自动 domain repair）→ `MemoryDryRun`（纯域 before/after）→ `MemoryExtractionResult`。
- Phase 6 Fix A/B 随本阶段落地：`repair_redaction.py`（leak evidence 句级 redact）、merger key 增强 + evidence-overlap 保守去重。
- 明确禁止：`memory-update/apply`、`chapter/finalize`、snapshot 写入、ChapterSummary 持久化——全部留给 Phase 8 的 CanonicalCommitService。
- API：`POST /chapter-draft/memory-preview`、`POST /memory-update/validate`（可选 dry_run=true）。
- `schemas/memory_update.schema.json` 由新契约模型重新生成。


## Phase 8 实现说明

Step 8 / 10-11（Finalization Gate + Transactional Apply + Snapshot）已落地：

- `ChapterGenerationService.generate_preview`：planning → draft → continuity（必要时 targeted repair，exhausted 则中止）→ memory extraction + dry-run → `ChapterPreviewPackage`（含 preview_package_hash）。
- `ChapterGenerationService.finalize`：零 LLM；构建 `ChapterCommitRequest` 交由 `CanonicalCommitService.commit_chapter`。
- `CanonicalCommitService`：staleness/hash/duplicate 校验 → deterministic continuity recheck → memory 重验 + fresh dry-run → 单事务应用（固定顺序）→ chapter 版本化保存 → summaries → current_chapter_number → snapshot → `chapter_commits` 记录（fingerprints + per-operation evidence provenance）。
- 新表 `chapter_commits`；`chapters` 新增 draft_hash / before / after state hash 列；`chapter_summaries` 新增 author_summary / reader_summary 列（migration e0e14c253f15）。
