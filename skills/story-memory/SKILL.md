# Skill: story-memory

## Purpose

实现或修改人物记忆、事件、知识、伏笔、剧情债务、向量检索与 Context Builder 时使用。

## Principle

“记忆”不是一个向量库。

### Semantic canonical memory
SQL 中的稳定事实：人物、境界、物品、地点、关系、事实。

### Episodic memory
Event log：发生过什么。

### Working memory
最近章节、上一 scene、当前 plan。

### Vector index
只是 episodic/summary 的召回索引，不是真相源。

## Character Knowledge Rules

任何秘密相关事实都必须可表达为 StoryFact。

角色知道事实时新增 CharacterKnowledge，不要复制/改写 StoryFact。

WriterContext 必须通过：

```python
visible_facts = public_facts | facts_known_by(pov_character)
```

禁止直接给 Writer hidden truth。

## Retrieval

优先顺序：
1. exact entity linkage
2. active plot linkage
3. recent events
4. semantic vector retrieval

建议混合评分：
`0.50 semantic + 0.25 recency + 0.25 importance`

但如果事件与当前 entity 强关联，可 deterministic boost。

## Memory Extraction

Extractor 只提出 delta，不生成完整新 snapshot。

每条高风险 delta 必须带 evidence：
- death
- realm breakthrough
- item ownership
- relationship reversal
- secret learned
- plot resolved

## Do not

- 每章重新总结人物全历史作为唯一人物记忆；
- 用 embedding 检索回答“当前 owner 是谁”；
- 用 summary 覆盖 event log；
- 把 knowledge 当 relationship 字段。
