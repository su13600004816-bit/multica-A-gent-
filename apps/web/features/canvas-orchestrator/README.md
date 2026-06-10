# canvas-orchestrator (画布编排) — migration into multica

Migrating the legacy canvas station's **visual line-orchestrator console**
(`ouroboros-circuit-console`, disk `/home/fleet/canvas`) into multica.

Source SPEC: the canvas-orchestration migration SPEC, tracked in **PL-152** and
authored in PR #10. Once PR #10 lands it lives at
`docs/tech-assets/canvas-orchestration/SPEC.md`; this feature does not require
that file to be present and is safe to merge independently. Tracking issue:
**PL-157**.

The product goal: a draggable canvas where you lay out a production line as a
DAG of dev/audit nodes, compile it into topological **waves**, dispatch each
wave to agents, watch node status colour in real time, and auto-rework on audit
FAIL — i.e. *draw → compile → dispatch → observe → self-heal*.

## Phased plan

| Phase | Scope | Status |
|---|---|---|
| **1 — compiler core** | Port the backend-independent IR + compiler (`line-ir.ts`) and its status enum; unit-test `compileToWaves` / `validateLine` / rework helpers. | **done (PR #13)** |
| **2 — canvas UI + dispatch/observe** | Port the xyflow + dagre canvas (draw/connect/auto-layout/undo-redo) against multica's design system; rewrite dispatch/observe onto the **multica task queue + WS** (NOT old-station `agent-control` polling). | **done (this PR)** |
| **3 — persistence + autonomous rework** | Persist runs/layout to multica **DB** (issue/task linked) instead of local JSON; wire `reworkAudit` into multica's trigger system, coordinating boundaries with the watchdog↔line-brain conflict fix (PL-156). | sub-issue |

## What landed in phase 2

The canvas UI and a **multica-native** dispatch/observe layer — no legacy
agent-control transport, no DB persistence / autonomous rework (those stay
phase 3).

- `lib/rf-mapping.ts` — ProductionLine ⇄ `@xyflow/react` node/edge mapping,
  `dagre` left-to-right auto-layout, add/connect/remove helpers, and the T01
  reference preset. Pure (no React) so it unit-tests directly.
- `lib/dispatch-adapter.ts` — the transport rewrite. `compileNodeToQueueRequest`
  projects a node onto a `quickCreateIssue` payload (one queue row per node),
  `resolveAgentForExecutor` maps `claude`/`codex` → a workspace agent by
  model/name, and `wsEventToCircuit` / `taskStatusToCircuit` colour nodes from
  the `task:*` WebSocket lifecycle (enum drift downgrades, never throws).
- `lib/canvas-history.ts` — pure past/present/future undo/redo (cap 50).
- `components/circuit-node.tsx` — presentational xyflow node; status colour
  arrives via props from the WS-fed store, **no embedded fetch** (decoupling
  point 2 below). Colours use semantic tokens (`success`/`warning`/`info`/
  `destructive`).
- `components/node-inspector.tsx` — edit a node's executor/mode/instruction/
  ownedPaths; every change commits to undo history.
- `components/canvas-orchestrator.tsx` — the page: ReactFlow canvas + toolbar
  (add dev/audit, auto-layout, undo/redo, delete, T01 preset, clear, execute/
  stop) + inspector + execution log. `execute()` validates, compiles waves,
  and dispatches each wave sequentially onto the multica task queue, gating
  wave N+1 on wave N reaching a terminal status over WS.
- Route: `apps/web/app/[workspaceSlug]/(dashboard)/canvas-orchestrator/page.tsx`.
- Tests: `rf-mapping.test.ts`, `dispatch-adapter.test.ts`, `canvas-history.test.ts`
  (29 new; 53 total in this feature).

**Scope/decisions for phase 2 reviewers**

- The feature lives in `apps/web/features/canvas-orchestrator/` (continuing
  phase 1's location). It is web-only today; if desktop ever needs it the UI
  should be promoted to `packages/views/` per the monorepo sharing rules.
- A sidebar nav entry is intentionally **not** wired yet — the page is reachable
  at `/{workspace}/canvas-orchestrator`. Adding a nav link touches the shared
  `NavKey`/`paths`/i18n surface and is deferred to keep this PR's blast radius
  on the feature itself.
- `execute()` creates **real** quick-create issues/tasks (that is the queue
  evidence), guarded by a confirm dialog. There is no autonomous rework loop —
  audit nodes simply colour by completion; verdict parsing / re-dispatch is
  phase 3.

## What landed in phase 1

- `lib/circuit-status.ts` — the `CircuitStatus` union (execution + design states).
  The legacy registry/drive/sync payload types were intentionally dropped; they
  are not part of the line-orchestrator.
- `lib/line-ir.ts` — `ProductionLine` / `LineNode` / `LineEdge` IR plus the
  reused compiler: `compileToWaves` (Kahn topological layering), `validateLine`
  (unique ids, non-empty instruction, write-node ownedPaths, DAG/cycle check,
  per-wave ownedPaths overlap), and the auto-rework helpers
  (`parseVerdict` fail-closed, `upstreamWriteNodes`, `tailFindings`).
- `lib/__tests__/line-ir.test.ts` — 24 unit tests (`pnpm vitest run features/canvas-orchestrator`).

Per SPEC §5 this compiler is backend-agnostic and reused as-is; only the
`CircuitStatus` import path changed and one `noUncheckedIndexedAccess` guard was
added in `parseVerdict`.

## Decoupling map for phases 2–3 (the heavy rewrite)

The legacy UI is coupled to the old station in two ways that MUST be cut on the
way in — not carried over:

1. **Transport**: legacy `dispatchItems()` POST `/api/agent-control/dispatch-batch`
   then `pollNodes()` polls `tasks/{id}/status` every 4s. Replace with
   *enqueue to the multica task queue + subscribe to WS events* for status
   colouring. `compileNodeToDispatchItem` is kept here only as the pure
   IR→payload projection; phase 2 supplies a multica-native adapter.
2. **In-component side effects**: the legacy `CircuitNode` polls
   `/api/work-status` directly. The multica node component must take status via
   props from the WS-fed store, with no embedded fetch.

Other decoupling points (SPEC §5.2): persistence (local JSON → DB),
executor=claude/codex → multica agent/runtime mapping, and node
role/mode/ownedPaths semantics aligned to multica.
