# 记忆储存 强制说明书(SPEC)

| 字段 | 内容 |
|---|---|
| 技术名称 | 记忆储存(会话续接 + 显式上下文注入) / memory-store |
| 归档目录 | `docs/tech-assets/memory-store/` |
| 负责线 | T03(本份由 cc 亲自整理) |
| 当前状态 | 已部署 multica 生产 |
| 对应任务 | PL-154 |
| 维护人 | cc |
| 最后更新 | 2026-06-11 |

> ⚠️ **关键事实**:multica 的记忆是**多通道、显式、用户可见**模型,**不是向量库/隐式自动记忆**。反而**主动禁用** Codex 原生自动记忆(防跨工作空间泄漏)。连续性靠两条腿:① **PriorSessionID** 续接 provider 会话;② **显式上下文注入**(issue/评论/skills/上下文文件)。

## 1. 技术原理与作用
让 agent 跨多次执行保持记忆而不泄漏、可审计:
- **会话级**:DB 记录每个 issue/chat 最后一次成功的 provider `session_id` + `work_dir`,下次同 runtime 续接(`ResumeSessionID`)。
- **上下文级**:每任务隔离地把 issue 上下文、新评论提示、skills、项目资源写进工作目录文件并注入 prompt。
- **安全**:禁用 Codex 全局 `~/.codex/memories`,杜绝跨工作空间串记忆。

## 2. 核心源码文件清单(真实路径)
| 文件路径 | 角色 | 说明 |
|---|---|---|
| `server/internal/daemon/execenv/codex_memory.go` | 核心 | `ensureCodexMemoryConfig()` 禁用 Codex 原生记忆;`codexMemoryEnabled()` 读 `MULTICA_CODEX_MEMORY` |
| `server/internal/daemon/execenv/context.go` | 核心 | `writeContextFiles()` 写 `.agent_context/issue_context.md`、skills、`.multica/project/resources.json`;`ensureSkillFrontmatter()` |
| `server/internal/daemon/execenv/execenv.go` | 核心 | `Prepare()` / `Reuse()` / `Cleanup()` 执行环境生命周期 |
| `server/internal/daemon/execenv/codex_home.go` | 核心 | L54-137 `prepareCodexHomeWithOpts()` 隔离 CODEX_HOME(symlink sessions、copy config) |
| `server/internal/daemon/execenv/reply_instructions.go` | 核心 | 三层记忆提示:`BuildNewCommentsHint()`(热)/`BuildResumedCommentsHint()`(续)/`BuildColdCommentsHint()`(冷);`BuildCommentReplyInstructions()` |
| `server/internal/daemon/execenv/runtime_config.go` | 核心 | L163 `InjectRuntimeConfig()` 写 CLAUDE.md/AGENTS.md/GEMINI.md,含同一套三层决策树(单一来源,PR #2816) |
| `server/internal/daemon/prompt.go` | 入口 | L168-177 构建 prompt 时调三层提示 |
| `server/internal/daemon/daemon.go` | 调用 | `runTask()` 传 `PriorSessionID`;会话恢复失败则重起新会话 |
| `server/internal/handler/daemon.go` | 声称流程 | ClaimTask 时查 `GetLastIssueTaskSession()`/`GetLastChatTaskSession()`,仅同 `runtime_id` 才回填 PriorSessionID |
| `server/pkg/db/queries/chat.sql` | 数据 | L105-125 `GetLastChatTaskSession`、`UpdateChatSessionSession`(COALESCE 防空覆盖) |
| `server/pkg/agent/codex.go` | 执行 | 用 `ResumeSessionID = task.PriorSessionID` 拉起 agent |

**调用链**:ClaimTask → 查最后成功 session(同 runtime)→ 回填 `PriorSessionID/PriorWorkDir` → `runTask()` → `Prepare()/Reuse()`(`prepareCodexHomeWithOpts` 内 `ensureCodexMemoryConfig` 禁原生记忆 + `writeContextFiles`)→ `BuildPrompt()`(三层提示)→ `InjectRuntimeConfig()`(同源决策树写进 CLAUDE.md)→ `agent.Run(ResumeSessionID)`。

## 3. 对外接口
| 函数 | 入参 | 出参 | 说明 |
|---|---|---|---|
| `Prepare(params, logger)` | Task(含 PriorSessionID/WorkDir)、Provider | `*Environment` | 新建执行环境 |
| `Reuse(params, logger)` | WorkDir、刷新 Task | `*Environment`/nil | 复用现有目录 |
| `InjectRuntimeConfig(workDir, provider, ctx)` | 工作目录、provider、上下文 | `(配置文件路径, error)` | 写运行时配置 |
| `BuildNewCommentsHint(issueID, triggerCommentID, triggerThreadID, since, count)` | — | string | 热启动新评论指针 |
| `BuildResumedCommentsHint(...)` / `BuildColdCommentsHint(...)` | — | string | 续接/冷启动提示 |
| `ensureCodexMemoryConfig(configPath, logger)` | per-task config.toml | error | 写禁用块 |
| SQL `GetLastChatTaskSession`/`GetLastIssueTaskSession` | session/issue id | session_id,work_dir,runtime_id | 会话恢复指针 |

## 4. 依赖 / 环境变量 / 配置
- **依赖**:Go 标准库;PostgreSQL(session_id/work_dir 持久化);TOML 解析(Codex config)。无向量库。
- **环境变量**:`MULTICA_CODEX_MEMORY`(truthy 才保留 Codex 原生记忆,默认禁用);`CODEX_HOME`(未设回退 `~/.codex`)。
- **per-task config.toml 注入块**(daemon 管理,勿手改):`features.memories=false`、`memories.generate_memories=false`、`memories.use_memories=false`;幂等。
- **存储结构**:`chat_session`(session_id/work_dir/runtime_id/...)、`agent_task_queue`(session_id/work_dir/runtime_id/status/failure_reason);文件型:`.agent_context/issue_context.md`、SKILL.md(YAML frontmatter,`name` 必填)。

## 5. 迁移到新系统的步骤
1. **带走的文件**:`execenv/` 整包(codex_memory/context/execenv/codex_home/reply_instructions/runtime_config)、`prompt.go`、`chat.sql` 的 session 查询、`codex.go` 的 ResumeSessionID 用法。
2. **强耦合点**:① 依赖 `chat_session`/`agent_task_queue` 表与 `runtime_id` 概念;② 依赖 daemon/ClaimTask 的任务声称流程;③ provider 专属(Codex config.toml 路径、CLAUDE.md/AGENTS.md/GEMINI.md 多 provider 落点)。
3. **需重写的胶水**:会话回填条件(同 runtime 才续)绑定具体表;新系统须有等价 session 指针存储。
4. **迁移步骤**:建 session 字段 → 移 execenv 包 → 接新系统 ClaimTask 回填 PriorSessionID → 移三层提示与配置注入(保持单一来源,prompt 与配置文件同调)→ 验证续接与禁用。
5. **风险/坑**:**单一来源原则**——prompt 与运行时配置两处必须调同一组 `Build*Hint()`,否则漂移(PR #2816 教训);跨 runtime 不可续接(防不兼容);Windows PowerShell 非 ASCII 编码坑(用 `--content-file`)。

## 6. 已知 BUG / 限制 / 坑
| 现象 | 触发条件 | 影响 | 绕法/状态 |
|---|---|---|---|
| 跨工作空间记忆泄漏 | Codex 原生记忆写全局 `~/.codex/memories` | 不相关工作空间串记忆 | 已禁用(`codex_memory.go`,issue #3130);逃生阀 `MULTICA_CODEX_MEMORY=1`(自担风险) |
| Windows 中文变 `?` | PS5.1 ASCII 管道 | 评论内容损坏→重试循环 | Windows 强制 `--content-file`(UTF-8),Unix 用 quoted HEREDOC(#2198/2236/2376) |
| 提示双表面漂移 | prompt 与配置文件各写各的 | 行为不一致 | 单一来源,二者同调 Build*Hint(PR #2816) |
| 会话恢复失败 | PriorSessionID 有但 result 无 | — | 自动重起新会话;`UpdateChatSessionSession` 用 COALESCE 不覆盖好指针 |
| 仅取最后一条会话 | 设计 | 无多分支历史 | 设计选择,简化 |
| 无向量/无自动摘要 | 设计 | 靠 agent 主动拉历史 | 设计选择 |

## 7. 验证方法
- **构建门禁**:`cd server && go build ./... && go vet ./...`;`go test ./internal/daemon/execenv/ -run 'Memory|Codex|Hint'`(含 `codex_memory_test.go` 幂等与 TOML 兼容)。
- **功能验证**:① 同一 issue 连续两次触发,第二次确认 agent 续接前次会话(看日志 `prior_session`);② 检查 per-task `config.toml` 含三个禁用块;③ 跨工作空间验证不串记忆;④ Windows 端中文评论不乱码。
