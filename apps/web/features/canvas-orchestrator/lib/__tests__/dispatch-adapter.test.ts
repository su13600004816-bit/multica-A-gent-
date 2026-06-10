import { describe, expect, it } from "vitest";
import type { Agent } from "@multica/core/types";

import {
  buildNodePrompt,
  compileNodeToQueueRequest,
  isTerminal,
  resolveAgentForExecutor,
  taskStatusToCircuit,
  wsEventToCircuit,
} from "../dispatch-adapter";
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
    expect(wsEventToCircuit("task:cancelled")).toBe("neutral");
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
    expect(taskStatusToCircuit("cancelled")).toBe("neutral");
    expect(taskStatusToCircuit("some_future_state")).toBe("neutral");
    expect(taskStatusToCircuit(undefined)).toBe("neutral");
  });
});

describe("isTerminal", () => {
  it("treats done/failed/neutral as terminal", () => {
    expect(isTerminal("done")).toBe(true);
    expect(isTerminal("failed")).toBe(true);
    expect(isTerminal("neutral")).toBe(true);
    expect(isTerminal("running")).toBe(false);
    expect(isTerminal("pending")).toBe(false);
  });
});
