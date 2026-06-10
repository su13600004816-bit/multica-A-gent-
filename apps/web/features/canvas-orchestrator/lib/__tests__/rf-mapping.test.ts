import { describe, expect, it } from "vitest";

import {
  addNode,
  applyStatuses,
  autoLayout,
  connectNodes,
  lineToRf,
  removeNode,
  rfToLine,
  T01_PRESET,
  CIRCUIT_NODE_TYPE,
} from "../rf-mapping";

describe("lineToRf / rfToLine round-trip", () => {
  it("maps every node and edge to the circuit node type", () => {
    const { nodes, edges } = lineToRf(T01_PRESET);
    expect(nodes).toHaveLength(T01_PRESET.nodes.length);
    expect(edges).toHaveLength(T01_PRESET.edges.length);
    expect(nodes.every((n) => n.type === CIRCUIT_NODE_TYPE)).toBe(true);
    expect(nodes.every((n) => n.data.status === "neutral")).toBe(true);
    expect(edges.every((e) => e.animated === true)).toBe(true);
  });

  it("seeds per-node status from the supplied map", () => {
    const { nodes } = lineToRf(T01_PRESET, { "t01-audit": "running" });
    const audit = nodes.find((n) => n.id === "t01-audit");
    expect(audit?.data.status).toBe("running");
  });

  it("round-trips back to an equivalent ProductionLine, capturing moved positions", () => {
    const { nodes, edges } = lineToRf(T01_PRESET);
    nodes[0]!.position = { x: 123, y: 456 };
    const line = rfToLine(nodes, edges, "line-x", "标题");
    expect(line.id).toBe("line-x");
    expect(line.title).toBe("标题");
    expect(line.nodes).toHaveLength(T01_PRESET.nodes.length);
    expect(line.nodes[0]!.position).toEqual({ x: 123, y: 456 });
    expect(line.edges.map((e) => [e.source, e.target])).toEqual(
      T01_PRESET.edges.map((e) => [e.source, e.target]),
    );
  });
});

describe("addNode", () => {
  it("appends a node of the requested kind", () => {
    const before = lineToRf(T01_PRESET).nodes;
    const after = addNode(before, "audit", { x: 10, y: 20 });
    expect(after).toHaveLength(before.length + 1);
    const added = after[after.length - 1]!;
    expect(added.data.node.kind).toBe("audit");
    expect(added.data.node.role).toBe("audit");
    expect(added.position).toEqual({ x: 10, y: 20 });
    expect(added.data.status).toBe("draft");
  });
});

describe("connectNodes", () => {
  const { nodes } = lineToRf(T01_PRESET);
  const a = nodes[0]!.id;
  const b = nodes[1]!.id;

  it("adds a new edge", () => {
    const edges = connectNodes([], a, b);
    expect(edges).toHaveLength(1);
    expect(edges[0]!.source).toBe(a);
    expect(edges[0]!.target).toBe(b);
  });

  it("ignores self-loops", () => {
    expect(connectNodes([], a, a)).toHaveLength(0);
  });

  it("dedupes an identical edge", () => {
    const once = connectNodes([], a, b);
    expect(connectNodes(once, a, b)).toHaveLength(1);
  });
});

describe("removeNode", () => {
  it("drops the node and every connected edge", () => {
    const { nodes, edges } = lineToRf(T01_PRESET);
    const res = removeNode(nodes, edges, "t01-compress");
    expect(res.nodes.find((n) => n.id === "t01-compress")).toBeUndefined();
    expect(res.edges.some((e) => e.source === "t01-compress" || e.target === "t01-compress")).toBe(
      false,
    );
    // the two edges touching t01-compress are gone; the other two remain
    expect(res.edges).toHaveLength(edges.length - 2);
  });
});

describe("autoLayout", () => {
  it("lays a linear chain out left-to-right with increasing x", () => {
    const { nodes, edges } = lineToRf(T01_PRESET);
    const laid = autoLayout(nodes, edges);
    const xById = new Map(laid.map((n) => [n.id, n.position.x]));
    expect(xById.get("t01-ingest")!).toBeLessThan(xById.get("t01-compress")!);
    expect(xById.get("t01-compress")!).toBeLessThan(xById.get("t01-validate")!);
    expect(xById.get("t01-validate")!).toBeLessThan(xById.get("t01-chat-gate")!);
    expect(xById.get("t01-chat-gate")!).toBeLessThan(xById.get("t01-audit")!);
  });
});

describe("applyStatuses", () => {
  it("recolours nodes from the status map and falls back otherwise", () => {
    const { nodes } = lineToRf(T01_PRESET);
    const recoloured = applyStatuses(nodes, { "t01-ingest": "done" }, "pending");
    expect(recoloured.find((n) => n.id === "t01-ingest")!.data.status).toBe("done");
    expect(recoloured.find((n) => n.id === "t01-audit")!.data.status).toBe("pending");
  });
});
