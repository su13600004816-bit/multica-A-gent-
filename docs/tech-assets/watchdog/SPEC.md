# 看门狗（watchdog）· 独立技术储备说明书

> **独立体**：看门狗是**兜底**子系统，由「仓库内三层（multica 后端）」+「仓库外一层(line-config 套件)」组成。`package/` 含仓库内清扫器源码 + 仓库外 line-watchdog 全套脚本（这套不在 multica 仓库，迁移最易漏）。
> 维护人：总管。最后更新：2026-06-11。

## 0. 一句话
agent/任务不正常时把系统从卡死中救回来：超时任务失败重派、空转 run 强停、blocked/done 滞留按小队重路由；并用 suppress/terminal-gate 防止"看门狗 ↔ 线主脑"互相触发成死循环。

## 1. 四个机制（前三在 multica 后端仓库，第四在 line-config 仓库外）
1. **运行时清扫器**（`runtime_sweeper.go`）：服务端 goroutine，每 30s。心跳超时→失败孤儿任务；dispatched/running 超时→失败；queued 过期清理；失败任务的 issue 由 `in_progress` 回滚 `todo`（触发重派）。
2. **守护进程超时看门狗**（`daemon/config.go` + `daemon.go:runIdleWatchdog`）：每 run 安全网。静默+队列空 >30min 强停；单 tool 在飞放宽到 2h；15s 心跳维持 `last_seen_at`。env：`MULTICA_AGENT_IDLE_WATCHDOG`/`_TOOL_WATCHDOG`/`_TIMEOUT`/`_HEARTBEAT_INTERVAL`。
3. **触发/防刷屏 + terminal-gate**（`handler/comment.go`/`squad.go`/`issue.go`）：评论三路触发（assignee/squad-leader/@mention）+ `HasPendingTaskForIssueAndAgent` 去重 + `/note` 不触发 + **`suppress_triggers`/`--no-trigger`（可见不唤醒）** + **terminal-gate**（终态 issue 不再被触发，关 self-ignition）。
4. **外层 line-watchdog（仓库外，package/external-line-watchdog/）**：`line-watchdog.service` 跑 `line_watchdog.py`，检测 blocked/in_review/done 滞留与证据冲突，`line_reset.py` 按小队自动分流、发"🚨看门狗自动分流"评论。由 member 账号 `e1c7cb89` 驱动，**不在 multica 仓库**。

## 2. 本包文件（package/）
| 文件 | 层 | 作用 |
|---|---|---|
| `package/runtime_sweeper.go` | 仓库内① | 清扫器主体（`runRuntimeSweeper`/`sweepStaleRuntimes`/`sweepStaleTasks`/`sweepExpiredQueuedTasks`；L~314 issue 回滚 todo） |
| `package/external-line-watchdog/line_watchdog.py` | 仓库外④ | 外层看门狗本体（检测滞留/冲突） |
| `package/external-line-watchdog/line_reset.py` | 仓库外④ | 自动分流/重路由（发 🚨 评论，纯脚本不判因） |
| `package/external-line-watchdog/line_done_gate.py` | 仓库外 | done 前置门禁 |
| `package/external-line-watchdog/line_evidence.py` | 仓库外 | 证据校验 |
| `package/external-line-watchdog/line-watchdog.service` | 仓库外 | systemd 单元 |
| `package/external-line-watchdog/RECOVERY_WATCHDOG.md` | 仓库外 | 恢复手册 |
> 仓库外完整套件还有 `line_states/line_dispatch/line_foreman/line_observe/line_partial/ab_gate/line_bridge` 等（同目录 `/home/fleet/line-config/`），迁移时整套带走。

## 3. 依赖 / 环境
- 仓库内：随 multica 后端二进制编译；无额外依赖。
- 仓库外：Python3 + systemd；调 multica HTTP API（用 member `e1c7cb89` token）；模型代理 `model_api_port.py`（qwen:18181/deepseek:18182）。

## 4. 对外接口
- 仓库内：无对外 API，纯后台 goroutine + run 内 watchdog；阈值靠 env/常量。
- 仓库外：line-watchdog 定时跑，经 multica API 读 issue/任务、发评论、改状态。

## 5. 迁移 / 接入新系统
1. 仓库内三层随新系统后端一起带（清扫器 goroutine + daemon watchdog + 触发/suppress/terminal-gate）。
2. **仓库外 line-watchdog 套件必须单独带走**（`package/external-line-watchdog/` + line-config 全套），改 systemd 单元里的 API 地址/token 指向新系统。
3. 新系统需有等价的 issue/task/agent_task_queue 模型 + 评论触发机制。

## 6. 同步
- 仓库内 SSOT = multica 后端（已部署，含 suppress+terminal-gate）。
- 仓库外 SSOT = `/home/fleet/line-config/`（活在 HK box）。两边各自维护，本包是快照；改了要同步回本包并登记。

## 7. 改进指南
- 调清扫器阈值：`runtime_sweeper.go` L19-63 常量（改需重部署）。
- 调 daemon 超时：env 即可，无需重部署。
- self-ignition 收敛：terminal-gate 已关掉仓库内的；**仓库外 line_reset.py 仍较激进**（本会话见过 PL-157 被反复 🚨），可加"终态/done_real 不再分流"的判因，是已知可改进点。

## 8. 删除 / 卸载
1. 仓库外：`systemctl disable --now line-watchdog.*`，停 line-config 套件。
2. 仓库内清扫器：去掉 `main.go` 里 `go runRuntimeSweeper(...)` 装配（**慎删**：去掉后卡死任务无人兜底）。
3. daemon watchdog：env 设大或去掉 `runIdleWatchdog` 调用。

## 9. 验证方法
- 仓库内：`go build` 过；`/app/server` strings 含 `runRuntimeSweeper`（已验 422 命中）。
- suppress：`multica issue comment add --no-trigger` 发评论不触发（**已实测通过**）。
- 外层：`systemctl is-active line-watchdog`（HK box 上 supervisor timer active）。

## 10. 已知点 / SPEC 勘误
- **旧 SPEC(pl153) 已过时**：它说"suppress_triggers 不存在""看门狗↔线主脑冲突未根治"——**两者均已实现**（suppress + terminal-gate 在线上后端 `multica-backend:pl91mem-20260611`）。本说明书为最新口径。
- 外层 line_reset 的 self-ignition 仍是可改进点（见第 7 节）。

## 11. 当前部署状态
- 仓库内：随 `multica-backend:pl91mem-20260611` 在产（含清扫器/daemon watchdog/suppress/terminal-gate）。
- 仓库外：`line-watchdog.service` + supervisor timer 在 HK box 运行（member e1c7cb89，24h 约 120 条分流评论）。
