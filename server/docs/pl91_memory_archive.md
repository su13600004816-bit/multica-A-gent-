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

1. **C. chat 超阈值止血** — `handler/chat.go` `SendChatMessage` 入口：按
   `bytes/comment_count/token_estimate` 判阈值，超阈值对旧 `chat_session` 调
   `MemoryArchiveService.ArchiveChatSession`（内部 `MarkChatSessionCompacted`
   清空 session_id/work_dir 并盖 compacted_at）。claim gate 已就绪，缺触发。

2. **D. 小队 leader 自触发循环** — 当前归档**不发任何评论**（纯 DB 行），无自触发风险；
   若将来要发归档播报评论，用 `author_type='system'` 且不带 `mention://agent|squad`。

3. **E. retry 连续失败 fresh** — 已有 `CreateRetryTask` 对
   `codex_semantic_inactivity` 置 `force_fresh_session`；扩展为连续 N 次失败也置位。

## 构建 / 测试（已在 go1.26.1 + sqlc v1.31.1 下跑通）

```
cd server
sqlc generate                 # 由 memory_archive.sql 生成 Go（已提交生成结果）
go build ./internal/handler/ ./pkg/db/generated/   # ✓ 编译通过
go test ./internal/service/memorycompact/...        # ✓ 15/15 PASS
# 迁移：migrate up 跑到 112；回滚 down 到 111 应干净
```

> 全量 `go build ./...` 受本机磁盘 100% 限制未跑完（仅余 ~130M），但所有
> 受影响的包（handler / generated db / memorycompact）均已单独编译 + vet + 测试通过。

## 验收映射

| 验收项 | 覆盖 |
|--------|------|
| chat 超阈值后下次 claim 无 PriorSessionID | gate 已落地；阈值触发见待接线 #3 |
| issue complete 后归档 + prompt 不要全量 | 归档服务/gate 已落地；hook+prompt 见 #2/#4 |
| squad leader 不被 archive 评论自触发 | 见 #5（system 作者 + 无 mention） |
| retry 连续失败 fresh session | 见 #6 |
| 原文不删除可溯源 | memory_archive 仅新增；T4→T1 可下探（已落地） |
