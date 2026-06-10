# 看门狗 强制说明书(SPEC)

| 字段 | 内容 |
|---|---|
| 技术名称 | 看门狗(运行时清扫器 + 守护进程超时 + 触发/路由协议) / watchdog |
| 归档目录 | `docs/tech-assets/watchdog/` |
| 负责线 | T03(本份由 cc 亲自核对真实源码整理) |
| 当前状态 | 已部署 multica 生产;**看门狗(被动定时)与线主脑(主动事件)对同一 issue 无协调并发写,冲突未根治**(本 SPEC 重点) |
| 对应任务 | PL-153 |
| 维护人 | cc |
| 最后更新 | 2026-06-11 |

## 1. 技术原理与作用
本仓库里的"看门狗"由三层真实机制构成,职责是**兜底**——在 agent/任务不正常时把系统从卡死中救回来:

1. **后端运行时清扫器(runtime sweeper)**:服务端 goroutine,每 30s 扫一遍。① 心跳超时判离线运行时并失败其孤儿任务;② 失败卡死任务(dispatched 超时、running 超时);③ 过期清理积压队列;④ 失败任务后把对应 issue 从 `in_progress` 回滚到 `todo`(等于触发重派)。它是**被动定时、粗粒度**的。
2. **守护进程侧超时看门狗(daemon idle/tool watchdog)**:每个 run 内的安全网。后端静默 + 队列空超过 30 分钟 → 强停;单个 tool_call 在飞(已发 tool_use 未回结果)允许静默到 2 小时(给长构建留时间)。配合 15s 心跳上报维持 `last_seen_at` 新鲜。
3. **触发/防刷屏协议**:评论/状态变更如何拉起或抑制 agent run,以及防止 agent 互相 @ 形成死循环的去重与防护。

> 注意范围澄清:本 issue 评论区里那个"🚨看门狗自动分流"(检测 blocked/in_review 滞留并按小队重派的脚本)**不在本代码仓库里**,它是平台外层的编排/调度层(member 账号 `e1c7cb89` 驱动),本仓库无对应源码。详见第 6 节"F"与第 2 节末尾说明。

## 2. 核心源码文件清单(真实路径 · 行号已逐条核对)
> 全部路径/行号已用 grep + 打开核对,审核可逐条点开。

### 2.1 后端运行时清扫器(看门狗主体)
| 文件路径 | 关键行 | 角色 |
|---|---|---|
| `server/cmd/server/runtime_sweeper.go` | L76 `runRuntimeSweeper()` 主循环;L95 `sweepStaleRuntimes()`;L180 `filterStaleRuntimesByLiveness()`(Redis 活性优先,DB 兜底);L212 `gcRuntimes()`;L246 `sweepStaleTasks()`;L270 `sweepExpiredQueuedTasks()`;**L308-314 失败任务→`issue.Status` 由 `in_progress` 改 `todo`** | 看门狗主体 |
| `server/cmd/server/main.go` | L348 `go runRuntimeSweeper(...)` 启动 | 装配点 |
| `server/pkg/db/queries/agent.sql` | L462 `FailStaleTasks`;L478 `ExpireStaleQueuedTasks`;L544 `HasPendingTaskForIssueAndAgent` | SQL 查询 |

**关键配置常量**(`runtime_sweeper.go` L19-63,改值需重新部署):
`sweepInterval=30s`、`staleThresholdSeconds=150s`、`offlineRuntimeTTLSeconds=7天`、`dispatchTimeoutSeconds=300s`、`runningTimeoutSeconds=9000s`、`queuedTTLSeconds=2h`、`queuedExpireBatchSize=500`。

### 2.2 守护进程侧超时看门狗
| 文件路径 | 关键行 | 角色 |
|---|---|---|
| `server/internal/daemon/config.go` | L20 `DefaultHeartbeatInterval=15s`;L26 `DefaultAgentTimeout=0`(关闭绝对墙钟);L40 `DefaultAgentIdleWatchdog=30m`;L50 `DefaultAgentToolWatchdog=2h`;L297/L304 支持 `MULTICA_AGENT_IDLE_WATCHDOG`/`MULTICA_AGENT_TOOL_WATCHDOG` 环境变量覆盖 | 阈值配置 |
| `server/internal/daemon/daemon.go` | L3483 `runIdleWatchdog()` 空闲/工具超时判定;L3457 `idleWatchdogReason()` 失败文案;L3234 `go d.runIdleWatchdog(...)` 每 run 起一只;L1279 `heartbeatLoop()`;L1325 `runRuntimeHeartbeat()` | run 内安全网 + 心跳 |

### 2.3 触发 / 路由 / 防刷屏协议
| 文件路径 | 关键行 | 角色 |
|---|---|---|
| `server/internal/handler/comment.go` | L936 `noteCommentPrefix="/note"`;L942 `isNoteComment()`;L951 `triggerTasksForComment()`(评论三路触发总闸:assignee / squad leader / @mentions);L1112 `enqueueMentionedAgentTasks()`;L1151/L1186 `HasPendingTaskForIssueAndAgent` 去重 | 评论触发 + 防刷屏闸门 |
| `server/internal/handler/squad.go` | L917 `shouldEnqueueSquadLeaderOnComment()`;L967 `lastTaskWasLeader()` 自触发防护;L983 `commentMentionsAnyone()` 守卫;L998 `shouldEnqueueSquadLeaderOnAssign()`;L1035 `enqueueSquadLeaderTask()`;L1050 `HasPendingTaskForIssueAndAgent` 去重 | 线主脑派发决策 |
| `server/internal/handler/issue.go` | L2203 `UpdateIssue()`;L2421 `CancelTasksForIssue`+L2424 `EnqueueTaskForIssue`/L2430 `enqueueSquadLeaderTask`;L2445-2451 backlog→活跃 唤醒触发;L2689 `BatchUpdateIssues()` 镜像逻辑(L2915-2935) | 赋值/状态触发 |
| `server/internal/handler/issue_child_done.go` | L51 `notifyParentOfChildDone()`;L246 `dispatchParentAssigneeTrigger()`;L272 `triggerChildDoneAgent()`;L304 `triggerChildDoneSquad()` 共享 leader 防环 | 子任务完成触发 |
| `server/internal/util/mention.go` | L16 `MentionRe`(`mention://type/id`);L24 `ParseMentions()`;类型 member/agent/squad/issue/all | mention 解析 |
| `server/internal/service/task.go` | L433 `EnqueueTaskForIssue()`;L500 `EnqueueTaskForMention()`;L510 `EnqueueTaskForSquadLeader()`;L743 `CancelTasksForIssue()` | 任务入队/取消 |

**调用关系/数据流**
- 清扫器:`time.Ticker(30s)` → `sweepStaleRuntimes`(心跳过期→失败孤儿任务)+ `sweepStaleTasks`(dispatched/running 超时→失败)+ `sweepExpiredQueuedTasks`(queued 过期),失败任务的 issue 从 `in_progress` 回滚 `todo`(L314)→ 下一个事件触发重派。
- 守护进程:每 run 起 `runIdleWatchdog`,监听 `lastActivityAt` 与 `inFlightTools`;静默超阈值且消息队列空 → `cancel()` 强停,写失败原因。
- 触发:评论 → `triggerTasksForComment` 三路;`/note` 前缀 → `isNoteComment` 命中即整条不触发;每路入队前 `HasPendingTaskForIssueAndAgent` 去重(只看 queued/dispatched)。

## 3. 对外接口 / 触发协议
- **清扫器**:无对外 API,纯后台 goroutine 定时循环;靠 DB(agent_task_queue/agent_runtime/issue)+ Redis liveness 工作。
- **守护进程看门狗**:无对外 API;阈值由 `config.go` 常量 + `MULTICA_AGENT_IDLE_WATCHDOG`/`MULTICA_AGENT_TOOL_WATCHDOG`/`MULTICA_AGENT_TIMEOUT`/`MULTICA_DAEMON_HEARTBEAT_INTERVAL` 环境变量控制。
- **触发协议(评论侧)**:`POST 评论` → `triggerTasksForComment()` 分流到 assignee agent / squad leader / @mention agents。
  - **`/note` 防刷屏**:评论首 token 为 `/note`(`isNoteComment`,大小写不敏感)→ 整条评论不拉起任何 run。这是当前仓库里**唯一**的"留痕不触发"机制(提交 `dfc159e1`,MUL-3115)。
  - **`suppress_triggers` / CLI `--no-trigger`:本仓库不存在。** grep 全仓 `suppress_triggers`/`suppressTriggers`/`--no-trigger`/`noTrigger`/`skipTrigger` 均无命中。需要"可见不唤醒"时只能用 `/note`。此条为已知缺口,迁移/新系统若要做应作为新功能实现。

## 4. 依赖 / 环境变量 / 配置
- **依赖**:PostgreSQL(`agent_task_queue` / `agent_runtime` / `issues`);Redis(运行时活性 liveness,权威优先于 DB,Redis 不可用时 `filterStaleRuntimesByLiveness` 退回 DB)。
- **环境变量(守护进程)**:`MULTICA_DAEMON_HEARTBEAT_INTERVAL`(默认 15s)、`MULTICA_AGENT_IDLE_WATCHDOG`(默认 30m)、`MULTICA_AGENT_TOOL_WATCHDOG`(默认 2h)、`MULTICA_AGENT_TIMEOUT`(默认 0=关闭)。
- **配置项/默认值**:清扫器阈值见第 2.1 节常量表(编译期常量,改需重部署)。
- **运行前置**:后端进程常驻(`runRuntimeSweeper` 在 `main.go:348` 起);至少一个在线 daemon 上报心跳;Redis/Postgres 可达。

## 5. 迁移到新系统的步骤
1. **要带走的文件**:`server/cmd/server/runtime_sweeper.go`(主体)、`server/internal/daemon/{config.go,daemon.go}` 的 idle/tool 看门狗与心跳段、`server/internal/service/task.go`(入队/取消/失败重派)、`server/pkg/db/queries/agent.sql`(`FailStaleTasks`/`ExpireStaleQueuedTasks`/`HasPendingTaskForIssueAndAgent`)、触发链 `comment.go`/`squad.go`/`issue.go`/`issue_child_done.go`/`util/mention.go`。
2. **依赖当前系统的强耦合点**:
   - ① 绑定 `agent_task_queue` 状态机(queued→dispatched→running→completed/failed)与 `issues` 状态机(backlog/todo/in_progress/in_review/done/blocked/cancelled);
   - ② 依赖 Redis liveness 作为活性权威源;
   - ③ 与 issue 生命周期(`issue.go`)、squad 触发(`squad.go`)深度交织,失败回滚直接写 `issues.status`。
3. **需要重写的胶水代码**:失败后回滚+重派逻辑绑定具体表名/列;新系统须先有等价 task queue 与 issue 状态机,再写适配层。`/note` 抑制是 handler 层硬编码,需在新评论入口重接。
4. **迁移步骤**:移植任务/issue 状态机 → 接 Redis liveness(或等价心跳源)→ 移 sweeper 循环 + daemon 看门狗 → **连同第 6 节三项加固一起带走**(否则冲突重现)→ 跑第 7 节回归 → 压测验证不出现重派风暴。
5. **风险/坑**:看门狗与编排器**双写 issue 状态**是命门(第 6 节 A);迁移时务必带"状态版本/CAS 校验 + 跨生命周期去重锁 + 触发路径隔离"三件套,否则换个系统冲突照样复现。

## 6. 已知 BUG / 限制 / 坑 —— 看门狗 ↔ 线主脑冲突(核心)
**一句话根因**:看门狗(被动定时、粗粒度)与线主脑(主动事件、细粒度)对同一 issue/task **无协调地并发读写**,叠加去重只看部分状态、自触发防护未覆盖全路径,产生状态互覆盖与重派死循环。

| 编号 | 现象 | 触发条件 | 影响 | 绕法/状态 |
|---|---|---|---|---|
| **A 状态互覆盖** | 看门狗判 task 超时把 issue 写回 `todo`(`runtime_sweeper.go:314`),同时 leader 完工经 `UpdateIssue`(`issue.go:2203`)写 `done`,后写覆盖先写 | 超时与完工并发 | issue 终态被错误回滚或反复 | 缺状态版本/CAS 校验,**未根治** |
| **B 评论触发死循环放大** | 去重只看 `(queued,dispatched)`(`squad.go:1050` / `agent.sql:544`),task 完成后同一评论可再次触发 → leader→@agent→run→leader 风暴 | 评论链 + 完工后重触发 | 重派风暴、刷屏、烧 token | 提交 `021fcf83`(单次执行锁)缓解;根治应改 `(trigger_comment_id, agent_id)` 跨生命周期唯一锁 |
| **C 赋值+取消竞争** | `UpdateIssue` 先 `CancelTasksForIssue`(`issue.go:2421`)再 `enqueueSquadLeaderTask`(L2430),与 sweeper 失败 dispatched task(`runtime_sweeper.go:246`)竞争 | 赋值瞬间撞上清扫 tick | 任务被取消/重建错乱 | **未根治** |
| **D 自触发防护不全** | 自触发防护 `lastTaskWasLeader` 只在评论路径(`squad.go:967`),**assign 路径 `shouldEnqueueSquadLeaderOnAssign`(L998)无对应防护** | leader 被赋值到本队 issue | leader 自建并自派给本队=自我派发 | 路径未隔离,**未根治** |
| **E mention 绕过路由→死循环** | 个人 `mention://agent` 绕过小队路由,看门狗重派叠加 → 死循环;`8c0418e5` 已抑制"队内个人 @agent 拉起 leader" | 评论里点名个人 agent | 绕过小队派工铁律、循环重派 | 提交 `8c0418e5` 部分修;回归审计见 PL-146/PL-149,**临时协议:禁止个人 mention 派工** |
| **F 外层"自动分流"脚本不在本仓库** | 评论区 🚨"看门狗自动分流"是平台外层调度(member `e1c7cb89`),检测 blocked/in_review 滞留按小队重派;**本代码仓库无对应源码**,无法在 repo 内核对/改阈值 | blocked/in_review 滞留超时 | 同一 issue 反复被外层重新路由,与本仓库触发链叠加放大刷屏 | 限制/缺口:外层逻辑需单独建档或纳入仓库 |

**根治方向(三维加固,迁移必带)**:
1. **全状态去重锁**:评论 ×(agent/leader)跨整个生命周期唯一(不止 queued/dispatched),堵 B。
2. **路径隔离 + 补防护**:assign 与 comment 各自维护自触发防护,给 assign 路径补 `lastTaskWasLeader` 等价检查,堵 D。
3. **看门狗友好的状态写**:issue 状态更新加版本号/CAS 校验,防陈旧定时数据覆盖新的事件态,堵 A、C。

**相关提交**(均已核对存在):`021fcf83` add trigger-comment single-execution lock;`8c0418e5` suppress in-squad personal @agent mention dispatch;`dfc159e1` skip agent triggering on /note-prefixed comments。**相关任务**:PL-137/142/143/145/146/149。

## 7. 验证方法
- **构建门禁**:本次为纯文档归档,无代码改动,不涉及 tsc/build。如改动看门狗代码,门禁为:
  `cd server && go build ./... && go vet ./...`;
  `go test ./cmd/server/ -run Sweep`、`go test ./internal/handler/ -run 'Squad|Trigger|Comment'`、`go test ./internal/daemon/ -run Watchdog`。
  (注:本归档环境无 `go` 工具链,上述命令需在带 Go 的构建机/CI 执行。)
- **冲突回归(对照第 6 节)**:
  1. 造 leader→@agent→leader 回环,确认完工后同一评论不再无限重派(B);
  2. 造 task 超时与 leader 完工并发,确认 issue 终态不被 `todo` 覆盖(A);
  3. leader 被赋值到本队,确认无自我派发(D);
  4. 个人 mention 场景,确认不绕过小队路由(E,PL-146)。
- **生产取证**:`squad-ops logs multica-后端`;`squad-ops db-ro "SELECT status,count(*) FROM agent_task_queue GROUP BY 1"` 看是否异常积压/反复重派;`squad-ops status` 看运行时在线/心跳。
- **守护进程看门狗验证**:设 `MULTICA_AGENT_IDLE_WATCHDOG=1m` 造静默 run,确认 1 分钟后被强停且失败原因含空闲阈值文案(`idleWatchdogReason`)。

---
<!-- 自检:7 节已填,路径/行号已逐条核对真实,BUG 节诚实(含 suppress_triggers 缺失、外层分流脚本不在仓库两项缺口),验证可复现。 -->
