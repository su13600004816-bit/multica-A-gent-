# PL-91 — 会话记忆四级归档与强制新会话机制

## 问题

后端在每次 chat 跟进 / issue 跟进时会自动 resume 上一个 provider session
（`chat_session.session_id` / `agent_task_queue` fallback），并且 assignment
任务的 prompt 强制读取全量 comment history（server cap 2000）。没有任何归档或
止血机制，导致每一轮都重新吃进越来越大的上下文 → token 异常偏高。

## 方案总览

把"一段对话/评论"压缩成 **四级归档 T1..T4**，新会话默认只注入 T1/T2 低梯度摘要，
并在 daemon claim 链路切断旧 session。**全程 fail-safe：永不删除 chat_message /
comment 原文，只新增摘要 + 翻转可空标记列。**

| 级别 | 用途 |
|------|------|
| T1 | 一行 headline，每个新会话默认注入 |
| T2 | 结构化短摘要（参与者/轮次/关键决策行），随 T1 注入 |
| T3 | 逐条压缩摘要，按需下探 |
| T4 | 近似逐字（每条有上限）digest，最深下探 |

## 已落地（本分支 `pl91-memory-archive`，可直接 build/test 的部分）

### A. 数据层 — `migrations/112_memory_archive.{up,down}.sql`
- 新表 `memory_archive`：`workspace_id, scope_type(chat_session|issue|task|squad),
  scope_id, level(T1..T4), content, source_from/to, source_count, generator,
  created_by_agent_id, created_at`。
- `chat_session.compacted_at`、`issue.memory_compacted_at`（可空标记列）。
- `workspace.session_resume_enabled`、`agent.session_resume_enabled`
  （resume 总开关，默认 true，保持现状）。

### B. 归档服务 — `internal/service/memorycompact/`
- `Compactor` 接口 + `DeterministicCompactor`（无模型、无网络、**永不报错**的
  fail-safe 兜底）+ `ModelCompactor`（包 `ModelClient` 抽象；逐级失败时自动回落
  到 deterministic）。
- **Qwen 已接入** — `qwen.go` `QwenClient`：DashScope OpenAI-compatible
  `/chat/completions`（与 `line_bridge.py` 记忆总管同一通道与 T1..T4 提示词）。
  `DefaultCompactor()`：设了 `DASHSCOPE_API_KEY` 用 Qwen，否则 deterministic。
  env：`DASHSCOPE_API_KEY`（必填）、`DASHSCOPE_BASE_URL`（默认 compatible-mode）、
  `QWEN_MODEL`（默认 qwen-plus）。Qwen 故障逐级回落，不阻断止血。
- `Archiver`：先把每一级落库，**全部成功后才** 翻转 compaction 标记。任一步失败 →
  不翻标记 → daemon 照旧 resume → 无止血但无损。
- `compactor_test.go`：覆盖四级生成、梯度递减、排序、关键行提取、模型回落、
  Archiver fail-safe（save 失败不翻标记）。纯 Go，无需 DB。

### C/E 局部 — daemon claim 止血 gate（`internal/handler/daemon.go`）
- 新增 `sessionResumeBlocked(agentID, issueID)`：开关关闭 or issue 已 compacted →
  返回 true（best-effort，查询出错则 fail-open 保持现状）。
- issue 任务：`!task.ForceFreshSession && !sessionResumeBlocked(...)` 才注入
  `PriorSessionID/PriorWorkDir`。
- chat 任务：额外判 `!cs.CompactedAt.Valid`，切断 `GetLastChatTaskSession` 兜底。
- 新查询：`GetIssueMemoryCompactedAt`、`GetAgentSessionResumeEnabled`、
  `MarkChatSessionCompacted`、`MarkIssueMemoryCompacted`、`CreateMemoryArchive`、
  `ListMemoryArchivesByScope`、`GetLatestMemoryArchiveLevel`
  （`pkg/db/queries/memory_archive.sql`，需 `sqlc generate`）。

## D 已落地（trigger 侧，本轮接线，已 build+test 通过）
- **归档服务** `internal/service/memory_archive.go` `MemoryArchiveService`：
  读 issue 评论 → `Compactor`（Qwen/deterministic）→ 落 T1..T4 → 标记
  `issue.memory_compacted_at`，事务内、fail-safe；`dbArchiveStore` 适配
  `db.Queries`，`commentsToMessages` 转换。
- **issue 完成后归档** `service/task.go` `CompleteTask`：post-commit 最佳努力调用
  `ArchiveIssue`（失败仅告警，不动原文、不回滚任务）。
- **构造接线** `handler/handler.go`：`taskSvc.Memory = NewDefaultMemoryArchiveService(...)`。
- **claim 注入** `handler/daemon.go`：issue 已归档时设 `resp.MemorySummary`（T1+T2）。
- **DTO 透传** `agent.go` / `daemon/types.go`：新增 `memory_summary` 字段
  （server→daemon JSON 透传，无需手动 mapping）。
- **prompt 收敛** `daemon/prompt.go`：assignment 分支带 summary 时注入 T1/T2、
  不再要求全量 2000 条；原文不删，必要时逐级下探。
- **测试**：`prompt_test`（compacted 注入 / 未 compacted 保留全量）、
  `memory_archive_test`（转换/nil-safe）、`memorycompact`(15)；`go build ./...` 绿；
  handler/daemon/service 全套件无回归。

## 仍待接线（下一批）

## C/E 已落地（第二批接线，已 build+test 通过）
- **C. chat 超阈值止血** — `handler/chat.go` `SendChatMessage`：在创建新用户消息
  **之前**调 `MaybeCompactChatSession`。`GetChatSessionWindowSize` 只数“上次压缩后”
  的活动窗口（`since=chat_session.compacted_at`），超 `40 条 / 200KB` 阈值即
  `ArchiveChatSession` → `MarkChatSessionCompacted`（清空 session_id/work_dir + 盖
  compacted_at）。新消息开启全新窗口。
- **daemon chat gate（续）** — 不再硬挡整段；compacted 后 `cs.SessionID` 已为空 →
  主续接自然落空；**仅跳过跨任务 fallback**（避免摸到压缩点之前的旧 session）。
  fresh 会话完成后 `CompleteTask` 回填 `cs.SessionID`，连续性自动恢复。
- **E. retry 连续失败 fresh** — `CreateRetryTask`：父任务本身已是 retry
  (`parent_task_id IS NOT NULL`) 或 `codex_semantic_inactivity` 时，子任务
  NULL 化 session/work_dir 且 `force_fresh_session=true`。
- **D. squad 自触发** — 归档**不发任何评论**（纯 DB 行），无 leader/agent 唤醒，
  天然无自触发循环。
- **7. DeepSeek/Qwen** — `DefaultCompactor` 优先 Qwen(记忆总管)，无则 DeepSeek，
  再无则 deterministic；任一级模型失败逐级回落，**不丢原文、不中断主流程**。

## 构建 / 测试（go1.26.1 + sqlc v1.31.1，全部跑通）

```
cd server
sqlc generate                                   # 已提交生成结果
go build ./...                                  # ✓ 全量编译通过
go test ./internal/service/memorycompact/       # ✓ 17/17
go test ./internal/service/ ./internal/handler/ ./internal/daemon/  # ✓ 全过，无回归
# 迁移：migrate up 跑到 112；回滚 down 到 111 应干净
```

## 审计 FAIL 返工（03:50 Codex-T02 VERDICT: FAIL → 已补齐）
1. **runtime config 条件化历史** — `execenv` 的 assignment brief（`runtime_config.go` 第3步）
   原写死全量 `comment list ... --output json` mandatory。现 `TaskContextForEnv` 增
   `MemorySummary`，daemon `InjectRuntimeConfig` 注入；非空时第3步改为注入 T1/T2 摘要 +
   “不要默认读全量、必要时逐级下探”，移除 mandatory 全量指令。
2. **chat fresh 摘要注入** — daemon claim 对 compacted chat session 取
   `ChatSessionMemorySummary`(scope=chat_session) 注入 `resp.MemorySummary`；
   `buildChatPrompt` 顶部注入 T1/T2 摘要 + archive 说明，不再只发最新 user message。
3. **task 级归档** — `CompleteTask` 除 `ArchiveIssue` 外，新增 `ArchiveTask` 写
   `scope_type='task'` 记录（trigger 上下文 + 最终输出），fail-safe、不删原文，可经
   archive 索引溯源单次 run。
- 测试：`runtime_config_test`（compacted 无 mandatory 全量 / 未 compacted 保留）、
  `prompt_test`（chat 注入摘要）、`memory_archive_test`（extractTaskOutput / taskResultToMessages）。

## 验收映射

| 验收项 | 覆盖 |
|--------|------|
| chat 超阈值后下次 claim 无 PriorSessionID | `MaybeCompactChatSession` 清 session_id + gate 跳 fallback ✓ |
| issue complete 后归档 + prompt 不要全量 | `CompleteTask` hook + `MemorySummary` 注入 + prompt 收敛 ✓ |
| squad leader 不被 archive 评论自触发 | 归档不发任何评论 ✓ |
| retry 连续失败 fresh session | `CreateRetryTask` parent_task_id 谓词 ✓ |
| 原文不删除可溯源 | memory_archive 仅新增；T4→T1 可下探 ✓ |

> 说明：上表“行为级”验收的端到端断言需带 Postgres 的 CI 集成测试覆盖；本仓库
> 单测覆盖决策/转换逻辑（阈值、窗口过滤、prompt 注入、模型选择/回落、Archiver
> fail-safe），其余由 `go build ./...` 全绿 + 各包套件无回归保证编译期正确。
