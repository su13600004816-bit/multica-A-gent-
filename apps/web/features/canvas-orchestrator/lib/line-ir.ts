// Production-Line Graph IR — the backend-independent compiler for the visual
// line-orchestrator canvas.
//
// MIGRATION (PL-157, phase 1): ported verbatim from the legacy canvas station
// (ouroboros-circuit-console, `src/lib/line-ir.ts`). Per the migration SPEC §5
// (tracked in PL-152, authored in PR #10; once that lands it lives at
// docs/tech-assets/canvas-orchestration/SPEC.md) this compiler is fully
// backend-agnostic and is reused as-is — only the `CircuitStatus` import path
// changed and one `noUncheckedIndexedAccess` guard was added. `compileToWaves`
// (Kahn topological layering) and `validateLine` are the reused core.
//
// `DispatchItem` / `compileNodeToDispatchItem` below describe the LEGACY
// agent-control `/dispatch-batch` payload. They are kept here only as the pure
// IR→dispatch mapping; phase 2 replaces the *transport* (agent-control polling
// → multica task queue + WS) with a multica-native adapter and does NOT call
// back into the old station.

import type { CircuitStatus } from "./circuit-status";

export type LineNodeKind = "dev" | "audit";
export type LineExecutor = "claude" | "codex";
export type LineRole = "dev" | "audit";
export type LineMode = "write" | "read" | "audit";

export interface LineNode {
  id: string;
  kind: LineNodeKind;
  executor: LineExecutor;
  role: LineRole;
  mode: LineMode;
  instruction: string;
  ownedPaths: string[]; // write mode: required, must not overlap within a wave
  externalTaskId?: string; // write mode: required; defaults to `${lineId}:${node.id}`
  position?: { x: number; y: number };
}

export interface LineEdge {
  id: string;
  source: string;
  target: string;
}

export interface ProductionLine {
  id: string;
  title: string;
  nodes: LineNode[];
  edges: LineEdge[];
}

// Legacy agent-control /dispatch-batch item shape. Phase 2 supersedes the
// transport with a multica queue payload; this mapping stays as the pure
// IR projection.
export interface DispatchItem {
  executor_type: LineExecutor;
  instruction: string;
  role: LineRole;
  mode: LineMode;
  owned_paths: string[];
  metadata: {
    external_task_id?: string;
    line_id: string;
    node_id: string;
  };
}

export interface ValidationResult {
  ok: boolean;
  errors: string[];
}

let counter = 0;
function uid(prefix: string): string {
  counter += 1;
  const rand = Math.random().toString(36).slice(2, 8);
  return `${prefix}-${counter.toString(36)}${rand}`;
}

// NOTE: default id is deterministic so server and client render the same markup
// (no Math.random in the SSR path). Use freshLineId() on the client after mount
// to assign a unique id without a hydration mismatch.
export function emptyLine(title = "未命名生产线", id = "line-draft"): ProductionLine {
  return { id, title, nodes: [], edges: [] };
}

export function freshLineId(): string {
  return uid("line");
}

export function createLineNode(kind: LineNodeKind, position?: { x: number; y: number }): LineNode {
  const isDev = kind === "dev";
  return {
    id: uid(kind),
    kind,
    executor: isDev ? "claude" : "codex",
    role: isDev ? "dev" : "audit",
    mode: isDev ? "write" : "audit",
    instruction: "",
    ownedPaths: [],
    position,
  };
}

export function createLineEdge(source: string, target: string): LineEdge {
  return { id: uid("e"), source, target };
}

// status_class (agent-control) -> CircuitStatus (canvas color).
export function statusClassToCircuit(statusClass: string | undefined): CircuitStatus {
  switch (statusClass) {
    case "running":
      return "running";
    case "done":
      return "done";
    case "failed":
    case "blocked":
      return "failed";
    case "queued":
      return "pending";
    case "cancelled":
      return "neutral";
    default:
      return "neutral";
  }
}

const TERMINAL_CIRCUIT_STATUSES: ReadonlySet<CircuitStatus> = new Set<CircuitStatus>([
  "done",
  "failed",
  "neutral",
]);

export function isTerminalCircuitStatus(status: CircuitStatus): boolean {
  return TERMINAL_CIRCUIT_STATUSES.has(status);
}

export function resolveExternalTaskId(node: LineNode, lineId: string): string {
  return node.externalTaskId?.trim() || `${lineId}:${node.id}`;
}

// Normalize an owned path for comparison: strip backslashes, collapse "./",
// drop a trailing slash. (AUDIT-20260601-0003 A3)
export function normalizeOwnedPath(p: string): string {
  let s = p.trim().replace(/\\/g, "/");
  s = s.replace(/^\.\//, "").replace(/\/+$/, "");
  return s;
}

function isIllegalOwnedPath(np: string): boolean {
  if (!np || np === ".") return true;
  if (np.startsWith("/")) return true; // absolute
  return np.split("/").includes(".."); // traversal
}

// Two owned paths conflict if equal OR one contains the other. (A3)
function ownedPathsConflict(a: string, b: string): boolean {
  return a === b || b.startsWith(a + "/") || a.startsWith(b + "/");
}

export function validateLine(line: ProductionLine): ValidationResult {
  const errors: string[] = [];
  const ids = new Set<string>();
  for (const node of line.nodes) {
    if (ids.has(node.id)) errors.push(`节点 id 重复: ${node.id}`);
    ids.add(node.id);
    if (!node.instruction.trim()) errors.push(`节点 ${node.id} 缺少 instruction`);
    if (node.mode === "write") {
      if (node.ownedPaths.length === 0) {
        errors.push(`write 节点 ${node.id} 必须声明 ownedPaths`);
      }
      for (const p of node.ownedPaths) {
        if (isIllegalOwnedPath(normalizeOwnedPath(p))) {
          errors.push(`write 节点 ${node.id} 的 ownedPath 非法(绝对路径或含 ..): ${p}`);
        }
      }
    }
  }

  for (const edge of line.edges) {
    if (!ids.has(edge.source)) errors.push(`边 ${edge.id} 源节点不存在: ${edge.source}`);
    if (!ids.has(edge.target)) errors.push(`边 ${edge.id} 目标节点不存在: ${edge.target}`);
  }

  if (line.nodes.length === 0) errors.push("生产线为空,至少需要一个节点");

  // cycle detection via topo (only if endpoints valid)
  if (errors.length === 0 && detectCycle(line)) {
    errors.push("依赖图存在环,生产线必须是 DAG");
  }

  // owned_paths overlap within the same wave (write/fix tasks dispatch together).
  // Conflict on equality OR containment, over normalized paths. (A3)
  if (errors.length === 0) {
    for (const wave of compileToWaves(line)) {
      const claimed: Array<{ path: string; nodeId: string }> = [];
      for (const node of wave) {
        if (node.mode !== "write") continue;
        for (const raw of node.ownedPaths) {
          const p = normalizeOwnedPath(raw);
          const clash = claimed.find((c) => ownedPathsConflict(c.path, p));
          if (clash) {
            errors.push(`同批 ownedPaths 冲突: 节点 ${clash.nodeId}(${clash.path}) 与 ${node.id}(${p}) 重叠`);
          }
          claimed.push({ path: p, nodeId: node.id });
        }
      }
    }
  }

  return { ok: errors.length === 0, errors };
}

function detectCycle(line: ProductionLine): boolean {
  const indegree = new Map<string, number>();
  line.nodes.forEach((n) => indegree.set(n.id, 0));
  line.edges.forEach((e) => indegree.set(e.target, (indegree.get(e.target) || 0) + 1));
  const queue = line.nodes.filter((n) => (indegree.get(n.id) || 0) === 0).map((n) => n.id);
  let visited = 0;
  while (queue.length) {
    const id = queue.shift() as string;
    visited += 1;
    for (const e of line.edges) {
      if (e.source !== id) continue;
      const d = (indegree.get(e.target) || 0) - 1;
      indegree.set(e.target, d);
      if (d === 0) queue.push(e.target);
    }
  }
  return visited !== line.nodes.length;
}

// Kahn topological layering: each layer runs in parallel, layers run in series.
export function compileToWaves(line: ProductionLine): LineNode[][] {
  const indegree = new Map<string, number>();
  const byId = new Map(line.nodes.map((n) => [n.id, n]));
  line.nodes.forEach((n) => indegree.set(n.id, 0));
  line.edges.forEach((e) => indegree.set(e.target, (indegree.get(e.target) || 0) + 1));

  const waves: LineNode[][] = [];
  let frontier = line.nodes.filter((n) => (indegree.get(n.id) || 0) === 0).map((n) => n.id);
  const placed = new Set<string>();

  while (frontier.length) {
    const layer = frontier.filter((id) => !placed.has(id));
    if (layer.length === 0) break;
    layer.forEach((id) => placed.add(id));
    waves.push(layer.map((id) => byId.get(id)).filter(Boolean) as LineNode[]);

    const next: string[] = [];
    for (const id of layer) {
      for (const e of line.edges) {
        if (e.source !== id) continue;
        const d = (indegree.get(e.target) || 0) - 1;
        indegree.set(e.target, d);
        if (d === 0) next.push(e.target);
      }
    }
    frontier = next;
  }
  return waves;
}

// --- auto-rework helpers (canvas self-correction loop) ----------------------

export type AuditVerdict = "PASS" | "FAIL";

// Parse an auditor's final message into a PASS/FAIL verdict. Prefers an explicit
// `VERDICT: PASS|FAIL` line; falls back to a bare PASS/FAIL token; defaults to
// FAIL when neither is present (fail-closed, like the Python orchestrators).
export function parseVerdict(text: string | undefined): AuditVerdict {
  if (!text) return "FAIL";
  const m = /VERDICT:\s*(PASS|FAIL)/i.exec(text);
  if (m && m[1]) return m[1].toUpperCase() as AuditVerdict;
  if (/\bFAIL\b/.test(text)) return "FAIL";
  if (/\bPASS\b/.test(text)) return "PASS";
  return "FAIL";
}

// The write nodes that feed an audit node (direct predecessors with mode "write").
// These are the nodes re-dispatched (with the audit findings) during rework.
export function upstreamWriteNodes(line: ProductionLine, auditNodeId: string): LineNode[] {
  const byId = new Map(line.nodes.map((n) => [n.id, n]));
  const sources = line.edges.filter((e) => e.target === auditNodeId).map((e) => e.source);
  return sources
    .map((id) => byId.get(id))
    .filter((n): n is LineNode => Boolean(n) && n!.mode === "write");
}

// Keep the tail of an auditor message as the findings fed back to the dev node.
export function tailFindings(text: string | undefined, max = 1500): string {
  const t = (text || "").trim();
  return t.length > max ? t.slice(-max) : t;
}

export function compileNodeToDispatchItem(node: LineNode, lineId: string): DispatchItem {
  return {
    executor_type: node.executor,
    instruction: node.instruction,
    role: node.role,
    mode: node.mode,
    owned_paths: node.ownedPaths,
    metadata: {
      external_task_id: node.mode === "write" ? resolveExternalTaskId(node, lineId) : undefined,
      line_id: lineId,
      node_id: node.id,
    },
  };
}
