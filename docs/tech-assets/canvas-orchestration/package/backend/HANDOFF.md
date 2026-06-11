# 交接:画布编排 P3 跨设备持久化 · 后端端点(总管 → 后端线/cc,2026-06-11)

## 背景
multica「画布编排」(squad-canvas)P3 手工布局持久化。**前端已上线**(localStorage 本地版,单设备可用;并已写好服务端接线、优雅降级)。
**缺的就是后端这 1 个 canvas-layout 端点**。把它并进当前后端线(pl165 那条)再部署,跨设备同步即生效。

## 改动极小且独立(不碰任何现有逻辑)
1. **新文件** `server/internal/handler/squad_canvas_layout.go`(裸 SQL,用 `h.DB`;独立表,不动 squad 的 `SELECT *` sqlc)。
2. **router.go** 在 squad `/{id}` 路由块加 2 行(`UpdateSquadMemberRole` 之后):
   ```go
   r.Get("/canvas-layout", h.GetSquadCanvasLayout)
   r.Put("/canvas-layout", h.SetSquadCanvasLayout)
   ```
3. **迁移** `server/migrations/118_squad_canvas_layout.up.sql`(建 `squad_canvas_layout` 表;**幂等 IF NOT EXISTS**;表已在线上库建好,迁移再跑无害)。

## 依赖(任何后端版本都有,无需改别处)
`h.DB`(dbExecutor: Exec/Query/QueryRow)、`parseUUIDOrBadRequest`、`writeError`、`writeJSON`、`chi.URLParam`。
端点在 `/api/squads` 路由组下,**需 `X-Workspace-Slug` 头**(前端 api 客户端自动带,见 packages/core/api/client.ts:288)。

## 怎么应用(二选一)
- **A. git am**(若基于同仓库):`git am 0001-canvas-layout-backend.patch`(router.go 行号变了可能 fuzzy,手动核 squad 路由块)。
- **B. 手动**(最稳):拷 `squad_canvas_layout.go` 进 handler 目录;router.go 加上面 2 行;`118_*.sql` 进 migrations。

## 应用后
1. `sudo deploy_multica_backend.sh <你的后端源dir> - <tag>` 构建部署(后端自动跑迁移)。
2. `multica-deploy-doctor` 确认 DOCTOR_OK。
3. 自测端点:`POST /auth/send-code` 造验证码行 → `verify-code` 用 dev code(staging 下 MULTICA_DEV_VERIFICATION_CODE=889168,需先 send-code 造 DB 行)→ 拿 cookie → `PUT/GET /api/squads/{id}/canvas-layout`(带 `X-Workspace-Slug: pl1`)。返回 200 即通。
4. 前端无需再动(`multica-web:pl156-canvas-p3srv` 已接好,会自动从服务端读)。

## 前端对应改动(已上线,供对照)
`origin/pl156-canvas-merge`:`api.getSquadCanvasLayout/setSquadCanvasLayout`(client.ts) + squad-canvas-board.tsx(serverLayout query + sig 不阻塞渲染 + saveLayout 存服务端 + fitView 居中)。

## ⚠️ 注意
- 前端部署已被总管冻结(`DEPLOY_FROZEN_WEB`,画布由总管统一管);**这交接只动后端,别碰前端**。
- 别把 squad 表加列(会撑爆 `SELECT *` 的 sqlc 扫描)——所以用独立表。
