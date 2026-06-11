# 画布编排（canvas-orchestration）· 独立技术储备说明书

> **独立体**：multica 的可视化小队编排画布（ReactFlow）。`package/` 含核心组件与路由页源码。
> ⚠️ **认准主体（苏总已纠正）**：本技术 = **multica 自己的 `squad-canvas-board`**，**不是**老站 ouroboros 的 `/line-orchestrator`（那是另一套，别混）。详见 [[canvas-orchestration-feature]]。
> 维护人：总管。状态：**改进中（苏总：先缓）**。最后更新：2026-06-11。

## 0. 一句话
把 multica 的「小队 + 成员 + 任务状态」用 ReactFlow 节点图画出来，可手动拖拽编排，一眼看清一条生产线里谁在干、卡在哪；架在现有 issue/squad/agent 数据上的**可视化 + 手工编排层**。

## 1. 作用与原理
- 两块画面：①顶层「生产线电路板画布」(`canvas/page.tsx`，读 projectListOptions)；②「小队编排画布」(`canvas/[id]`，一队一图，成员扇出成节点，按主导状态着色+流水线动画)。
- 数据流**单向只读派生**：react-query 拉 issue/squad/member/agent → 按 assignee 聚合算节点主导状态 → 映射颜色 → 喂 ReactFlow。手工编辑(拖拽/连线/加删)活在组件内存（P3 才落库）。

## 2. 本包文件（package/）
| 文件 | 作用 |
|---|---|
| `package/components/squad-canvas-board.tsx` | **核心引擎**(~580行)：节点/连线种子、状态着色、实时刷新、连线/加删/改名/重置工具栏 |
| `package/components/squad-canvas-detail-page.tsx` | 小队编排页骨架(面包屑+左详情栏+右画布+智能体详情弹窗) |
| `package/pages/canvas__page.tsx` | 入口①电路板画布路由页 |
| `package/pages/canvas__circuit-node.tsx` / `circuit-theme.ts` | 电路板节点视图 + 暗色主题/状态色映射 |
| `package/pages/[id]__page.tsx` | 入口②小队画布路由页(re-export 详情页) |

## 3. 依赖
- `@xyflow/react ^12.11.0`(ReactFlow)；项目内 `@multica/core`(queries/hooks/paths) / `@multica/ui` / `@multica/views`。
- 数据源：`projectListOptions`/`issueListOptions`/`squadListOptions`/`memberListOptions`/`agentListOptions`。
- P3 跨设备持久化：后端 `squad_canvas_layout` 表 + handler（见 [[canvas-orchestration-feature]]）。

## 4. 对外接口
- `SquadCanvasBoard({squadId, members, getEntityName, onSelectAgent?})` → ReactFlow 画布主组件。
- `SquadCanvasDetailPage()` 整页入口；路由 `/<ws>/canvas/<squadId>`。

## 5. 迁移 / 接入新系统
1. 复制 `package/` 组件与页面；装 `@xyflow/react`。
2. 接新系统的 squad/member/agent/issue 数据源（替换 queries import）。
3. 加画布路由页；节点着色复用任务状态配色。
4. 手工编排持久化（P3）：建 layout 表 + GET/PUT 接口。

## 6. 同步
- SSOT = multica 前端 `packages/views/squads/components/` + `apps/web/app/.../canvas/`。
- 当前活跃分支：`canvas/pl111-canvas-workbench-v2`（画布工作台二版）；线上前端 `multica-web:canvas-photo-20260611` 含这些页（但 `/pl1/canvas-orchestrator` 路由仍 404，核心编排迁移未完）。
- **改进中**：苏总在单独推进，本包是当前快照；定版后同步回并更新本 SPEC。

## 7. 改进指南（苏总规格，[[canvas-orchestration-feature]]）
- 目标：eraser.io/n8n 级**可手工编排无限画布** + 画布 AI；节点点击弹智能体详情面板（动态/Tasks/指令/Skills/环境变量/MCP+近30天表现）；连线动态流水线+状态变色；小队 T01..T08 切换。
- UI 铁律：视觉/配色/版面严格跟现 multica 一致，绝不另起样式。
- 折叠屏：必测 Z Fold7 视口（见 [[user-views-on-zfold7]]）。

## 8. 删除 / 卸载
1. 移除画布路由页与小队列表的"进画布"入口。
2. 删 `squad-canvas-board`/`squad-canvas-detail-page` 组件。
3. 可选删 `squad_canvas_layout` 表（P3 持久化）。无其它功能依赖它。

## 9. 验证方法
- `tsc`+`next build` 过（线上镜像已含这些页）。
- 真机（Z Fold7）实测：节点可见(fitView 居中)、点击弹面板、连线流水线/状态色、小队切换、手工拖拽持久化。
- 折叠屏视口逐项 PASS。

## 10. 已知点
- 核心「可视化线编排」迁移未完（`/pl1/canvas-orchestrator` 404）；苏总改进中。
- P3 跨设备持久化后端未完全闭环（单设备 localStorage 已够）。

## 11. 当前部署状态
- squad-canvas 页在线上 `multica-web:canvas-photo-20260611`；编排核心改进中（缓）。
