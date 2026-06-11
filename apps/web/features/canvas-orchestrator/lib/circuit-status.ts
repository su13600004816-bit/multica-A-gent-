// Canvas-orchestrator node/edge status enum.
//
// Migrated from the legacy canvas station (ouroboros-circuit-console,
// `src/lib/circuit-types.ts`). Only the execution-relevant status union is
// carried over — the legacy registry/drive/sync payload types are NOT part of
// the line-orchestrator migration and are intentionally dropped.
//
// `running`/`done`/`failed` are the line-orchestrator runtime states; the rest
// are the static design states reused by the canvas colouring layer.

export type CircuitStatus =
  | "active"
  | "draft"
  | "pending"
  | "unmapped"
  | "blocked"
  | "neutral"
  // execution states (line orchestrator runtime)
  | "running"
  | "done"
  | "failed";
