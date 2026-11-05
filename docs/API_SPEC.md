# API Spec (MVP)

Base: `/api/v1`

## POST /novels
创建小说。

Request:
```json
{
  "title": "九霄问道",
  "genre": "玄幻",
  "premise": "..."
}
```

## POST /novels/{novel_id}/bootstrap
初始化世界。

可提交：
- world bible
- power system
- characters
- factions
- starting items
- initial facts
- first arc

## GET /novels/{novel_id}/state
返回前端可展示的 StoryStateView，不要暴露所有 hidden truth 给普通读者视图。

支持 `view=author` / `view=reader`。

## POST /novels/{novel_id}/chapters/plan
只生成计划，不写正文。

## POST /novels/{novel_id}/chapters/generate
Request:
```json
{
  "user_direction": "...",
  "target_length": 4500,
  "pov_character_id": null,
  "finalize": true
}
```

## POST /novels/{novel_id}/chapters/{number}/regenerate
重新生成，不覆盖旧版本。

## GET /novels/{novel_id}/chapters/{number}
可通过 `version` 查询历史版本。

## GET /novels/{novel_id}/characters

## GET /novels/{novel_id}/characters/{id}
author view 包括 secret/knowledge；reader view 过滤。

## GET /novels/{novel_id}/plot-threads

## POST /novels/{novel_id}/constraints

## PATCH /novels/{novel_id}/constraints/{id}

## POST /novels/{novel_id}/facts
作者手工加入 canonical fact。

## 错误模型

```json
{
  "code": "CONSTRAINT_CONFLICT",
  "message": "用户指令与硬约束冲突",
  "details": [
    {"constraint_id": "...", "reason": "..."}
  ]
}
```
