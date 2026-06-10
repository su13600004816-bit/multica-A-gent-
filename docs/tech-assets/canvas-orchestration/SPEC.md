# 画布编排 强制说明书(SPEC)

| 字段 | 内容 |
|---|---|
| 技术名称 | 画布编排(可视化线编排控制台) / canvas-orchestration |
| 归档目录 | `docs/tech-assets/canvas-orchestration/` |
| 负责线 | T02(本份由 cc 亲自整理) |
| 当前状态 | **已部署在老 canvas 站(canvas.pl-1.com),正在并入 multica(P2 进行中);新站 multica 尚无此可视化画布** |
| 对应任务 | PL-152 |
| 维护人 | cc |
| 最后更新 | 2026-06-11 |

> ⚠️ **关键事实(苏总纠正后亲自核对)**:画布编排技术=**老 canvas 站的可视化线编排控制台**(磁盘 `/home/fleet/canvas`,工程名 `ouroboros-circuit-console`,基于 `@xyflow/react` + `dagre`)。**不是** multica 后端那套规则化 squad 体系(那是 multica 的派发后端,本 SPEC 不再混为一谈)。本份据老站真实源码编写,目标是把这块可视化画布**迁移并入 multica**。

## 1. 技术原理与作用
一块可拖拽的可视化画布:用户在画布上摆"生产线"的节点(开发/审计)、连边成 DAG,点执行后系统把图**拓扑分层成波次(waves)**,逐层把节点派发给 agent-control 执行,轮询任务状态实时给节点上色;审计节点 FAIL 时**自治返工**(重派上游 write 节点带审计意见,最多 3 轮)。即"画图 → 编译 → 派发 → 观测 → 自修复"的多智能体编排。

## 2. 核心源码文件清单(真实路径 · 源在 /home/fleet/canvas)
| 文件路径(老 canvas 仓库相对) | 角色 | 说明 |
|---|---|---|
| `src/app/line-orchestrator/page.tsx` | 入口 | 画布页面路由 |
| `src/app/line-orchestrator/LineOrchestratorConsole.tsx` | 入口/核心 | 主控制台(~1403行):节点/边状态、undo/redo、copy/paste、auto-layout、`execute()` 编排执行、轮询观测 |
| `src/components/circuit/EditableCircuitCanvas.tsx` | 核心 | 可编辑画布(拖拽/连线/多选删/缩放平移),基于 @xyflow/react |
| `src/components/circuit/CircuitCanvas.tsx` | 核心 | 只读画布 |
| `src/components/circuit/CircuitNode.tsx` | 核心 | 节点渲染(状态灯/图标/徽章) |
| `src/components/circuit/canvasInteractionConfig.ts` | 配置 | 响应式交互(mobile/tablet/desktop) |
| `src/lib/line-ir.ts` | 数据模型 | 生产线 IR:`ProductionLine{nodes,edges}`、`LineNode{kind,executor,role,mode,instruction,ownedPaths,position}`、`compileToWaves()`、`validateLine()` |
| `src/lib/circuit-types.ts` | 数据模型 | `CircuitNodeModel`/`CircuitGraph`/状态枚举 |
| `src/lib/line-runs-store.ts` | 数据/持久化 | 运行台账 `RunRecord`(含 `nodeTaskMap`),落盘 `LINE_RUNS_DIR`(默认 `/home/fleet/work/line-runs/*.json`) |
| `src/lib/orchestrator-tools.ts` | 编排工具层 | 框架无关的 dispatch/observe/takeover 8 个工具(JSON Schema) |
| `src/app/api/line-runs/route.ts` + `[id]/route.ts` | API | 运行台账 GET/POST/单查 |
| `src/app/api/orchestrator/call/route.ts` + `tools/route.ts` | API | 工具调用/列表入口 |
| `src/orchestrator-mcp/server.mjs` | 接口 | MCP server(`npm run mcp:orchestrator`),同一套工具 |

**调用链**:打开画布(`page.tsx`→`LineOrchestratorConsole`)→ 编辑节点/边+`autoLayout`(dagre)→ `validateLine()`(ID唯一/instruction非空/write节点有ownedPaths/无环/ownedPaths不重叠)→ `compileToWaves()`(Kahn 拓扑分层)→ 逐 wave `dispatchItems()` POST `/api/agent-control/dispatch-batch` → `pollNodes()`(4s×90)着色 → 审计FAIL则 `reworkAudit()` 重派上游(≤3轮)→ `persistRun(ended)`;事后可 `attachRun()` 重新挂载某次运行在画布上回放观测。

## 3. 对外接口 / API / 事件
- **画布内 API**(同源代理,不直连 :8788):
  - `POST /api/agent-control/dispatch-batch` 批量派发;`GET /api/agent-control/tasks/{id}/status|result|logs`;`POST .../message`(接管对话);`POST .../cancel`。
  - `GET/POST /api/line-runs`、`GET /api/line-runs/{runId}`(运行台账,画布可观测的源)。
  - `/api/line-templates`(生产线模板存取)。
- **编排工具(orchestrator-tools)**:`dispatch_line / list_runs / get_run / get_task_status / get_task_result / get_task_logs / message_task / cancel_task`——同时暴露给 Next API route、MCP server、Dify 函数调用。
- **快捷键**:Ctrl+Z/Shift+Z/Y 撤销重做、Ctrl+C/V 复制粘贴、Del 删节点。

## 4. 依赖 / 环境变量 / 配置
- **画布库**:`@xyflow/react` ^12.10.2(React Flow)、`dagre` ^0.8.5(+`@types/dagre`);框架 `next` 16.2.6 / `react` 19.2.4;`drizzle-orm` ^0.45.2。**未用** konva/d3。
- **关键常量**(`LineOrchestratorConsole.tsx`):`DISPATCH_TIMEOUT_S=900`(单节点15min)、`POLL_MS=4000`、`POLL_LIMIT=90`(≈6min)、`MAX_REWORK_ROUNDS=3`。
- **存储**:运行台账 JSON 落盘 `LINE_RUNS_DIR`(默认 `/home/fleet/work/line-runs/`);模板经 `/api/line-templates`。
- **后端依赖**:agent-control(:8788)经同源代理调用,代理路径白名单 `[A-Za-z0-9_-]+`。

## 5. 迁移到新系统(multica)的步骤 —— 本说明书核心
multica 现状:有规则化 squad 派发后端,但**没有可视化画布**。迁移=把这块可视化编排 UI + 编译/派发/观测引擎搬进 multica,并把派发目标从老站 agent-control 切到 multica 的任务体系:
1. **带走的文件**:`line-orchestrator/*`、`components/circuit/*`、`lib/line-ir.ts`、`lib/circuit-types.ts`、`lib/line-runs-store.ts`、`lib/orchestrator-tools.ts`、`api/line-runs/*`、`api/orchestrator/*`、`orchestrator-mcp/server.mjs`,以及依赖 `@xyflow/react`+`dagre`。
2. **强耦合点(迁移要切的)**:① 派发走老站 `/api/agent-control/dispatch-batch` + 轮询 `tasks/{id}/status`——multica 是 task queue + WS 事件模型,需把"批派发+轮询"改写成"入 multica 队列 + 订阅 WS";② 运行台账落本地文件无锁,multica 应改存 DB(issue/task 关联);③ executor=claude/codex 的概念需映射到 multica 的 agent/runtime;④ node.ownedPaths/role/mode 的语义需对齐 multica。
3. **需重写的胶水**:`dispatchItems()`/`pollNodes()` 整段(轮询→事件驱动);持久化层(文件→DB);代理层(agent-control→multica API)。
4. **迁移步骤**:移植画布 UI(xyflow/dagre 可直接用)→ 保留 line-ir 编译器(`compileToWaves`/`validateLine` 与后端无关,可整段复用)→ 重写派发/观测适配 multica 队列与 WS → 持久化改 DB → 验证一条最小线(如 T01 预设)能在 multica 上画→编→派→观测→自治返工。
5. **风险/坑**:**编译器(line-ir)可无痛复用,派发/观测层必须重写**;轮询模型搬到 multica 要换成 WS,否则与 multica 现有实时机制打架;自治返工(reworkAudit)重派逻辑要对接 multica 的触发体系,小心和看门狗↔线主脑冲突(见 `docs/tech-assets/watchdog/SPEC.md`)叠加。

## 6. 已知 BUG / 限制 / 坑(诚实写 · 多智能体配合相关)
| 现象 | 触发条件 | 影响 | 绕法/状态 |
|---|---|---|---|
| 运行台账并发覆盖 | 同 runId 并发 POST,本地文件无分布式锁 | 台账被覆盖 | 当前 best-effort upsert,迁移改 DB 解决 |
| 轮询固定超时无退避 | 慢任务(大 clone/大审计)> 6min(90×4s) | 节点误判失败 | 无指数退避/自适应,迁移时改进 |
| 自治返工轮次硬编码 | audit FAIL | 仅 3 轮且无中间复核 | `MAX_REWORK_ROUNDS=3` 写死 |
| 代理单点 | agent-control 代理故障 | 整条编排链断 | 无 fallback/熔断 |
| 大图卡顿 | 节点 > ~100 | 画布布局/签名计算卡 | `useDeferredAutoFit` 仅缓解 |
| 仅单 DAG、无分支/条件路由 | 需并行/条件/跨线 | 边的 PASS/FAIL/HOLD 标签在 `compileToWaves` 未生效;跨线需手动拆多个 lineId | 编排表达力受限,多智能体复杂配合不友好 |

## 7. 验证方法
- **老站现状(已部署)**:画布在 canvas.pl-1.com 的 `/line-orchestrator`;单测 `src/lib/__tests__/orchestrator-tools.test.ts`、`src/app/api/orchestrator/__tests__/call-route.test.ts`。
- **迁移后验证(multica)**:① 前端 `pnpm tsc`+`build` 过;② 在 multica 画一条最小线(如 T01:ingest→compress→validate→chat-gate→audit),点执行,确认节点按波次派发、状态实时着色、审计FAIL能自治返工;③ 确认派发真的进了 multica 任务体系(`squad-ops db-ro` 看 agent_task_queue),而非老站 agent-control;④ 运行台账落 DB 且并发不丢。
