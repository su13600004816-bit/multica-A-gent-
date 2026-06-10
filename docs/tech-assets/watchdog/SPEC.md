# 看门狗 强制说明书(SPEC)

| 字段 | 内容 |
|---|---|
| 技术名称 | 看门狗(运行时清扫器/任务路由守护) / watchdog |
| 归档目录 | `docs/tech-assets/watchdog/` |
| 负责线 | T03(本份由 cc 亲自整理) |
| 当前状态 | 已部署 multica 生产;**与线主脑(squad leader)存在冲突,未根治**(本 SPEC 重点) |
| 对应任务 | PL-153 |
| 维护人 | cc |
| 最后更新 | 2026-06-11 |

## 1. 技术原理与作用
看门狗 = 后端定时清扫器(runtime sweeper),每 30s 扫一遍,负责:① 检测离线运行时并失败其任务;② 失败卡死任务(dispatched 超时、running 超时);③ 清理积压队列;失败后回滚 issue 状态并触发自动重派。它是**被动定时**的;线主脑是**主动事件**的——两者对同一 issue 的写入无协调,是冲突根源。

## 2. 核心源码文件清单(真实路径 · 行号)
| 文件路径 | 关键行 | 角色 |
|---|---|---|
| `server/cmd/server/runtime_sweeper.go` | L19-63 配置;L76 `runRuntimeSweeper()`;L95 `sweepStaleRuntimes()`;L180 `filterStaleRuntimesByLiveness()`(Redis 活性优先);L246 `sweepStaleTasks()`;L270 `sweepExpiredQueuedTasks()`;L310-316 失败任务→issue=todo | 看门狗主体 |
| `server/internal/handler/issue.go` | L2203-2452 `UpdateIssue()`;L2419-2431 取消旧 task+派 leader;L2444-2451 backlog→todo 唤醒 | 赋值/状态触发 |
| `server/internal/handler/comment.go` | L951-975 `triggerTasksForComment()`;L942-948 `isNoteComment()` 防刷屏 | 评论触发 |
| `server/internal/handler/squad.go` | L917 `shouldEnqueueSquadLeaderOnComment()`;L931-939 自触发防护 `lastTaskWasLeader()`;L947 `commentMentionsAnyone()` 守卫;L998 `shouldEnqueueSquadLeaderOnAssign()`;L1034 `enqueueSquadLeaderTask()`;L1049-1056 dedup `HasPendingTaskForIssueAndAgent` | 线主脑派发决策 |
| `server/internal/handler/issue_child_done.go` | L51-351 `dispatchParentAssigneeTrigger()`;L304-329 共享 leader 防环 | 子任务完成触发 |
| `server/internal/service/task.go` | L430-545 `EnqueueTaskFor*`;L743 `CancelTasksForIssue()` | 任务队列 |
| `server/pkg/db/queries/agent.sql` | L540-560 `HasPendingTaskForIssueAndAgent` 等 | 去重查询 |

**关键配置**(`runtime_sweeper.go`):`sweepInterval=30s`、`staleThresholdSeconds=150s`、`dispatchTimeoutSeconds=300s`、`runningTimeoutSeconds=9000s`、`queuedTTLSeconds=2h`。

## 3. 对外接口 / 触发协议
- 看门狗无对外 API,是后台 goroutine 定时循环。
- 触发协议(评论侧):评论 → `triggerTasksForComment()` 三路(assignee agent / squad leader / @mentions);`/note` 前缀经 `isNoteComment()` 完全不触发(防刷屏)。
- `suppress_triggers` / `--no-trigger`:可见不唤醒协议(参 PL-145),让评论留痕但不拉起 run。

## 4. 依赖 / 环境变量 / 配置
- PostgreSQL(agent_task_queue / agent_runtime / issue);Redis(运行时活性 liveness,权威优于 DB)。
- 定时器:`time.Ticker`,周期 `sweepInterval`。
- 阈值均为 `runtime_sweeper.go` 内常量,改需重部署。

## 5. 迁移到新系统的步骤
1. **带走的文件**:`runtime_sweeper.go`,以及它依赖的 `task.go`(失败/取消/重派)、`agent.sql` 去重查询。
2. **强耦合点**:① 依赖 agent_task_queue 状态机(queued/dispatched/running/completed/failed);② 依赖 Redis liveness;③ 与 issue 生命周期(issue.go)和 squad 触发(squad.go)深度交织。
3. **需重写的胶水**:失败后回滚+重派的逻辑绑定具体表;新系统须先有等价的 task queue 与 issue 状态机。
4. **迁移步骤**:移植任务状态机 → 接 Redis liveness → 移 sweeper 循环 → **连同第6节三项加固一起带走**(否则冲突重现)→ 压测验证。
5. **风险/坑**:看门狗与编排器双写 issue 状态是命门,见第6节;迁移时务必带"状态版本校验 + 全状态去重锁 + 触发路径隔离"。

## 6. 已知 BUG / 限制 / 坑 —— 看门狗 ↔ 线主脑冲突(苏总两天未解的核心)
**一句话根因**:看门狗(被动定时、粗粒度)与线主脑(主动事件、细粒度)对同一 issue/task 无协调地并发读写,叠加 dedup 不足与角色未隔离,产生状态互覆盖与重派死循环。

| 冲突点 | 机制 | 代码位置 | 状态 |
|---|---|---|---|
| **A 状态互覆盖** | 看门狗判 task 超时→`issue=todo`,同时 leader 完工→`issue=done`,后写覆盖先写 | `runtime_sweeper.go:310-316` ↔ `issue.go:2367` | 缺状态版本校验,未根治 |
| **B 评论触发死循环放大** | dedup 只看 `(queued,dispatched)`;task 完成后同一评论可再次触发,leader→@agent→run→leader 风暴 | `squad.go:1049-1056` / `agent.sql` | commit `021fcf83` 修(应改全状态锁 `(trigger_comment_id, agent_id)` 唯一) |
| **C 赋值+取消竞争** | `UpdateIssue` 先 `CancelTasksForIssue` 再 `enqueueSquadLeaderTask`,与 sweeper 失败 dispatched task 竞争 | `issue.go:2420-2431` ↔ `runtime_sweeper.go:246` | 未根治 |
| **D 自触发防护不全** | 自触发防护只在评论路径(`squad.go:931-939`),**assign 路径无**;leader 自建并自派给本队 → 自我派发 | `squad.go:998 shouldEnqueueSquadLeaderOnAssign` | 路径未隔离 |
| **E mention 绕过路由→死循环** | 个人 mention 绕过小队路由导致看门狗死循环 | 参 PL-146 修复 | 修复中/回归审计 PL-149 |
| F 队列积压与准入不同步 | `queuedTTLSeconds=2h` 与准入检查不一致,积压任务不受 leader 就绪保护 | `service/autopilot.go:66` | 限制 |

**根治方向(三维加固,迁移必带)**:① **全状态去重锁**——评论×(agent/leader)对跨生命周期唯一;② **路径隔离**——assign 与 comment 各自维护防护,assign 路径补自触发防护;③ **看门狗友好**——issue 状态更新加版本/CAS 校验,防陈旧数据覆盖新状态。

相关提交:`021fcf83`(单次执行锁)、`8c0418e5`(squad 内 @agent 抑制 leader)、`dfc159e1`(`/note` 防刷屏)。相关任务:PL-137/142/143/145/146/149。

## 7. 验证方法
- **构建门禁**:`cd server && go build ./... && go vet ./...`;`go test ./internal/handler/ -run 'Squad|Trigger'`、`go test ./cmd/server/ -run Sweep`。
- **冲突回归(对照第6节)**:① 造 leader→@agent→leader 回环,确认不再无限重派(B);② 造 task 超时与 leader 完工并发,确认 issue 终态不被 todo 覆盖(A);③ leader 自派本队,确认无自我派发(D);④ 个人 mention 场景确认不绕过路由(E,PL-146)。
- **生产取证**:`squad-ops logs multica-后端`、`squad-ops db-ro "SELECT status,count(*) FROM agent_task_queue GROUP BY 1"` 观察是否有异常积压/反复重派。
