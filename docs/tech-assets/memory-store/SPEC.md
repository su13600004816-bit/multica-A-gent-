# 记忆储存（Memory Store）技术说明书 · SPEC

> 归属总任务 **PL-150**；本档对应 **PL-154**。
> 所有源码路径均基于仓库 `multica-A-gent-` 的 `main` 分支逐条核对，真实存在。
> 适用范围:Multica 守护进程（daemon）为每个 agent 任务装配的"记忆/上下文"全链路。

---

## 1. 概述(这套"记忆储存"到底是什么)

Multica 没有一个单独的"记忆数据库"。它的记忆能力由**多条显式、用户可见、可编辑的通道**组成,由 daemon 在每次起任务时按 (agent, issue) 维度装配进 agent 的运行环境。核心理念:**记忆是显式装配的上下文,而不是模型私藏的黑盒**。

记忆储存由四类通道构成:

1. **会话续接记忆(Session continuation)** —— 把上一轮在同一 (agent, issue) 上跑出的 Claude Code 会话 `session_id` 存库,下一轮用 `--resume` 续上,保留整段对话上下文。
2. **显式上下文注入(Context injection)** —— daemon 把 issue 任务信息、技能(skill)、项目资源落成文件写进 agent 工作目录(`.agent_context/` 等),agent 启动即可读。
3. **平台级显式记忆通道** —— issue 描述/评论、issue metadata KV、CLAUDE.md/技能记忆。这些是"长期、可人工编辑"的记忆载体。
4. **Codex 原生记忆的主动屏蔽** —— 主动禁用 Codex CLI 自带的自动记忆子系统,防止跨任务/跨工作空间的隐性泄漏(对应上游 issue #3130)。

> 关键判断:**真正的记忆储存 = 会话续接 + 显式上下文注入 + 平台显式通道**,而 Codex 原生 auto-memory 是被**刻意关掉**的(它不可审计、会泄漏),不属于本系统采纳的记忆方案。

---

## 2. 存储结构与格式

### 2.1 会话续接记忆(落库)

| 项 | 内容 |
|---|---|
| 存储位置 | Postgres 表 `agent_task_queue` 的 `session_id` / `work_dir` 列 |
| 建表迁移 | `server/migrations/020_task_session.up.sql`(新增 `session_id TEXT`、`work_dir TEXT`) |
| 续接控制位 | `server/migrations/066_force_fresh_session.up.sql`(`force_fresh_session`,手动重跑时强制开新会话) |
| 聊天会话侧 | `server/migrations/060_chat_session_runtime_id.up.sql`(chat_session 的 runtime 绑定) |
| 格式 | `session_id`:Claude Code 运行时返回的会话 ID 字符串;`work_dir`:上一轮工作目录路径 |

### 2.2 显式上下文注入(落盘)

| 文件 | 路径(相对 agent 工作目录) | 格式 | 生成函数 |
|---|---|---|---|
| Issue 任务上下文 | `.agent_context/issue_context.md` | Markdown | `renderIssueContext` → `writeContextFiles`(`server/internal/daemon/execenv/context.go`) |
| 项目资源 | agent 工作目录内 JSON(`projectResourceFile`) | JSON(thin pass-through of API) | `writeProjectResources`(同上,`context.go:121`) |
| 技能文件 | skills 目录(`resolveSkillsDir`/`skillsDirPath` 解析;Codex 走 codex-home) | Markdown + frontmatter | `writeSkillFiles`(`context.go:423`) |

`issue_context.md` 内容形态(见 `renderIssueContext`,`context.go:479`):`# Task Assignment` + Issue ID + 触发类型(New Assignment / Comment Reply)+ Quick Start(提示用 `multica issue get` 拉全量)+ 可用技能列表。Quick-create / Autopilot 任务分别走 `renderQuickCreateContext` / `renderAutopilotContext`。

> 写入采用"fail-on-preexist"语义:若 `.agent_context/issue_context.md` 已存在(用户自建或上一轮残留),daemon **拒绝覆盖**并放行——因为运行时简报(CLAUDE.md/AGENTS.md/GEMINI.md)已承载同样的事实。

### 2.3 Codex 记忆屏蔽块(落盘 · 反向)

daemon 在每个任务的 codex-home `config.toml` 写入两段"托管块",关闭 Codex 自动记忆:

- `# BEGIN multica-managed memory-feature ...`:关 `features.memories`
- `# BEGIN multica-managed memory-config ...`:关 `memories.generate_memories`、`memories.use_memories`

源码 `server/internal/daemon/execenv/codex_memory.go`(标记常量见 `multicaMemoryFeatureBeginMarker` 等)。块内根据用户 `config.toml` 是否已有 `[features]`/`[memories]` 表,自适应选择"表内注入"或"根级 dotted-key"两种写法(TOML 不允许重复定义表)。

---

## 3. 读写 / 召回接口

### 3.1 会话续接的读(召回)

- **DB 查询**:`GetLastTaskSession`(`server/pkg/db/queries/agent.sql:357`)—— 取同一 `(agent_id, issue_id)` 最近一条任务的 `session_id`/`work_dir`。接受 `completed` 与 `failed`(失败任务可能已建立真实会话),但**排除已被判"毒化"的终态**(iteration_limit、agent_fallback_message、api_invalid_request、codex_semantic_inactivity)。聊天侧对应 `GetLastChatTaskSession`。
- **调用点(claim handler)**:`server/internal/handler/daemon.go:1346-1396` —— 仅当 `prior.RuntimeID == task.RuntimeID` 时把 `prior.SessionID` 赋给 `resp.PriorSessionID`,并带上 `PriorWorkDir`。`force_fresh_session=true`(手动重跑)时整段跳过。
- **续接执行**:`resp.PriorSessionID` → daemon 侧 `task.PriorSessionID`(`server/internal/daemon/types.go:53`)→ `execOpts.ResumeSessionID`(`server/internal/daemon/daemon.go:2930`)→ 以 `--resume <session_id>` 续接 Claude Code 会话。若续接失败且无新 session,`daemon.go:2981` 处会清空重试。

### 3.2 会话续接的写

- 运行时返回的 `session_id` 落回 `agent_task_queue`;mid-flight 由 `UpdateAgentTaskSession` 提前固定 resume 指针(见 `agent.sql` 注释),保证 daemon 重启/超时中断也不丢在途上下文。

### 3.3 上下文注入的写

- 入口 `writeContextFiles(workDir, provider, ctx, manifest)`(`context.go:36`),由 `server/internal/daemon/execenv/execenv.go:219` 与 `:354` 调用(任务准备阶段)。所有落盘动作记入 `sidecarManifest`(`server/internal/daemon/execenv/sidecar_manifest.go`)以便清理/审计。

### 3.4 Codex 记忆屏蔽的读写

- 入口 `ensureCodexMemoryConfig(configPath, logger)`(`codex_memory.go:280`),由 `server/internal/daemon/execenv/codex_home.go:132` 在装配 codex-home 时调用。逻辑:先 strip 旧托管块与用户指令,再按表结构注入/根级前置 → 写回 `config.toml`。幂等(内容无变化则不写)。

---

## 4. 与 agent 上下文注入的接口(集成点)

| 接口 / 字段 | 位置 | 作用 |
|---|---|---|
| `TaskContextForEnv` | `execenv` 包 | daemon → 落盘渲染的入参(IssueID、TriggerCommentID、AgentSkills、项目资源、Autopilot/QuickCreate 字段等) |
| `PriorSessionID` / `PriorWorkDir` | `server/internal/handler/agent.go:189`、`handler/daemon.go` | claim 响应里回传给 runtime 的续接指针 |
| `ResumeSessionID` | `server/internal/daemon/daemon.go:2930` | 实际传给 Claude Code 的 `--resume` 值 |
| 评论回复提示 | `server/internal/daemon/execenv/reply_instructions.go`(`BuildNewCommentsHint`/`BuildResumedCommentsHint`/`BuildColdCommentsHint`/`BuildCommentReplyInstructions`) | 续接对话时,告诉 agent "聚焦本条评论",防止继承上一轮的 "Done." 状态 |
| 续接防护 | `server/internal/daemon/prompt.go:170` | `PriorSessionID != ""` 时切换提示词,叠加"Focus on THIS comment"护栏 |

---

## 5. 依赖与配置

- **运行时依赖**:Postgres(会话/任务表)、Claude Code runtime(`--resume` 能力)、Codex CLI(被屏蔽记忆的目标)。
- **环境变量**:
  - `MULTICA_CODEX_MEMORY`(`codex_memory.go:MulticaCodexMemoryEnv`)——truthy(`1/true/yes/on`,大小写不敏感)时**保留** Codex 原生记忆(用户自担泄漏风险);默认/其它值=关闭。
- **关键迁移**:`020_task_session`、`060_chat_session_runtime_id`、`066_force_fresh_session`。
- **不修改用户全局配置**:Codex 屏蔽只写**每任务** codex-home 的 `config.toml`,用户全局 `~/.codex/config.toml` 永不改动。

---

## 6. 迁移到新系统的步骤

1. **迁数据库列**:在新系统执行 `agent_task_queue` 的 `session_id`/`work_dir`(迁移 020)及 `force_fresh_session`(迁移 066)、chat_session runtime 绑定(迁移 060)。无这些列则会话续接整条链路失效。
2. **搬续接逻辑**:移植 claim handler 的 prior-session 查询(`GetLastTaskSession`/`GetLastChatTaskSession`,`agent.sql:357`)与 runtime 匹配 + 毒化态排除规则;保留 `force_fresh_session` 旁路。
3. **搬上下文注入**:移植 `execenv` 的 `writeContextFiles` 与 `renderIssueContext` 系列;确认新系统 agent 工作目录约定一致(`.agent_context/issue_context.md`、skills 目录、项目资源 JSON)。
4. **搬 Codex 屏蔽**:若新系统仍用 Codex CLI,必须移植 `ensureCodexMemoryConfig` 及其 TOML 自适应注入,否则重现 #3130 跨工作空间记忆泄漏。新系统若不用 Codex,可整块省略。
5. **保留 resume 防护**:移植 `reply_instructions.go` 与 `prompt.go:170` 的 "Focus on THIS comment" 护栏,否则续接会继承上一轮的完成标记。
6. **核对显式通道**:issue 描述/评论、metadata KV、CLAUDE.md/技能记忆在新平台需有等价载体——它们才是长期记忆的主体。
7. **回归验证**:① 同一 (agent, issue) 二次触发能 `--resume` 续上;② 手动重跑 `force_fresh_session` 确实开新会话;③ Codex 任务 `config.toml` 含两段托管块且 `~/.codex` 未被改;④ `MULTICA_CODEX_MEMORY=1` 能放行原生记忆。

---

## 7. 已知限制

1. **会话续接受 runtime 绑定**:`prior.RuntimeID == task.RuntimeID` 不成立(换了 runtime)时不续接,退化为冷启动——跨 runtime 的对话上下文不保留。
2. **毒化态会丢上下文**:命中 iteration_limit / agent_fallback_message / api_invalid_request / codex_semantic_inactivity 的会话被排除,auto-retry 会丢掉那段对话记忆(这是刻意的防毒化取舍)。
3. **手动重跑必丢记忆**:`force_fresh_session=true` 下完全不读旧会话,是设计取舍(用户已判上一轮产出不可用)。
4. **`issue_context.md` 不覆盖**:若该文件已存在,daemon 拒写——依赖运行时简报(CLAUDE.md/AGENTS.md)承载同样事实,sidecar 副本可能滞后。
5. **项目资源为 best-effort**:写失败仅告警不阻塞起任务,agent 可能"看不到"某资源文件。
6. **Codex 原生记忆被关**:换来隔离与可审计,代价是放弃 Codex 自带的跨轮自动记忆;需要它的用户须显式 `MULTICA_CODEX_MEMORY=1` 并自担泄漏风险。
7. **无统一"记忆库"抽象**:记忆分散在多条通道(DB 会话 / 落盘上下文 / issue 文本 / metadata / 技能),迁移时需逐条对齐,没有单一导出/导入入口。

---

## 附:真实源码索引(逐条可点开核对)

- `server/internal/daemon/execenv/codex_memory.go` —— Codex 记忆屏蔽(`ensureCodexMemoryConfig`、托管块、TOML 自适应注入、`MULTICA_CODEX_MEMORY`)
- `server/internal/daemon/execenv/codex_memory_test.go` —— 屏蔽逻辑测试
- `server/internal/daemon/execenv/context.go` —— 上下文落盘(`writeContextFiles`/`renderIssueContext`/`writeProjectResources`/`writeSkillFiles`)
- `server/internal/daemon/execenv/reply_instructions.go` —— 续接对话的评论护栏
- `server/internal/daemon/execenv/codex_home.go:132` —— 调用 `ensureCodexMemoryConfig`
- `server/internal/daemon/execenv/execenv.go:219,354` —— 调用 `writeContextFiles`
- `server/internal/handler/daemon.go:1346-1396` —— prior-session 查询与续接指针装配
- `server/internal/handler/agent.go:189` —— `PriorSessionID` 字段
- `server/internal/daemon/daemon.go:2930,2981` —— `ResumeSessionID` / 续接失败清空重试
- `server/internal/daemon/prompt.go:170` —— 续接提示词护栏
- `server/internal/daemon/types.go:53` —— `PriorSessionID`
- `server/pkg/db/queries/agent.sql:357` —— `GetLastTaskSession`
- `server/migrations/020_task_session.up.sql` / `060_chat_session_runtime_id.up.sql` / `066_force_fresh_session.up.sql` —— 存储列
