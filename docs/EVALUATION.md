# Evaluation

不要只评价“文笔好不好”。系统至少有 5 个维度。

## 1. Canon Consistency

构造固定世界状态与带陷阱的章节请求，统计：
- death contradictions
- knowledge leaks
- item ownership errors
- illegal power changes
- time/location contradictions

目标：随着章节增长，错误率不应明显随 context 长度恶化。

## 2. Memory Recall

在第 N 章埋入事实，在 N+10 / N+50 / N+100 请求相关剧情，检查：
- 是否检索到；
- 是否正确使用；
- 是否把不相关事实塞进上下文。

## 3. Plot Thread Management

评价：
- active thread 是否被持续追踪；
- foreshadowing 是否有 setup/reinforcement/payoff；
- 是否提前泄底；
- plot debt 是否被遗忘。

## 4. User Control Adherence

测试 hard 与 soft instruction：
- “苏青不能死亡”必须 100% 遵守；
- “接下来减少战斗、增加探索”应高概率遵守但允许合理例外。

## 5. Prose Quality

人工/LLM-as-judge 可评价：
- 可读性
- 重复度
- 对话自然度
- 场景空间清晰度
- 章节推进感
- hook 质量

文笔评价应与 Canon consistency 分开，避免“写得爽但逻辑崩”得到高分。
