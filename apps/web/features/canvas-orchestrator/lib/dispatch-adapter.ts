// Multica-native dispatch / observe adapter (PL-157 phase 2).
//
// This REPLACES the legacy agent-control transport: instead of POSTing to
// `/api/agent-control/dispatch-batch` and polling `tasks/{id}/status` every
// 4s, a line node is enqueued onto the multica task queue (one quick-create
// issue per node → a row in `agent_task_queue`) and node colour is driven by
// the `task:*` WebSocket events the server already broadcasts.
//
// Everything here is pure: it builds the request payload and maps task/event
// status → CircuitStatus. The React layer owns the api call + WS subscription.

import type { Agent } from "@multica/core/types";
import type { WSEventType } from "@multica/core/types";

import type { CircuitStatus } from "./circuit-status";
import { resolveExternalTaskId, type LineExecutor, type LineNode } from "./line-ir";

// The quick-create request a single node compiles to. `agent_id` is the
// resolved multica agent (NOT the legacy executor string); `prompt` carries
// the node instruction plus its role/mode/ownedPaths context.
export interface QueueDispatchRequest {
  agent_id: string;
  prompt: string;
  parent_issue_id?: string | null;
}

// A node paired with the task it was enqueued as. The canvas keys WS updates
// by `taskId`; `externalTaskId` is the stable line-scoped id retained for
// cross-referencing (e.g. db-ro evidence).
export interface NodeDispatch {
  nodeId: string;
  taskId: string;
  externalTaskId: string;
}

// Build the human-readable prompt enqueued for a node. Audit nodes are asked
// for an explicit verdict so a future phase-3 rework loop can parse it; phase 2
// only colours by completion.
export function buildNodePrompt(node: LineNode, lineTitle: string): string {
  const lines: string[] = [];
  lines.push(`[生产线 ${lineTitle} · 节点 ${node.id} · ${node.role}/${node.mode}]`);
  lines.push("");
  lines.push(node.instruction.trim());
  if (node.mode === "write" && node.ownedPaths.length > 0) {
    lines.push("");
    lines.push(`仅允许改动以下路径：${node.ownedPaths.join(", ")}`);
  }
  if (node.mode === "audit") {
    lines.push("");
    lines.push("完成后请在结论中给出独占一行 `VERDICT: PASS` 或 `VERDICT: FAIL`。");
  }
  return lines.join("\n");
}

// Map a line executor (claude/codex) to a concrete workspace agent by matching
// the agent's model / name against the executor's hints, falling back to the
// first available agent so a line can still run in a workspace whose agents are
// named differently. Returns undefined only when the workspace has no agents.
const EXECUTOR_HINTS: Record<LineExecutor, string[]> = {
  claude: ["claude", "anthropic", "sonnet", "opus", "haiku"],
  codex: ["codex", "openai", "gpt", "o3", "o4"],
};

export function resolveAgentForExecutor(
  executor: LineExecutor,
  agents: readonly Agent[],
): Agent | undefined {
  if (agents.length === 0) return undefined;
  const hints = EXECUTOR_HINTS[executor];
  const match = agents.find((a) => {
    const model = (a.model ?? "").toLowerCase();
    const name = (a.name ?? "").toLowerCase();
    return hints.some((h) => model.includes(h) || name.includes(h));
  });
  return match ?? agents[0];
}

export function compileNodeToQueueRequest(
  node: LineNode,
  lineId: string,
  lineTitle: string,
  agentId: string,
  parentIssueId?: string | null,
): QueueDispatchRequest {
  return {
    agent_id: agentId,
    prompt: buildNodePrompt(node, lineTitle),
    parent_issue_id: parentIssueId ?? null,
  };
}

export { resolveExternalTaskId };

// task:* WebSocket event → CircuitStatus. This is the live colouring path.
export function wsEventToCircuit(event: WSEventType): CircuitStatus | undefined {
  switch (event) {
    case "task:queued":
    case "task:dispatch":
    case "task:waiting_local_directory":
      return "pending";
    case "task:running":
    case "task:progress":
      return "running";
    case "task:completed":
      return "done";
    case "task:failed":
      return "failed";
    case "task:cancelled":
      // A cancelled task is a wave-blocking terminal, NOT a passing one: an
      // upstream node that was cancelled must stop downstream nodes from being
      // enqueued (see runWaves). It is mapped to `blocked`, distinct from the
      // passing `neutral` terminal.
      return "blocked";
    default:
      return undefined;
  }
}

// AgentTask.status → CircuitStatus. Used to reconcile colour from a snapshot
// (e.g. on mount / WS reconnect) rather than a live event. Enum drift
// downgrades to "neutral" rather than crashing (CLAUDE.md API-compat rule).
export function taskStatusToCircuit(status: string | undefined): CircuitStatus {
  switch (status) {
    case "queued":
    case "dispatched":
    case "waiting_local_directory":
      return "pending";
    case "running":
      return "running";
    case "completed":
      return "done";
    case "failed":
      return "failed";
    case "cancelled":
      // Wave-blocking terminal — see wsEventToCircuit. Kept distinct from the
      // `neutral` enum-drift fallback below so a snapshot reconcile gates waves
      // the same way a live `task:cancelled` event does.
      return "blocked";
    default:
      return "neutral";
  }
}

const TERMINAL: ReadonlySet<CircuitStatus> = new Set<CircuitStatus>([
  "done",
  "failed",
  "neutral",
  // A cancelled node (mapped to `blocked`) is terminal, so waitForWaveTerminal
  // resolves immediately rather than burning the 15-min wave timeout. runWaves
  // then gates on it via isNodeBlocked.
  "blocked",
]);

export function isTerminal(status: CircuitStatus): boolean {
  return TERMINAL.has(status);
}

// --- wave runner (fail-closed) ---------------------------------------------
//
// Dispatch each topological wave in order, gating wave N+1 on wave N. The
// control flow is fail-closed: if ANY node in a wave cannot be enqueued — the
// `dispatchNode` callback rejects, or resolves `null` because no agent could be
// resolved — the wave is a failure and NO further wave is dispatched. This is
// what keeps the multica task queue consistent with the DAG: a downstream node
// must never create a real issue/task while an upstream node it depends on
// failed to enqueue.
//
// Pure control flow: the React layer injects the side-effecting closures
// (`dispatchNode` owns the api call + status/log writes; `waitForWaveTerminal`
// polls the WS-driven status mirror), so this is unit-testable without a DOM.
export interface WaveRunnerDeps {
  // Enqueue one node. Resolve with the created task id => the node is now live
  // and will be observed via WS. Resolve `null` => the node could not be
  // dispatched (e.g. no matching agent) and the wave fails closed. Reject =>
  // enqueue failed and the wave fails closed.
  dispatchNode: (node: LineNode) => Promise<string | null>;
  // Resolve true once every live node reached a terminal status; false on
  // timeout or cancellation.
  waitForWaveTerminal: (liveNodeIds: string[]) => Promise<boolean>;
  // Did a node settle in a wave-blocking terminal state (via WS) — i.e.
  // `failed` OR `cancelled`/`blocked`? Either must stop downstream waves: a
  // cancelled upstream node is no more "done" than a failed one.
  isNodeBlocked: (nodeId: string) => boolean;
  log: (message: string) => void;
  isCancelled: () => boolean;
}

export async function runWaves(
  waves: readonly (readonly LineNode[])[],
  deps: WaveRunnerDeps,
): Promise<void> {
  for (let w = 0; w < waves.length; w += 1) {
    if (deps.isCancelled()) {
      deps.log("⏹ 已停止");
      return;
    }
    const wave = waves[w]!;
    deps.log(`层 ${w + 1}/${waves.length}: 派发 ${wave.length} 个节点`);

    const liveNodeIds: string[] = [];
    let dispatchFailed = false;
    for (const node of wave) {
      try {
        const taskId = await deps.dispatchNode(node);
        if (taskId == null) {
          // Node could not be dispatched (no agent) — fail closed and stop
          // enqueuing the rest of this wave.
          dispatchFailed = true;
          break;
        }
        liveNodeIds.push(node.id);
      } catch {
        // Enqueue rejected. The node status/log was already written by the
        // dispatchNode closure; here we just stop the wave from continuing.
        dispatchFailed = true;
        break;
      }
    }

    if (dispatchFailed) {
      deps.log(`层 ${w + 1} 入队失败，停止后续层`);
      return;
    }

    const ok = await deps.waitForWaveTerminal(liveNodeIds);
    if (deps.isCancelled()) {
      deps.log("⏹ 已停止");
      return;
    }
    const blocked = liveNodeIds.filter((id) => deps.isNodeBlocked(id));
    if (!ok || blocked.length > 0) {
      deps.log(
        `层 ${w + 1} 未全部通过${blocked.length ? `（失败/取消: ${blocked.join(", ")}）` : "（超时）"}，停止后续层`,
      );
      return;
    }
    deps.log(`层 ${w + 1} 全部完成 ✓`);
  }
}
