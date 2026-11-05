# Skill: novel-engine

## Purpose

当任务涉及本项目整体架构、章节生成 orchestration、领域边界或开发优先级时使用此 skill。

## Core mental model

本项目是 Narrative Engine，不是 Chat Completion wrapper。

始终从以下 5 层思考：

1. Canonical State
2. Retrieval / Context Projection
3. Planning
4. Prose Generation
5. Validation + State Commit

## Workflow

### 新功能设计前
检查该功能属于：
- canonical data
- derived data
- agent proposal
- domain validation
- UI projection

不要把 domain truth 放到 prompt-only 层。

### 编写章节功能
遵循：

```text
state -> context -> plan -> validate -> scenes -> write -> check -> extract -> validate updates -> transaction
```

### 新增 Agent 前
回答：
1. 是否可以 deterministic code 完成？
2. 是否已有 Agent 可以承担？
3. 是否需要非确定性语言理解/生成？

只有 3=yes 且职责边界清晰时才新增 Agent。

## Must preserve

- Event sourcing
- Knowledge isolation
- Hard constraints
- Immutable chapter versions
- Transactional finalization
- Structured LLM outputs

## Completion checklist

- [ ] 新数据有 schema
- [ ] Canonical/derived 分类明确
- [ ] 有 repository/service 边界
- [ ] LLM 输出有 Pydantic validation
- [ ] 有失败路径
- [ ] 有测试
- [ ] 不会扩大 Writer 上下文到全书 dump
