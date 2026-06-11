# 记忆储存（memory-store · 全局四层）· 独立技术储备说明书

> **独立体**：四层记忆归档 + LLM 压缩器是后端独立子系统。`package/` 含可搬运的压缩器包与归档服务及建表迁移；woven-in 的接入点在第 5 节用真实 file:line 标注。
> 维护人：总管。最后更新：2026-06-11。

## 0. 一句话
把一条 (issue/task/chat) 的历史**分四层（T1 最精简 → T4 最全）压缩归档**进 `memory_archive` 表，新会话只读摘要不读全史 → 省 token、治"一分心就断"；压缩优先用 LLM（DeepSeek/Qwen），失败回退确定性算法。

## 1. 三块机制
1. **四层归档（本技术核心）**：`memory_archive(scope_type, scope_id, level, content, generator, ...)`；scope=issue/task/chat_session，level=T1..T4。每个完成事件压一遍。
2. **会话续接**：`agent_task_queue.session_id/work_dir`，同 (agent,issue) 下一轮 `--resume` 续上整段对话（迁移 020_task_session）。
3. **上下文注入**：`writeContextFiles` 把 issue/项目/技能写进 agent 工作目录；Codex 走记忆屏蔽块。

## 2. 本包文件（package/，可搬运）
| 文件 | 作用 |
|---|---|
| `package/compactor.go` | 压缩器抽象：`DeterministicCompactor`（无网络兜底）+ `ModelCompactor`（包 LLM 客户端，失败回退）；四层 budget |
| `package/qwen.go` | `DefaultCompactor()` 选择逻辑 + Qwen(DashScope)/DeepSeek 的 OpenAI 兼容 HTTP 客户端（`NewQwenClientFromEnv`/`NewDeepSeekClientFromEnv`） |
| `package/archive.go` | 归档落库帮助 |
| `package/memory_archive.go` | `MemoryArchiveService`：算四层、查上轮摘要、写库 |
| `package/112_memory_archive.up/down.sql` | 建/删 `memory_archive` 表（含 scope_type/scope_id/level/generator 列） |

## 3. 依赖 / 环境变量
- Go 标准库 + 项目内 `db`/`pgtype`；LLM 客户端用 `net/http`（OpenAI 兼容 /chat/completions）。
- **环境变量（决定是否启用 LLM 压缩）**：`DASHSCOPE_API_KEY`(+`DASHSCOPE_BASE_URL`/`QWEN_MODEL`) 或 `DEEPSEEK_API_KEY`(+`DEEPSEEK_BASE_URL`/`DEEPSEEK_MODEL`)。都不设 → 退化为 `deterministic`。
- 会话续接/上下文注入额外依赖迁移 020_task_session、060、066（在主仓）。

## 4. 对外接口
- `memorycompact.DefaultCompactor()` → Qwen / DeepSeek / Deterministic（按 env 自动选）。
- `Compactor.Compact(ctx, Input) (Levels, error)` → 四层结果；generator 标 `deepseek-chat` / `mixed:<model>+deterministic` / `deterministic`。
- `MemoryArchiveService` 写 `memory_archive`；新会话经 prompt 注入 `MemorySummary`。

## 5. 接入点（woven-in，迁移时要接的真实位置）
- 归档触发：issue/task 完成、chat 阈值 → 调 `MemoryArchiveService`。
- claim 时读上轮：`server/internal/handler/daemon.go`（prior-session 查询）+ `agent.sql:GetLastTaskSession`。
- 续接执行：`server/internal/daemon/daemon.go` 以 `--resume <session_id>`。
- 摘要注入 prompt：`server/internal/daemon/prompt.go`（chat 块）+ `execenv/runtime_config.go`（issue 上下文 step3，有摘要走摘要、否则读全史）。
- claim 响应字段：`handler/agent.go` 的 `MemorySummary`/`MemorySummaryScope`/`ResumeBlockedReason`。

## 6. 迁移 / 接入新系统
1. 复制 `package/` 的 compactor 包到新系统的服务层；建 `memory_archive` 表（跑 112 迁移）。
2. 在新系统的「任务完成/会话阈值」处调 `MemoryArchiveService` 写四层归档。
3. 在「新会话起 prompt」处把 T1/T2 摘要注入（见第 5 节 prompt.go 模式）。
4. 配 `DEEPSEEK_API_KEY` 或 `DASHSCOPE_API_KEY` 启用 LLM 压缩（不配则 deterministic 也能用）。
5. 会话续接需新系统有等价的 (agent,issue,session_id) 模型 + resume 能力。

## 7. 同步
- 本包 SSOT = multica 后端 PL-91 实现（已部署 `multica-backend:pl91mem-20260611`）。
- multica 内若改进压缩器 → 同步回本包；新系统从本包取。`diff package/compactor.go <目标副本>`。

## 8. 改进指南
- 换模型：改 `DEEPSEEK_MODEL`/`QWEN_MODEL` env，或在 `qwen.go` 加新 provider 客户端（实现 `ModelClient` 接口即可）。
- 调四层粒度：`compactor.go` 的 `modelT1..T4MaxRunes` budget。
- chat_session 归档当前较少触发，可加阈值（PL-91 已有 `GetChatSessionWindowSize`）。

## 9. 删除 / 卸载
1. 停止调用 `MemoryArchiveService`（归档触发点移除）；prompt 注入点恢复读全史。
2. 移除 `DEEPSEEK_API_KEY`/`DASHSCOPE_API_KEY` env。
3. 可选：跑 `112_memory_archive.down.sql` 删表（**会丢历史归档，慎用**；只整理不删除原则下建议保留表）。
4. 会话续接列 session_id/work_dir 可保留（无害）。

## 10. 验证方法（真证据）
- `go build ./...` 过；`go test ./internal/service/memorycompact/...` 过。
- 部署后查 `SELECT level,generator FROM memory_archive ORDER BY created_at DESC` → generator 应为 `deepseek-chat`（配了 key）而非 `deterministic`。
- **已实测（2026-06-11）**：部署后 1 分钟内新归档 generator=`deepseek-chat`，四层 T1-T4 落库。

## 11. 当前 multica 部署状态
- 已上线：`multica-backend:pl91mem-20260611`（= 线上后端线 hotfix/squad-terminal-gate + PL-91 记忆，保留 suppress+termgate 抑制）；配了 `DEEPSEEK_API_KEY`，generator 实测 `deepseek-chat`。
- 合并源沙箱：`/home/fleet/wt-mem-int`；记忆功能分支 `pl91-memory-archive-main`。
