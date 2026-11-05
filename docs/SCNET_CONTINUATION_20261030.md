# SCNet《择道者》实际续跑 — 2026-10-30

## 结果

- 任务：`4336af8fb4ac4862833b0c66878917ed`，原定五章；第 1 章已恢复为 `awaiting_review`，第 2–5 章尚未生成，仍须逐章人工批准。
- 最终候选：`54d8010f3fa3`，5,253 字符、5 场景、32 条记忆操作。
- Continuity valid；Memory valid，0 error / 0 warning（含 0 evidence_not_in_draft、0 knowledge_unknown_fact）；dry-run applicable。
- 模型文学评分（可读性/人物/对话/衔接/推进）：4 / 4 / 4 / 4 / 5，仅为模型建议，非人工批准。
- Chapter / Commit / Snapshot / Event 各 0；前后 Canonical hash 均为 `75bfc21ac161184170721268c337b78975241794bc75047c68d624a1a093983e`。没有真实 finalize。

## 保存位置

- 正文：`studio_runs/4336af8fb4ac4862833b0c66878917ed/chapter_01/attempts/54d8010f3fa3/chapter.txt`
- 逐条审阅：同目录 `reading/review.md`；`preview_package.json`、`validation_report.json`、`dry_run.json`、`quality.json` 均保留。
- 总审计：任务根目录 `recovery_final_20261030.json`。
- 原始失败候选 `b38fa416232e` 与所有中间失败/修订候选保留，没有覆盖。

## 故障与处理

1. **思考占满输出额度**：实际 SCNet 请求接受 `enable_thinking=false`、DeepSeek 原生 `thinking.type=disabled`，但 Flash 仍返回大量 reasoning。不能把“接口接受”当成“思考已关闭”。原生格式参照 [DeepSeek 官方说明](https://api-docs.deepseek.com/guides/thinking_mode/)，是否生效以 SCNet 实测为准。
2. **受控输出调额**：8k 仍空输出；16k 曾让原稿检查/抽取/评审完成，但修订稿检查又耗尽 16k，故停止盲目加额。账户模型列表当时没有 Auto，没有假定免费或静默改为 Auto。
3. **显式角色切换**：小额验证账户已列出的 GLM-5.2，然后仅将 ContinuityChecker、WriterRepair、MemoryExtractor、LiteraryReviewer 配置为 GLM-5.2。Director / ScenePlanner / Writer 仍为 DeepSeek-V4.1-Flash-1017，此轮未重新调用前三者。GLM 参数探测未返回 reasoning 明细，不将缺失值宣称为已证实的 0。
4. **局部正文修订**：只在 scene 4 补黄守一站位、阿禾反应，在 scene 5 明确渠水过闸不等于灵气恢复。scene 1–3 与原稿逐字相同；修改和来源另存 `editorial_revision_*.json`。
5. **记忆修订**：Flash 原稿批次有跨场景证据；GLM 修订稿批次有章内中间状态误当 Canonical 旧值、未声明旧册依赖等 6 个 critical。未减弱 Validator；用真实旧值修正 `expected_old_value`，依据精确原句显式声明旧册，重新执行本地 Validator/dry-run。
6. **语义清理**：去除“看死过就说明已应验”的无证据结论；不把主角私有道途画面泄漏给旁观者；事件改用正确临时地点；两条一次性观察/调解不扩成永久关系；补上阿禾择道结果未验证的剧情债务。完整差异见 `memory_editorial_corrections_*.json`，不是冒充模型原始输出。
7. **干运行一致性**：补全心理状态旧值的 dry-run 投影，并让提交复验共用同一构造函数。Canonical 生成 provenance hash 定义未原地改写；更新的是新候选的预演结果和 preview hash。

## 用量与验证

- 本轮新增 11 次真实请求、143,405 tokens（含探测和失败）；任务累计 25 次、265,308 tokens；未知 usage 0。
- 本章上限 300k tokens / 50 次；任务总上限保留 2M；未核实单价，不换算费用。最后两次 Memory 修正/重验纯本地，未产生模型请求。
- 最终全量离线回归：`pytest -m 'not integration'`，360 passed、2 deselected；包括提交幂等、事务回滚、恢复不重规划/写作、显式声明证据和提交投影一致性。

## 尚需人工处理

当前 32 条 operations 必须逐条审阅，尤其是旧册归属、伤势/心理变化、角色知识可见性和剧情债务。没有自动填写批准、没有提交；批准当前候选后工坊才能生成 Chapter 2。五章/十章真实连续性与质量闭环仍未完成。
