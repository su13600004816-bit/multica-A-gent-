# canvas-orchestrator (画布编排) — migration into multica

Migrating the legacy canvas station's **visual line-orchestrator console**
(`ouroboros-circuit-console`, disk `/home/fleet/canvas`) into multica.

Source SPEC: `docs/tech-assets/canvas-orchestration/SPEC.md` (PL-152). Tracking
issue: **PL-157**.

The product goal: a draggable canvas where you lay out a production line as a
DAG of dev/audit nodes, compile it into topological **waves**, dispatch each
wave to agents, watch node status colour in real time, and auto-rework on audit
FAIL — i.e. *draw → compile → dispatch → observe → self-heal*.

## Phased plan

| Phase | Scope | Status |
|---|---|---|
| **1 — compiler core** | Port the backend-independent IR + compiler (`line-ir.ts`) and its status enum; unit-test `compileToWaves` / `validateLine` / rework helpers. | **done (this PR)** |
| **2 — canvas UI + dispatch/observe** | Port the xyflow + dagre canvas (draw/connect/auto-layout/undo-redo) against multica's design system; rewrite dispatch/observe onto the **multica task queue + WS** (NOT old-station `agent-control` polling). | sub-issue |
| **3 — persistence + autonomous rework** | Persist runs/layout to multica **DB** (issue/task linked) instead of local JSON; wire `reworkAudit` into multica's trigger system, coordinating boundaries with the watchdog↔line-brain conflict fix (PL-156). | sub-issue |

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
