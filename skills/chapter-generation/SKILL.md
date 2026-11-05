# Skill: chapter-generation

## Purpose

实现章节规划、scene planning、Writer、重写和 chapter service 时使用。

## Director ≠ Writer

Director 负责结构；Writer 负责文学表达。

Director 输出必须包含：
- goal
- scene beats
- conflict
- reveals
- must_not_reveal
- plot threads advanced
- expected state changes
- ending hook

Writer 不自行决定重大设定变化。

## Scene Writing

推荐每章 3~6 scene。

每个 SceneContext 仅包含：
- scene goal
- participants
- POV-safe knowledge
- location
- relevant abilities/items
- relevant past events
- constraints
- previous scene tail

## Long chapter strategy

不要一次请求 6000~10000 中文字然后希望模型保持结构。

分 scene 生成，再合并、检查衔接。

## Rewrite strategy

优先局部修：
- continuity report 指出问题段；
- 提供 canonical correction；
- rewrite 该 scene/段落。

只有全局结构错误才重写整章。

## Quality gate

finalize 前：
- required beats covered
- no critical continuity issue
- no hard constraint violation
- ending does not accidentally reveal must_not_reveal
- chapter has progression, not pure filler unless user requested
