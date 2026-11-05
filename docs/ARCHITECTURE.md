# Architecture

## 1. 总体分层

```text
┌───────────────────────────────────────────────┐
│                    UI                         │
│ user direction / state view / constraints     │
└──────────────────────┬────────────────────────┘
                       │
┌──────────────────────▼────────────────────────┐
│             Narrative Orchestration            │
│ ChapterService / ArcService / RetconService    │
└──────────────┬────────────────┬────────────────┘
               │                │
     ┌─────────▼────────┐  ┌────▼───────────────┐
     │      Agents       │  │  Context Builder   │
     │ Director          │  │ deterministic SQL │
     │ Writer            │  │ vector retrieval  │
     │ Continuity        │  │ context budgeting │
     │ MemoryExtractor   │  └────┬──────────────┘
     └─────────┬────────┘       │
               │                │
┌──────────────▼────────────────▼────────────────┐
│                 Domain Services                │
│ Character / Event / Plot / Knowledge / Item    │
└──────────────────────┬─────────────────────────┘
                       │
┌──────────────────────▼─────────────────────────┐
│             Canonical Persistence              │
│ SQL DB + Event Log + Vector Index              │
└────────────────────────────────────────────────┘
```

## 2. 真相源

### Canonical
- relational database current state
- event log
- immutable final chapter versions

### Derived
- chapter summary
- arc summary
- vector embeddings
- cached context bundle

Derived 数据损坏时可重建；Canonical 不可依赖 Derived 才能恢复。

## 3. Agent 与 Domain 的边界

Agent 是“不可信建议者”。

LLM 返回：proposal。
Domain service 负责：validate + apply。

例如 MemoryExtractor 可以提出：

```json
{"character_id":"x","field":"realm","new_value":"金丹"}
```

但真正更新前必须验证：
- 当前境界；
- 是否存在突破事件；
- PowerSystem 是否允许；
- 本章是否有 evidence。

## 4. Context Builder

> **Phase 3 实现说明**：retrieval 与 context 已经落地。边界为
> `repositories → retrieval（EntityLinker / StructuredEventRetriever / HybridRetriever）→ ContextBuilder → DirectorContext / WriterContext`。
> Knowledge Firewall（`app/context/knowledge_filter.py`）位于最终装配边界：所有 WriterContext 文本出口均经过 secret 掩码与 tagged-event 剔除；author-level summary 整体不进入 WriterContext。混合评分 `0.50 semantic + 0.25 recency + 0.25 importance`，语义分当前由 character-bigram 词法实现，向量后端可无感替换（`Retriever` 协议）。

Context Builder 负责把 Canonical State 投影成特定任务需要的上下文。

### DirectorContext
比 WriterContext 更宽，可知道隐藏 plot truth。

### WriterContext
按 POV 限制秘密，只包含场景需要信息。

### ContinuityContext
允许看到 canonical truth，以便找冲突。

### ExtractorContext
需要当前 state + 本章最终正文，防止把已有事实当新增事实。

## 5. Context Budget

建议分配：
- 10% system/style
- 15% current state
- 15% active plot
- 20% relevant history
- 10% recent summaries
- 30% scene/chapter local text

超预算时依次裁剪：
1. 低 importance 历史；
2. 远期无关人物；
3. 冗余 lore 描述；

永远不要裁掉：
- hard constraints
- 当前人物核心 state
- must_not_reveal
- scene goal

## 6. Failure Handling

- LLM invalid JSON → structured retry
- plan rule violation → repair plan
- scene continuity issue → scene rewrite
- final continuity critical → reject finalization
- memory validation failure → do not update canonical state
- vector service unavailable → deterministic retrieval fallback

## 7. 可观测性

每次 chapter run 保存：
- run_id
- model config
- prompt version
- retrieved memory IDs
- ChapterPlan
- raw/validated structured outputs
- continuity report
- token/cost stats
- finalization status

这样后续才能定位“为什么第 170 章开始崩”。
