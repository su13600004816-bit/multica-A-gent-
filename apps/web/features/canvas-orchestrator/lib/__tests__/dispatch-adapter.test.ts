import { describe, expect, it, vi } from "vitest";
import type { Agent, RuntimeDevice } from "@multica/core/types";

import {
  buildNodePrompt,
  checkAgentDispatchable,
  compileNodeToQueueRequest,
  isTerminal,
  preflightLineDispatch,
  resolveAgentForExecutor,
  resolveDispatchableAgentForExecutor,
  runWaves,
  taskStatusToCircuit,
  wsEventToCircuit,
} from "../dispatch-adapter";
import type { CircuitStatus } from "../circuit-status";
import type { LineNode } from "../line-ir";

const writeNode: LineNode = {
  id: "dev-1",
  kind: "dev",
  executor: "claude",
  role: "dev",
  mode: "write",
  instruction: "实现登录跳转修复",
  ownedPaths: ["src/auth.ts", "src/login.ts"],
};

const auditNode: LineNode = {
  id: "aud-1",
  kind: "audit",
  executor: "codex",
  role: "audit",
  mode: "audit",
  instruction: "审计登录修复",
  ownedPaths: [],
};

function agent(partial: Partial<Agent>): Agent {
  return partial as unknown as Agent;
}

describe("buildNodePrompt", () => {
  it("includes role/mode, instruction and owned paths for a write node", () => {
    const p = buildNodePrompt(writeNode, "我的线");
    expect(p).toContain("dev/write");
    expect(p).toContain("实现登录跳转修复");
    expect(p).toContain("src/auth.ts, src/login.ts");
    expect(p).not.toContain("VERDICT");
  });

  it("asks an audit node for an explicit verdict and omits owned-paths line", () => {
    const p = buildNodePrompt(auditNode, "我的线");
    expect(p).toContain("audit/audit");
    expect(p).toContain("VERDICT: PASS");
    expect(p).not.toContain("仅允许改动");
  });
});

describe("resolveAgentForExecutor", () => {
  const agents = [
    agent({ id: "a1", name: "Codex Bot", model: "gpt-5-codex" }),
    agent({ id: "a2", name: "Claude Dev", model: "claude-opus-4" }),
    agent({ id: "a3", name: "Generic", model: "other" }),
  ];

  it("matches claude → claude/anthropic model", () => {
    expect(resolveAgentForExecutor("claude", agents)?.id).toBe("a2");
  });

  it("matches codex → openai/codex model", () => {
    expect(resolveAgentForExecutor("codex", agents)?.id).toBe("a1");
  });

  it("falls back to the first agent when nothing matches", () => {
    const only = [agent({ id: "x", name: "Foo", model: "bar" })];
    expect(resolveAgentForExecutor("claude", only)?.id).toBe("x");
  });

  it("returns undefined when the workspace has no agents", () => {
    expect(resolveAgentForExecutor("claude", [])).toBeUndefined();
  });
});

describe("runtime version pre-flight (quick-create gate)", () => {
  // Build a runtime row with a given online status + reported cli_version.
  function runtime(id: string, status: "online" | "offline", cliVersion?: string): RuntimeDevice {
    return {
      id,
      status,
      metadata: cliVersion === undefined ? {} : { cli_version: cliVersion },
    } as unknown as RuntimeDevice;
  }

  function rtMap(...rs: RuntimeDevice[]): Map<string, RuntimeDevice> {
    return new Map(rs.map((r) => [r.id, r]));
  }

  function devNode(id: string, executor: "claude" | "codex" = "claude"): LineNode {
    return {
      id,
      kind: "dev",
      executor,
      role: "dev",
      mode: "write",
      instruction: `run ${id}`,
      ownedPaths: [`src/${id}.ts`],
    };
  }

  describe("checkAgentDispatchable", () => {
    it("blocks a `dev` cli_version runtime as missing (matches prod 422)", () => {
      const a = agent({ id: "a1", name: "Claude", model: "claude-opus-4", runtime_id: "r1" });
      const c = checkAgentDispatchable(a, rtMap(runtime("r1", "online", "dev")));
      expect(c.dispatchable).toBe(false);
      expect(c.state).toBe("missing");
      expect(c.current).toBe("dev");
      expect(c.min).toBe("0.2.20");
    });

    it("blocks a runtime with no reported cli_version (missing)", () => {
      const a = agent({ id: "a1", name: "Claude", model: "claude-opus-4", runtime_id: "r1" });
      const c = checkAgentDispatchable(a, rtMap(runtime("r1", "online")));
      expect(c.dispatchable).toBe(false);
      expect(c.state).toBe("missing");
    });

    it("blocks a too-old runtime", () => {
      const a = agent({ id: "a1", name: "Claude", model: "claude-opus-4", runtime_id: "r1" });
      const c = checkAgentDispatchable(a, rtMap(runtime("r1", "online", "0.2.19")));
      expect(c.dispatchable).toBe(false);
      expect(c.state).toBe("too_old");
      expect(c.current).toBe("0.2.19");
    });

    it("blocks an offline runtime even when its version would pass", () => {
      const a = agent({ id: "a1", name: "Claude", model: "claude-opus-4", runtime_id: "r1" });
      const c = checkAgentDispatchable(a, rtMap(runtime("r1", "offline", "0.3.17")));
      expect(c.dispatchable).toBe(false);
      expect(c.online).toBe(false);
      expect(c.state).toBe("ok");
    });

    it("passes an online runtime at/above the minimum", () => {
      const a = agent({ id: "a1", name: "Claude", model: "claude-opus-4", runtime_id: "r1" });
      const c = checkAgentDispatchable(a, rtMap(runtime("r1", "online", "0.3.17")));
      expect(c.dispatchable).toBe(true);
      expect(c.state).toBe("ok");
    });
  });

  describe("resolveDispatchableAgentForExecutor", () => {
    it("prefers a qualified same-executor agent over an unqualified one", () => {
      const agents = [
        // First match by hint, but its runtime is `dev` → unqualified.
        agent({ id: "a1", name: "Claude A", model: "claude-opus-4", runtime_id: "r-dev" }),
        // Second same-executor match, on a healthy online runtime → qualified.
        agent({ id: "a2", name: "Claude B", model: "claude-sonnet-4", runtime_id: "r-ok" }),
      ];
      const res = resolveDispatchableAgentForExecutor(
        "claude",
        agents,
        rtMap(runtime("r-dev", "online", "dev"), runtime("r-ok", "online", "0.3.0")),
      );
      expect(res.ok).toBe(true);
      expect(res.ok && res.agent.id).toBe("a2");
    });

    it("blocks (ok:false) with current/min when no agent qualifies", () => {
      const agents = [
        agent({ id: "a1", name: "Claude", model: "claude-opus-4", runtime_id: "r-dev" }),
      ];
      const res = resolveDispatchableAgentForExecutor(
        "claude",
        agents,
        rtMap(runtime("r-dev", "online", "dev")),
      );
      expect(res.ok).toBe(false);
      if (!res.ok) {
        expect(res.reason).toBe("missing");
        expect(res.agent?.id).toBe("a1");
        expect(res.runtimeId).toBe("r-dev");
        expect(res.current).toBe("dev");
        expect(res.min).toBe("0.2.20");
      }
    });

    it("reports `no_agent` when the workspace has no agents", () => {
      const res = resolveDispatchableAgentForExecutor("claude", [], rtMap());
      expect(res.ok).toBe(false);
      expect(res.ok ? null : res.reason).toBe("no_agent");
    });
  });

  describe("preflightLineDispatch", () => {
    it("blocks every node when the only online runtime is `dev` (the prod-FAIL case)", () => {
      const agents = [
        agent({ id: "a1", name: "Claude", model: "claude-opus-4", runtime_id: "r-dev" }),
        agent({ id: "a2", name: "Codex", model: "gpt-5-codex", runtime_id: "r-dev2" }),
      ];
      const blocks = preflightLineDispatch(
        [devNode("n1", "claude"), devNode("n2", "codex")],
        agents,
        rtMap(runtime("r-dev", "online", "dev"), runtime("r-dev2", "online", "dev")),
      );
      expect(blocks).toHaveLength(2);
      // Every block carries current/min so the log can name the gap.
      for (const b of blocks) {
        expect(b.reason).toBe("missing");
        expect(b.current).toBe("dev");
        expect(b.min).toBe("0.2.20");
        expect(b.agentName).toBeTruthy();
        expect(b.runtimeId).toBeTruthy();
      }
    });

    it("flags too_old and offline distinctly", () => {
      const agents = [
        agent({ id: "a1", name: "Claude old", model: "claude-opus-4", runtime_id: "r-old" }),
        agent({ id: "a2", name: "Claude off", model: "claude-sonnet-4", runtime_id: "r-off" }),
      ];
      const tooOld = preflightLineDispatch(
        [devNode("n1", "claude")],
        [agents[0]!],
        rtMap(runtime("r-old", "online", "0.1.0")),
      );
      expect(tooOld[0]!.reason).toBe("too_old");
      const offline = preflightLineDispatch(
        [devNode("n2", "claude")],
        [agents[1]!],
        rtMap(runtime("r-off", "offline", "0.3.0")),
      );
      expect(offline[0]!.reason).toBe("offline");
    });

    it("returns no blocks when every node has an online, version-passing agent", () => {
      const agents = [
        agent({ id: "a1", name: "Claude", model: "claude-opus-4", runtime_id: "r-ok" }),
        agent({ id: "a2", name: "Codex", model: "gpt-5-codex", runtime_id: "r-ok2" }),
      ];
      const blocks = preflightLineDispatch(
        [devNode("n1", "claude"), devNode("n2", "codex")],
        agents,
        rtMap(runtime("r-ok", "online", "0.3.0"), runtime("r-ok2", "online", "0.3.0")),
      );
      expect(blocks).toEqual([]);
    });

    it("gates dispatch: a blocked pre-flight never reaches quickCreateIssue", async () => {
      // Mirror execute()'s guard: pre-flight first, and only run the wave loop
      // when there are no blocks. With a `dev`-only workspace, the line must be
      // refused before any issue/task is created.
      const agents = [
        agent({ id: "a1", name: "Claude", model: "claude-opus-4", runtime_id: "r-dev" }),
      ];
      const runtimes = rtMap(runtime("r-dev", "online", "dev"));
      const nodes = [devNode("n1", "claude")];
      const quickCreateIssue = vi.fn(async (nodeId: string) => `task-${nodeId}`);

      const blocks = preflightLineDispatch(nodes, agents, runtimes);
      if (blocks.length === 0) {
        await runWaves([nodes], {
          dispatchNode: async (n) => quickCreateIssue(n.id),
          waitForWaveTerminal: async () => true,
          isNodeBlocked: () => false,
          log: () => {},
          isCancelled: () => false,
        });
      }

      expect(blocks.length).toBeGreaterThan(0);
      expect(quickCreateIssue).not.toHaveBeenCalled();
    });
  });
});

describe("compileNodeToQueueRequest", () => {
  it("targets the resolved agent and carries the built prompt + parent", () => {
    const req = compileNodeToQueueRequest(writeNode, "line-1", "我的线", "agent-7", "issue-99");
    expect(req.agent_id).toBe("agent-7");
    expect(req.parent_issue_id).toBe("issue-99");
    expect(req.prompt).toContain("实现登录跳转修复");
  });

  it("defaults parent to null", () => {
    const req = compileNodeToQueueRequest(writeNode, "line-1", "我的线", "agent-7");
    expect(req.parent_issue_id).toBeNull();
  });
});

describe("wsEventToCircuit", () => {
  it("maps the task lifecycle to circuit colours", () => {
    expect(wsEventToCircuit("task:queued")).toBe("pending");
    expect(wsEventToCircuit("task:dispatch")).toBe("pending");
    expect(wsEventToCircuit("task:running")).toBe("running");
    expect(wsEventToCircuit("task:progress")).toBe("running");
    expect(wsEventToCircuit("task:completed")).toBe("done");
    expect(wsEventToCircuit("task:failed")).toBe("failed");
    // A cancelled task is a wave-blocking terminal, not a passing one.
    expect(wsEventToCircuit("task:cancelled")).toBe("blocked");
  });

  it("ignores unrelated events", () => {
    expect(wsEventToCircuit("issue:created")).toBeUndefined();
  });
});

describe("taskStatusToCircuit", () => {
  it("maps queue statuses and downgrades unknown values", () => {
    expect(taskStatusToCircuit("queued")).toBe("pending");
    expect(taskStatusToCircuit("dispatched")).toBe("pending");
    expect(taskStatusToCircuit("running")).toBe("running");
    expect(taskStatusToCircuit("completed")).toBe("done");
    expect(taskStatusToCircuit("failed")).toBe("failed");
    expect(taskStatusToCircuit("cancelled")).toBe("blocked");
    expect(taskStatusToCircuit("some_future_state")).toBe("neutral");
    expect(taskStatusToCircuit(undefined)).toBe("neutral");
  });
});

describe("isTerminal", () => {
  it("treats done/failed/neutral/blocked as terminal", () => {
    expect(isTerminal("done")).toBe(true);
    expect(isTerminal("failed")).toBe(true);
    expect(isTerminal("neutral")).toBe(true);
    // cancelled → blocked is terminal so the wave doesn't wait out the timeout.
    expect(isTerminal("blocked")).toBe(true);
    expect(isTerminal("running")).toBe(false);
    expect(isTerminal("pending")).toBe(false);
  });
});

describe("runWaves (fail-closed wave gating)", () => {
  function node(id: string): LineNode {
    return {
      id,
      kind: "dev",
      executor: "claude",
      role: "dev",
      mode: "write",
      instruction: `run ${id}`,
      ownedPaths: [`src/${id}.ts`],
    };
  }

  // Build a dispatchNode closure that mirrors the component: it calls
  // quickCreateIssue per node, resolving with the task id or rejecting on
  // enqueue failure. The spy lets a test assert exactly which nodes reached
  // the queue.
  function makeDispatch(quickCreateIssue: (nodeId: string) => Promise<string>) {
    return async (n: LineNode): Promise<string | null> => quickCreateIssue(n.id);
  }

  it("does NOT dispatch the second wave when a first-wave node fails to enqueue", async () => {
    const quickCreateIssue = vi.fn(async (nodeId: string) => {
      if (nodeId === "w1") throw new Error("queue rejected");
      return `task-${nodeId}`;
    });

    await runWaves([[node("w1")], [node("w2")]], {
      dispatchNode: makeDispatch(quickCreateIssue),
      waitForWaveTerminal: async () => true,
      isNodeBlocked: () => false,
      log: () => {},
      isCancelled: () => false,
    });

    // First wave's node was attempted; the second wave never reached the queue.
    expect(quickCreateIssue).toHaveBeenCalledTimes(1);
    expect(quickCreateIssue).toHaveBeenCalledWith("w1");
    expect(quickCreateIssue).not.toHaveBeenCalledWith("w2");
  });

  it("stops enqueuing the rest of a wave once one node in it fails", async () => {
    const quickCreateIssue = vi.fn(async (nodeId: string) => {
      if (nodeId === "a") throw new Error("queue rejected");
      return `task-${nodeId}`;
    });

    await runWaves([[node("a"), node("b")], [node("c")]], {
      dispatchNode: makeDispatch(quickCreateIssue),
      waitForWaveTerminal: async () => true,
      isNodeBlocked: () => false,
      log: () => {},
      isCancelled: () => false,
    });

    // "a" failed → "b" (same wave) and "c" (next wave) are never enqueued.
    expect(quickCreateIssue).toHaveBeenCalledTimes(1);
    expect(quickCreateIssue).toHaveBeenCalledWith("a");
  });

  it("fails closed when a node cannot be dispatched (no agent → null)", async () => {
    const quickCreateIssue = vi.fn(async (nodeId: string) => `task-${nodeId}`);
    const dispatchNode = vi.fn(async (n: LineNode): Promise<string | null> => {
      if (n.id === "w1") return null; // no agent resolved
      return quickCreateIssue(n.id);
    });

    await runWaves([[node("w1")], [node("w2")]], {
      dispatchNode,
      waitForWaveTerminal: async () => true,
      isNodeBlocked: () => false,
      log: () => {},
      isCancelled: () => false,
    });

    expect(dispatchNode).toHaveBeenCalledTimes(1);
    expect(quickCreateIssue).not.toHaveBeenCalled();
  });

  it("dispatches every wave in order when all nodes enqueue and complete", async () => {
    const order: string[] = [];
    const quickCreateIssue = vi.fn(async (nodeId: string) => {
      order.push(nodeId);
      return `task-${nodeId}`;
    });

    await runWaves([[node("w1")], [node("w2")], [node("w3")]], {
      dispatchNode: makeDispatch(quickCreateIssue),
      waitForWaveTerminal: async () => true,
      isNodeBlocked: () => false,
      log: () => {},
      isCancelled: () => false,
    });

    expect(order).toEqual(["w1", "w2", "w3"]);
  });

  it("does NOT advance when a dispatched node settles failed via WS", async () => {
    const quickCreateIssue = vi.fn(async (nodeId: string) => `task-${nodeId}`);

    await runWaves([[node("w1")], [node("w2")]], {
      dispatchNode: makeDispatch(quickCreateIssue),
      waitForWaveTerminal: async () => true,
      isNodeBlocked: (id) => id === "w1",
      log: () => {},
      isCancelled: () => false,
    });

    expect(quickCreateIssue).toHaveBeenCalledTimes(1);
    expect(quickCreateIssue).not.toHaveBeenCalledWith("w2");
  });

  it("does NOT advance when a first-wave node is cancelled via WS (task:cancelled)", async () => {
    // The audit gap this covers: a real backend `task:cancelled` event used to
    // map to `neutral` (a PASSING terminal), so a cancelled upstream node let
    // the next wave enqueue. This wires the genuine mapping end-to-end —
    // wsEventToCircuit("task:cancelled") → status mirror → isTerminal →
    // isNodeBlocked — exactly as the component does, and asserts the second
    // wave never calls quickCreateIssue.
    const quickCreateIssue = vi.fn(async (nodeId: string) => `task-${nodeId}`);
    const statusMirror: Record<string, CircuitStatus> = {};

    const dispatchNode = async (n: LineNode): Promise<string | null> => {
      const taskId = await quickCreateIssue(n.id);
      // First-wave node receives a `task:cancelled` WS event, mapped the same
      // way the live colouring path maps it.
      if (n.id === "w1") statusMirror[n.id] = wsEventToCircuit("task:cancelled")!;
      return taskId;
    };
    const isNodeBlocked = (id: string) => {
      const s = statusMirror[id];
      return s === "failed" || s === "blocked";
    };

    await runWaves([[node("w1")], [node("w2")]], {
      dispatchNode,
      // Cancelled is terminal, so waitForWaveTerminal would resolve true here.
      waitForWaveTerminal: async () => true,
      isNodeBlocked,
      log: () => {},
      isCancelled: () => false,
    });

    // Sanity-check the mapping the gate relies on, then the gating outcome.
    expect(wsEventToCircuit("task:cancelled")).toBe("blocked");
    expect(isTerminal("blocked")).toBe(true);
    expect(quickCreateIssue).toHaveBeenCalledTimes(1);
    expect(quickCreateIssue).toHaveBeenCalledWith("w1");
    expect(quickCreateIssue).not.toHaveBeenCalledWith("w2");
  });

  it("does NOT advance when a wave times out before reaching terminal", async () => {
    const quickCreateIssue = vi.fn(async (nodeId: string) => `task-${nodeId}`);

    await runWaves([[node("w1")], [node("w2")]], {
      dispatchNode: makeDispatch(quickCreateIssue),
      waitForWaveTerminal: async () => false, // timeout / cancel
      isNodeBlocked: () => false,
      log: () => {},
      isCancelled: () => false,
    });

    expect(quickCreateIssue).toHaveBeenCalledTimes(1);
    expect(quickCreateIssue).not.toHaveBeenCalledWith("w2");
  });

  it("stops before dispatching when cancelled", async () => {
    const quickCreateIssue = vi.fn(async (nodeId: string) => `task-${nodeId}`);

    await runWaves([[node("w1")]], {
      dispatchNode: makeDispatch(quickCreateIssue),
      waitForWaveTerminal: async () => true,
      isNodeBlocked: () => false,
      log: () => {},
      isCancelled: () => true,
    });

    expect(quickCreateIssue).not.toHaveBeenCalled();
  });
});
