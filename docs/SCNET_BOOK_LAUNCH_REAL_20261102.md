# SCNet 真实开书与第一章预览测试

日期：2026-11-02（中国时间）。测试数据和数据库位于 `real_trial_runs/scnet_book_launch_20261102_a1/`，与正式小说主库隔离。初始世界使用明确标记为“隔离自动测试-非人工批准”的测试初始化；**不是作者对真实小说的批准**。没有提交任何章节。

## 测试结果

| 阶段 | 实际结果 |
|---|---|
| 模型列表与连通性 | SCNet 返回 `DeepSeek-V4.1-Flash-1017`、`GLM-5.3-Flash` 等；本次响应列表未出现 `Auto`。Flash 简短请求成功。 |
| 资料 | 导入此前 2026-11-02 番茄男频传统玄幻、玄幻脑洞的 20 条公开榜单元数据；非起点数据，不能代表全平台或多日趋势。 |
| 市场研究 | Flash 完成分析，给出 8 条有来源引用的观察和 5 个候选，15,654 tokens，约 85 秒。选题由测试执行者选择，并非作者批准。 |
| 架构首次尝试 | 同一选题用 Flash 非流式在 120 秒超时；换 GLM 非流式也在 120 秒超时。GLM 流式在 240 秒总截止时间被取消，期间收到 13,587 个帧、最终正文字符数 0。三次实际用量未知。 |
| 架构成功尝试 | 同一研究、同一选题改用 `GLM-5.3-Flash`，流式、`enable_thinking=false`，成功返回完整世界和十章计划，8,554 tokens，约 82 秒；其中 usage 报告推理 33 tokens。参数通过实测被接受，不保证对其他任务/模型始终有效。 |
| 故事 | 《契书吏》，主角谢临。Writer 在三个场景生成 2,492 字符。连续性报告 valid；文学评审标记需要修订。 |
| 记忆 | 初次抽取 2 处跨场景引用错误；重试同一正文仍有 3 处。测试执行者根据逐字原文修改两项操作后，Memory valid、dry-run applicable。该结果仅为结构与应用预演，语义仍需人工审阅。 |
| 数据库 | 隔离库 Novel 1、Chapter 0、ChapterCommit 0、StorySnapshot 0、Event 0。正式库未写入。 |

## 设定与正文质量问题

模型的原始设定把“已埋下补充契条款”写入了开篇状态，但第一章计划又安排才去签署；公开简介、能力规则还透露了标记为秘密的机制；分卷时间点与详细章节计划也有不一致。测试执行者另存了修订候选：调整公开简介、原契与补充契的先后、部分公开/秘密规则、以及第 3/10 章时间点。为了自洽还明确了涉见证人条款的范围。**这是测试修订，创作设定仍需作者判断。**原始结果和修订 hash 均保留。

市场候选也存在明显问题：候选《借命问心》一处描述姐姐被斩首，另一处又描述她在翻案现场道谢。系统对结构和来源 ID 的验证无法保证文学设定前后一致。

正文中，场景二末尾和场景三重复落押、手指渗血、“你的笔在抖”“墨重，压手”，反噬机制也在对白中提前说破。文学评审给出可读性 3、人物 4、对话 4、连贯性 3、剧情推进 **2**，指出重复、提前揭露和一个动作描述不清的问题。连续性报告 valid 与文学评审结果并不矛盾：前者未检出这些语义/文学问题，不能当成文章质量合格。

最新记忆候选的定向修订：`op2` 改为来源场景 `ch1_s1`，新物品名采用正文中的“补充契”；`op8` 删除来自其他场景的引用，保留同场景落押证据。没有模糊匹配、降低校验严格度或重新生成正文。即使 dry-run applicable，操作的完整性、是否合并重复事实和是否过度抽取仍需逐项人工核对。

## 费用与预算

- 工作流中供应商明确返回的用量：研究 15,654 + 架构 8,554 + 章节及一次记忆重试 64,128 = **88,336 tokens**。
- 四次简短非流式连通/参数探测合计 **405 tokens**，合计已知 **88,741 tokens**。
- 三次超时或取消的长请求没有返回 usage，账本分别保留 29,028 tokens 的保守预留；另一次小流式探测未采集 usage。这些预留不是供应商实际账单，**实际总消耗未知**。本地取消不保证供应商立即停止计费，金额请以 SCNet 账户为准。
- 没有测试账号 Auto 的价格或声称其免费。各次规划 fork 使用独立账本，费用统计应加总所有关联会话。

## 本轮改进与验证

1. 增加“复用研究并换模型”：复制并校验原研究 hash，生成新会话和独立预算，换模型不必重跑已完成研究。原任务与花费保留。
2. 增加蓝图修订：作者可在 `/book-launch` 编辑 JSON，保存为新 hash；原候选保留，过期确认失效，取消后恢复不会覆盖修订。CLI 的 fork 也支持传入已验证的能力 JSON。
3. 完整离线回归：`378 passed, 2 deselected`；其中开书定向测试 12 项。生成的 JSON/JSONL/TXT/MD 扫描未发现 SCNet Key。

## 产物

- [第 1 章正文（原稿，仍需修订）](../real_trial_runs/scnet_book_launch_20261102_a1/studio/33b9f31e92ee4f3abdd77f40bf445be4/chapter_01/attempts/fc18ad8b4e94/chapter.txt)
- [文学质量报告](../real_trial_runs/scnet_book_launch_20261102_a1/studio/33b9f31e92ee4f3abdd77f40bf445be4/chapter_01/attempts/69d09d9c3958/quality.json)
- [市场研究与五个选题](../real_trial_runs/scnet_book_launch_20261102_a1/studio/book_launch/bfacc4956d554b148fc1b9d508f529fe/research.json)
- [模型原始设定与十章计划](../real_trial_runs/scnet_book_launch_20261102_a1/studio/book_launch/85b23084bb23449886761f011a25ad1a/blueprint.json)
- [测试修订后的设定](../real_trial_runs/scnet_book_launch_20261102_a1/studio/book_launch/85b23084bb23449886761f011a25ad1a/blueprint_a8a0d3f3d20dc3f7cbf3e0438660b23ef30ea4ff3ee291b43f52925f4b7f56d6.json)
- [最新记忆预览和 dry-run](../real_trial_runs/scnet_book_launch_20261102_a1/studio/33b9f31e92ee4f3abdd77f40bf445be4/chapter_01/attempts/fc18ad8b4e94/memory_preview.json)

查看页面（本地服务运行时）：<http://127.0.0.1:8342/studio?run=33b9f31e92ee4f3abdd77f40bf445be4>。

服务若已退出，在项目根目录运行以下命令重开**同一个隔离测试库**；仅查看结果不需要 API Key：

```bash
cd /home/sty/novel_gen/novel_engine_agent_pack
conda activate novel-engine
NOVEL_DATABASE_URL="sqlite+aiosqlite:///$PWD/real_trial_runs/scnet_book_launch_20261102_a1/test.db" \
NOVEL_STUDIO_DIRECTORY="$PWD/real_trial_runs/scnet_book_launch_20261102_a1/studio" \
python -m uvicorn app.main:app --host 127.0.0.1 --port 8342 --workers 1
```

后续真实创作前应先修正文重复与泄露，再重新执行连续性、记忆抽取和 dry-run，并逐条审阅；当前结果不能作为真实多章闭环或正式提交验收。
