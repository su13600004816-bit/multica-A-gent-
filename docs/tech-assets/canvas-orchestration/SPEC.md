# 画布编排 强制说明书(SPEC)

| 字段 | 内容 |
|---|---|
| 技术名称 | 画布编排(多智能体编排/小队) / canvas-orchestration |
| 归档目录 | `docs/tech-assets/canvas-orchestration/` |
| 负责线 | T02(本份由 cc 亲自整理) |
| 当前状态 | 已部署 multica 生产(squad 体系) |
| 对应任务 | PL-152 |
| 维护人 | cc |
| 最后更新 | 2026-06-11 |

> ⚠️ **关键事实**:multica 的"画布编排"**不是可视化拖拽画布**,而是**规则驱动的小队(squad)编排体系**:线主脑(squad leader)+ 成员,靠"简报协议 + @mention 触发 + 活动记录"完成多智能体协作。无 ReactFlow/图形画布库。这是当前 BUG 最多、对多智能体配合最不友好的模块,见第6节。

## 1. 技术原理与作用
把一个 issue 派给一个 squad → 后端解析出该 squad 的 leader → 给 leader 注入"操作简报 + 成员花名册" → leader 用 `[@Name](mention://agent/<uuid>)` 把活委派给某成员 → 成员执行 → leader 用 `multica squad activity` 记录评估 → 每次新触发 leader 重新评估。实现规则化的多智能体分工。

## 2. 核心源码文件清单(真实路径)
| 文件路径 | 角色 | 说明 |
|---|---|---|
| `server/internal/handler/squad_briefing.go` | 核心 | `squadOperatingProtocol` 硬编码协议 + `buildSquadLeaderBriefing()`,每次 leader 领任务前置注入 |
| `server/internal/handler/squad.go` | 核心 | squad CRUD、成员状态派生 `deriveSquadMemberStatus()`、leader 派发决策(见 watchdog SPEC 第3节函数) |
| `server/internal/handler/issue.go` (L2203-2452) | 核心 | `UpdateIssue()`:assignee/status 变化 → 取消旧 task + 派 agent/leader |
| `server/internal/handler/comment.go` (L951-975) | 核心 | `triggerTasksForComment()` 三路触发(assignee agent / squad leader / @mentions) |
| `server/pkg/db/queries/squad.sql` | 数据 | squad/member 全部查询;`TransferSquadAssignees`(归档转移) |
| `server/migrations/084_squad.up.sql` | 数据 | `squad` / `squad_member` 建表;088 instructions、090 is_leader、096 autopilot squad |
| `server/cmd/multica/cmd_squad.go` | CLI | squad 管理命令 |
| `server/cmd/server/router.go` (L750-767) | 路由 | `/api/squads*` 与 `/api/issues/{id}/squad-evaluated` |
| `server/pkg/protocol/events.go` (L110-113) | 事件 | `squad:created/updated/deleted` WS 事件 |
| `packages/core/types/squad.ts` | 前端类型 | Squad/SquadMember/SquadActivityLog 等 |
| `packages/core/api/client.ts` (L1853-1910) | 前端 API | squad 全部接口 |
| `packages/views/squads/components/squad-detail-page.tsx` | 前端 UI | 详情页、成员状态实时刷新(WS invalidate) |
| `packages/views/modals/quick-create-issue.tsx` | 前端 UI | 快建 issue 的 squad 选择器(leader 不可达则隐藏) |

**调用链**:派 issue 给 squad(`assignee_type=squad`)→ `UpdateIssue()` 派发 leader task → `buildSquadLeaderBriefing()` 注入简报+花名册 → leader 唤醒 → `@mention` 委派成员 → 成员执行 → `multica squad activity` 记录 → 后续评论/状态变化经 `triggerTasksForComment()` 再唤醒 leader。

## 3. 对外接口 / API
- REST:`GET/POST /api/squads`、`GET/PUT/DELETE /api/squads/{id}`、`.../members`(增删/改角色/状态)、`POST /api/issues/{id}/squad-evaluated`。
- CLI:`multica squad get/list/create/update/delete/member/activity`。
- WS 事件:`squad:created|updated|deleted` → 前端 query invalidate。

## 4. 依赖 / 环境变量 / 配置
- 前端:`zustand` ^5.0.0、`@tanstack/react-query` ^5.96.2、`zod` ^4.1.5。
- 后端:Go + PostgreSQL(squad/squad_member 表);依赖 agent_runtime / agent_task_queue 派生成员实时状态。
- 协议文本硬编码在 `squad_briefing.go`,改协议=改代码重部署。

## 5. 迁移到新系统的步骤 —— 本说明书核心
1. **带走的文件**:`squad*.go`、`squad.sql`、migrations 084/085/088/090/096、`packages/core/types/squad.ts`、`packages/views/squads/*`、`cmd_squad.go`。
2. **强耦合点(迁移要切的)**:① 依赖 `agent_task_queue` 任务队列模型;② 依赖 `agent_runtime` 在线状态;③ 触发逻辑深度耦合 `issue.go/comment.go` 的 issue 生命周期;④ 简报协议假定"@mention 即触发 run"这一平台语义。新系统若没有同样的 task queue + 触发模型,leader 派发链要重写。
3. **需重写的胶水**:触发判定(`shouldEnqueueSquadLeaderOnComment/OnAssign`)、dedup(`HasPendingTaskForIssueAndAgent`)依赖具体表结构,需按新系统的队列重写。
4. **迁移步骤**:建表 → 移 handler/queries → 接新系统的 task queue 与触发总线 → 移植简报协议 → 接 WS 事件 → 验证。
5. **风险/坑**:**触发模型是命门**——看门狗与 leader 双写 issue 状态、评论触发放大成死循环(见第6节),迁移时必须连同"全状态去重锁 + 路径隔离 + 状态版本校验"一起带走,否则新系统重蹈覆辙。

## 6. 已知 BUG / 限制 / 坑(诚实写 · 多智能体配合相关)
| 现象 | 触发条件 | 影响 | 绕法/状态 |
|---|---|---|---|
| **双触发并行跑** | 既 @mention 成员、又建 todo 子任务派给他 | 同一 agent 并行跑两次 | 协议规定二选一;`squad_briefing_test.go` 已锁测试 |
| **评论触发死循环放大** | leader→@agent→run→leader 回环;一条 PASS 评论反复拉起下一轮 | 刷屏/烧钱/死循环 | 见 watchdog SPEC;commit `021fcf83` 修;根因 dedup 只看 queued/dispatched |
| **看门狗↔线主脑状态互覆盖** | 看门狗失败超时 task 改 issue→todo,同时 leader 改 done | 状态错乱、看板跳动 | 详见 `docs/tech-assets/watchdog/SPEC.md` 冲突点A/C |
| 成员实时状态漂移 | WS 事件丢失/延迟 | UI 显示陈旧状态 | 有 staleTime 兜底,只读视图影响小 |
| leader 不可达则 squad 失效 | leader agent 离线/归档 | 无法派发(无孤儿 squad) | 快建模态已隐藏不可达 squad |
| `/note` 之外无细粒度防刷屏 | 高频评论 | 触发风暴 | `isNoteComment()` 仅挡 `/note` 前缀 |

## 7. 验证方法
- **构建门禁**:`cd server && go build ./... && go vet ./...`;前端 `pnpm tsc`。后端关键测试:`go test ./internal/handler/ -run Squad`(含 `squad_briefing_test.go` 双触发锁、`squad_comment_trigger_test.go`)。
- **功能验证**:建 squad+leader+成员 → 派 issue 给 squad → 真机看 leader 是否注入简报并 @ 委派、成员是否执行、`squad activity` 是否记录。
- **回归重点**:制造 leader→@agent→leader 场景,确认**不再无限重派**(对照 commit `021fcf83`)。
