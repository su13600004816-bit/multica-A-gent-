<!--
强制说明书 v1 · 归属总任务 PL-150 · 子任务 PL-152
本文件由 SPEC_TEMPLATE.md 复制填空而来,7 节占位全部填实。
所有源码路径均经 git 核对真实存在;画布编排代码当前位于集成分支
`canvas/pl111-pl120-integration`(尚未合入 main),核对方式见文末。
-->

# 画布编排 / canvas-orchestration 强制说明书(SPEC)

| 字段 | 内容 |
|---|---|
| 技术名称 | 画布编排 / canvas-orchestration |
| 归档目录 | `docs/tech-assets/canvas-orchestration/` |
| 负责线 | T02 |
| 当前状态 | 仅代码未部署(代码在分支 `canvas/pl111-pl120-integration`,未合入 `main`,生产 canvas.pl-1.com 跑的是老站,非本仓库代码) |
| 对应任务 | PL-152(归属 PL-150;关联 PL-111 / PL-118 / PL-120) |
| 维护人 | T02 线 · cc 收口 |
| 最后更新 | 2026-06-11 |

## 1. 技术原理与作用
**一句话**:把 Multica 的「小队 + 成员 + 任务状态」用 ReactFlow 节点图画出来,做成可手动拖拽编排的画布,让人一眼看清一条生产线里谁在干活、卡在哪。

展开:画布编排不是一个独立的执行引擎,而是**架在现有 issue / squad / agent 数据之上的可视化与手工编排层**。它有两块互相独立的画面:

1. **生产线电路板画布**(顶层):把工作区的「项目(生产线)」每条画成一个电路板风格节点,显示名称 / 任务数 / 完成度 / 状态,点节点跳进该生产线详情。纯展示,连线只是装饰。
2. **小队编排画布**(`/<ws>/canvas/<squadId>`):一条小队一张画布。小队本体作根节点,成员(agent / 人)从右侧扇出成节点;节点和「小队→成员」连线按该成员名下任务的**主导状态**着色并做流水线流光动画。底部有「画布编排工具栏」,可手动加节点、连线、双击改名、删除、自动铺满、重置。点 agent 节点弹「智能体详情」面板。

核心数据流是**单向只读派生**:react-query 拉 issue/squad/member/agent 列表 → 按 `assignee_id` 聚合算每个节点的主导状态 → 映射成颜色 token → 喂给 ReactFlow。手工编辑(拖拽 / 连线 / 加删节点)只活在组件内存里,**不回写后端**。

## 2. 核心源码文件清单(真实路径)
> 路径均在分支 `canvas/pl111-pl120-integration` 实测存在(`git cat-file -e` 全部 OK)。main 上不存在,需带分支核对。

| 文件路径 | 角色 | 说明 |
|---|---|---|
| `apps/web/app/[workspaceSlug]/canvas/page.tsx` | 入口① | 顶层「生产线电路板画布」路由页;读 `projectListOptions`,只取前 5 条项目画节点 |
| `apps/web/app/[workspaceSlug]/canvas/circuit-node.tsx` | 核心逻辑 | 电路板节点视图(进度条 / 状态徽标 / 双 Handle) |
| `apps/web/app/[workspaceSlug]/canvas/circuit-theme.ts` | 配置/类型 | 暗色科技风调色板 + 项目状态→中文标签/强调色映射;注释标明「取自 /home/fleet/canvas 的电路板画布」 |
| `apps/web/app/[workspaceSlug]/(dashboard)/canvas/[id]/page.tsx` | 入口② | 小队画布路由页,一行 re-export `SquadCanvasDetailPage` |
| `packages/views/squads/components/squad-canvas-detail-page.tsx` | 核心逻辑 | 小队编排页骨架:面包屑 + 左侧详情栏(头像 tab 排 / 成员区 / 小线任务看板)+ 右侧画布 + 智能体详情弹窗 |
| `packages/views/squads/components/squad-canvas-board.tsx` | **核心引擎** | 编排画布主体(580 行):节点/连线种子、状态着色、实时状态原地刷新、连线/加删/改名/重置工具栏。本技术的重心 |
| `packages/views/squads/components/squad-canvas-tab-strip.tsx` | 核心逻辑 | 顶部小队头像 tab 排,点 tab 整页切到该小队 |
| `packages/core/issues/config/status.ts` | 配置 | `STATUS_CONFIG`(状态→`dividerColor` 等 Tailwind 类),画布节点状态点复用它 |
| `packages/core/issues/queries.ts` | 数据源 | `issueListOptions(wsId)`(line 233),画布状态着色的数据来源 |
| `packages/core/projects/queries.ts` | 数据源 | `projectListOptions(wsId)`(line 11),电路板画布数据来源 |
| `packages/core/workspace/queries.ts` | 数据源 | `squadListOptions` / `memberListOptions` / `agentListOptions` / `workspaceKeys` |
| `packages/core/paths/paths.ts` | 配置 | `squadCanvas(id)`(line 35)= `${ws}/canvas/${id}`,画布路由助手 |
| `packages/views/locales/{en,zh-Hans,ko}/squads.json` | 配置 | 画布 i18n 文案键(`canvas_page.*` / `canvas_tab.*`) |
| `apps/web/package.json` / `packages/views/package.json` | 依赖 | `@xyflow/react ^12.11.0`(ReactFlow) |

**调用关系/数据流**:
- 入口①:`canvas/page.tsx` →(`projectListOptions`)→ `lines.slice(0,5)` → 映射 `CircuitNode` 节点 + 相邻项目间装饰连线 → ReactFlow。点节点 `router.push(projectDetail)`。
- 入口②:`squads-page.tsx` 的 CanvasCard → `p.squadCanvas(squadId)` 路由 → `(dashboard)/canvas/[id]/page.tsx` re-export → `SquadCanvasDetailPage` → 内嵌 `SquadCanvasBoard`。
- 画布主体 `SquadCanvasBoard`:
  1. `issueListOptions` 拉全量 issue → 按 `assignee_id` 聚合 → `dominantStatus()` 按优先级(blocked > in_progress > in_review > todo > backlog > done)折成单一状态 → `statusByNodeId`(键是成员节点 `m.id`)+ `rootStatus`。
  2. `seed`:小队根节点 + 成员节点(右列等距)+「squad-root->成员」连线,按状态着色/动画。
  3. `useNodesState/useEdgesState` 持有可编辑状态;`seedKey = members.map(m=>m.id).join(",")` 变化时整盘重铺。
  4. 独立的 live-status `useEffect`:**原地**给节点打状态色/状态点、给 seed 连线换流光,**不动**位置和手工加的节点/连线。
  5. 工具栏回调:`onConnect`(addEdge)/ `handleAddNode` / `handleDeleteSelected` / `fitView` / `handleReset`。

## 3. 对外接口 / API / 事件
> 画布编排没有自己的后端 API;它是纯前端组件 + 复用 core 的 react-query option 工厂。下表是新系统接它时要对接的「函数级接口」。

| 接口/函数/事件 | 入参 | 出参 | 说明 |
|---|---|---|---|
| `SquadCanvasDetailPage()` | 无(从路由 `/<ws>/canvas/<squadId>` 读 squadId) | React 页面 | 小队画布整页入口 |
| `SquadCanvasBoard(props)` | `{ squadId, squadName, squadAvatarUrl?, leaderId, members, getEntityName, onSelectAgent? }` | ReactFlow 画布 | 编排画布主组件;`onSelectAgent(agentId)` 在点 agent 节点时回调 |
| `SquadCanvasTabStrip(props)` | `{ squads, activeSquadId, onSelect, label }` | 头像 tab 排 | `onSelect(squadId)` 切换当前小队 |
| `issueListOptions(wsId, sort?)` | 工作区 id | react-query options | 状态着色数据源 |
| `projectListOptions(wsId)` | 工作区 id | react-query options | 电路板画布数据源 |
| `squadListOptions / memberListOptions / agentListOptions(wsId)` | 工作区 id | react-query options | 小队 / 成员 / agent 列表 |
| `dominantStatus(issues)` | `Issue[]` | `IssueStatus \| null` | 按固定优先级折叠多任务为单一状态(私有,但是着色核心) |
| `onNodeClick`(ReactFlow 事件) | `(_, node)` | void | agent 成员节点 → 触发 `onSelectAgent` |
| `onConnect`(ReactFlow 事件) | `Connection` | void | 手工连线,加一条 idle 样式 edge |

## 4. 依赖项 / 环境变量 / 配置
- **第三方依赖(含版本)**:
  - `@xyflow/react ^12.11.0`(ReactFlow,画布核心)— `apps/web` 与 `packages/views` 两处 package.json 都声明
  - `@tanstack/react-query`(数据拉取)
  - `lucide-react`(图标:Plus/Trash2/Maximize2/RotateCcw/Users)
  - Next.js(`next/navigation` 的 `useRouter`)
- **本仓库内部依赖**:`@multica/core`(queries / hooks / paths / types / api / issues config / workspace avatar-url)、`@multica/ui`(Button / Skeleton / ActorAvatar / theme-provider)、`@multica/views`(navigation / i18n / layout / 通用 ActorAvatar / SquadTaskBoard / AgentDetailDialog)。
- **环境变量**:画布层本身无专属环境变量;依赖工作区现有 API base / 鉴权环境(同 web app)。
- **配置项 / 默认值**:电路板画布硬编码 `lines.slice(0, 5)`(只显示前 5 条生产线);成员列间距 `MEMBER_GAP_Y=84`、列宽 `COLUMN_X=320`;`minZoom 0.4 / maxZoom 1.5`;删除键 `Backspace/Delete`。
- **运行前置条件**:需登录的工作区上下文(`useWorkspaceId` / `useCurrentWorkspace`)、CSS 设计 token(`--warning/--success/--info/--destructive/--muted-foreground/--border`)、i18n `squads` 命名空间已加载。

## 5. 迁移到新系统的步骤(本说明书核心)
> 目标:让新系统直接拿走画布编排复用。下面把解耦点写清。

1. **要带走的文件**:
   - 顶层电路板:`apps/web/app/[workspaceSlug]/canvas/{page,circuit-node}.tsx` + `circuit-theme.ts`
   - 小队画布:`packages/views/squads/components/squad-canvas-{detail-page,board,tab-strip}.tsx` + `(dashboard)/canvas/[id]/page.tsx`
   - i18n:`packages/views/locales/*/squads.json` 里 `canvas_page.*` / `canvas_tab.*` 文案键
   - 依赖 `@xyflow/react ^12.11.0`

2. **依赖当前系统的耦合点(解耦时要切掉/替换)**:
   - **数据层**:`@multica/core` 的 react-query option 工厂(`issueListOptions` / `projectListOptions` / `squadListOptions` / `memberListOptions` / `agentListOptions` / `workspaceKeys`)+ `api.listSquadMembers`。这是最重的耦合——画布所有数据都从这里来。
   - **状态语义**:`STATUS_CONFIG`(`packages/core/issues/config/status.ts`)+ 语义 CSS token(`--warning` 等)。节点/连线颜色直接绑死这套 token。
   - **路由**:`@multica/core/paths` 的 `squadCanvas(id)` 与 `projectDetail/agentDetail/memberDetail`;`pathname.split("/").pop()` 直接从 URL 抠 squadId(脆,换路由器要改)。
   - **UI 基件**:`@multica/ui` 的 ActorAvatar / Button / Skeleton / theme-provider;`@multica/core/workspace/avatar-url` 的 `resolvePublicFileUrl`。
   - **类型**:`@multica/core/types` 的 `Issue/IssueStatus/Squad/SquadMember/Agent/ProjectStatus`。
   - **i18n**:`useT("squads")` 钩子与 `squads.json` 键。

3. **需要重写的胶水代码**:
   - react-query option 工厂 → 适配新系统数据层(REST/GraphQL/其它),这是迁移最大工作量。
   - 语义颜色 token 映射(`STATUS_COLOR_VAR` / `STATUS_CONFIG.dividerColor`)→ 新系统主题。
   - `useT` i18n 适配层。
   - ActorAvatar / avatar-url 解析适配。
   - **`squad-canvas-board.tsx` 里 seed-effect 与 live-status-effect 的双轨刷新逻辑**——这是最脆的胶水(见第 6 节 BUG①②③),迁移时必须连同 `EMPTY_ISSUES` 稳定引用、`seedKey` 重铺策略一起原样搬,改一处就可能复发 React #185 死循环。
   - **后端画布持久化(目前完全没有)**:要做成真正可用的编排画布,新系统必须新增「画布存储」(节点/连线/布局/手工步骤的持久化 + 读写 API)。当前是 in-session-only,刷新即丢。

4. **迁移步骤**:复制上述文件 → 把 `@multica/*` 依赖替换为新系统对应模块 → 重写数据层胶水(query 工厂)→ 接颜色/i18n/头像适配 → 新增后端画布存储与读写接口 → 配 `@xyflow/react` → 按第 7 节验证。

5. **风险/注意**:
   - 别把 in-session-only 当成已持久化,迁移时容易漏掉「需要新建后端存储」这一最大块。
   - 颜色 token 一旦换主题,状态语义(blocked=红 / in_progress=橙…)要逐一核对,否则画布会误导。
   - `dominantStatus` 的优先级是产品约定,不是真理,迁移要让产品确认。

## 6. 已知 BUG / 限制 / 架构债(本节诚实写,重点)
> 画布编排是当前系统里和多智能体配合最不友好、债最多的一块。下表逐条列。

| 现象 | 触发条件 | 影响 | 绕法/状态 |
|---|---|---|---|
| **React #185 无限渲染死循环** | `allIssues` 每次渲染拿到新 `[]` 引用 → `statusByNodeId` 重算 → live-status effect 反复 setState | 整页卡死/崩溃 | 已修:用模块级常量 `EMPTY_ISSUES` 稳定空数组兜底(commit `e71c4673`)。**极脆**:任何新派生数组不保持稳定引用就会复发 |
| **手工编辑不持久化(in-session-only)** | 拖拽/连线/加删/改名后刷新或离开页面 | 编排成果全丢,画布无法当真正的编排工具用 | 无绕法;代码注释明写「No backend canvas store yet」。需新增后端存储(最大债) |
| **成员集变化整盘重铺、清掉手工编辑** | `seedKey`(成员 id 集)变化触发 `setNodes(seed)` | 加成员/异步加载后,手工布局与自定义步骤被重置 | 现用「位置/状态分两条 effect」缓解:live-status 原地打补丁不动位置;但 seed 重铺仍会清。脆 |
| **连线是装饰、不代表真实依赖** | 电路板画布在相邻项目间连线;小队画布只有「根→成员」扇出 | 看不出真实的任务依赖 / agent 交接顺序,误导「在编排」 | T03 已为此返工:`移除按状态排序合成的假连线`(commit `59989934`/T03 系列),edges 改空。真实依赖仍无法表达 |
| **节点 id 与 assignee id 不同源,靠桥接** | 成员节点键 `m.id`,issue 键 `m.member_id`,着色靠两者映射 | 一旦键错配,节点静默显示错误/无状态,排查难 | 现靠 `statusByNodeId[m.id]` 显式桥接;无类型级保护 |
| **多任务折叠成单一状态点** | 一个成员名下多条 issue | `dominantStatus` 按固定优先级只取一个色,一个扛 10 个任务的 agent 只显示一个点 | 设计如此;对多智能体真实负载是误导,看不出并发/堆积 |
| **与真实多智能体编排解耦** | — | 画布只是「按 issue 状态着色的组织图」,看不到真实 run 状态 / 队列 / 看门狗路由 / 交接;实际编排在服务端 stage orchestrator(`server/internal/service/stage_orchestrator.go`,在 `pl103-orchestrator` 分支,**未在 main**)+ 看门狗派发(PL-153) | 对「观察多智能体配合」这个核心诉求最不友好 | 架构债:画布层与执行层完全两套,无打通 |
| **两套并行画布实现** | 电路板画布(项目级,暗色 `circuit-theme` 硬编码)vs 小队画布(成员级,Multica 设计 token) | 两套节点系统 / 两套配色,双重维护 | 未统一 |
| **电路板画布硬截前 5 条** | 项目数 > 5 | 静默截断,只显示前 5 条生产线 | `slice(0,5)` 硬编码,无提示 |
| **未合入 main / 未上生产** | — | 代码只在 `canvas/pl111-pl120-integration` 等分支;生产 canvas.pl-1.com 是老站(circuit-theme 注释指向 `/home/fleet/canvas`) | 迁移目标态;需先合流或随迁移整体搬走 |

## 7. 验证方法
> 本 SPEC 仅新增文档,未改任何代码,故文档本身不触发 tsc/build。下面给画布编排代码本身的可复现验证方法。

- **真实路径核对(审核照此逐条点开)**:
  ```bash
  git fetch origin
  B=origin/canvas/pl111-pl120-integration
  for f in \
    "apps/web/app/[workspaceSlug]/canvas/page.tsx" \
    "apps/web/app/[workspaceSlug]/canvas/circuit-node.tsx" \
    "apps/web/app/[workspaceSlug]/canvas/circuit-theme.ts" \
    "apps/web/app/[workspaceSlug]/(dashboard)/canvas/[id]/page.tsx" \
    "packages/views/squads/components/squad-canvas-board.tsx" \
    "packages/views/squads/components/squad-canvas-detail-page.tsx" \
    "packages/views/squads/components/squad-canvas-tab-strip.tsx" \
    "packages/core/issues/config/status.ts" \
    "packages/core/projects/queries.ts" \
    "packages/core/paths/paths.ts"; do
    git cat-file -e "$B:$f" && echo "OK  $f" || echo "MISS $f"
  done
  ```
- **构建门禁(在画布分支上跑)**:
  - 类型检查:`pnpm tsc`(= `turbo typecheck --filter=!@multica/mobile`)
  - 构建:`pnpm build`(= `turbo build --filter=!@multica/mobile`)
  - 说明:本 SPEC 未改代码,门禁状态以画布分支自身为准;历史上 React #185(BUG①)就是该分支的运行期门禁,已由 `EMPTY_ISSUES` 修复。
- **功能验证(浏览器)**:
  1. 起 `pnpm dev:web`,登录工作区,进「小队」页,点某小队的 Canvas 卡片 → 应进 `/<ws>/canvas/<squadId>`。
  2. 画布应显示:小队根节点 + 成员扇出节点,节点/连线按任务状态着色(in_progress 橙 / in_review 绿 / done 蓝 / blocked 红),有流光动画。
  3. 工具栏:加节点 / 连线 / 双击改名 / 删除 / 铺满 / 重置应都生效;点 agent 节点弹「智能体详情」。
  4. 顶部头像 tab 切小队 → 整页(详情/成员/任务看板/画布)联动切换。
  5. **反向验证 BUG②**:手工拖动节点后刷新页面 → 布局应丢失(证明 in-session-only,持久化缺失)。
- **新系统可用性验证(迁过去后)**:最小用例 = 在新系统打开一个小队画布,节点能从新数据层正确着色 + 手工编辑能落到新建的后端画布存储并在刷新后保留。后者是当前系统做不到、新系统必须补上的能力。

---
<!-- 自检:7 节已全填;路径已 git 核对真实;BUG 节按真实 commit(e71c4673 React#185 / 59989934 假连线返工)与代码注释诚实列;验证可复现。 -->
