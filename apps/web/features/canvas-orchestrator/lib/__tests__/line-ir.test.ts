import { describe, expect, it } from "vitest";
import {
  compileToWaves,
  createLineNode,
  isTerminalCircuitStatus,
  normalizeOwnedPath,
  parseVerdict,
  resolveExternalTaskId,
  statusClassToCircuit,
  upstreamWriteNodes,
  validateLine,
  type LineEdge,
  type LineNode,
  type ProductionLine,
} from "../line-ir";

// Build a write/dev node with sensible defaults; id-driven so tests are stable.
function dev(id: string, ownedPaths: string[] = [`src/${id}.ts`]): LineNode {
  return {
    id,
    kind: "dev",
    executor: "claude",
    role: "dev",
    mode: "write",
    instruction: `do ${id}`,
    ownedPaths,
  };
}

function audit(id: string): LineNode {
  return {
    id,
    kind: "audit",
    executor: "codex",
    role: "audit",
    mode: "audit",
    instruction: `audit ${id}`,
    ownedPaths: [],
  };
}

function edge(source: string, target: string): LineEdge {
  return { id: `${source}->${target}`, source, target };
}

function line(nodes: LineNode[], edges: LineEdge[] = []): ProductionLine {
  return { id: "line-test", title: "test", nodes, edges };
}

describe("compileToWaves (Kahn layering)", () => {
  it("returns no waves for an empty line", () => {
    expect(compileToWaves(line([]))).toEqual([]);
  });

  it("puts independent nodes in a single wave", () => {
    const waves = compileToWaves(line([dev("a"), dev("b"), dev("c")]));
    expect(waves).toHaveLength(1);
    expect(waves[0]?.map((n) => n.id).sort()).toEqual(["a", "b", "c"]);
  });

  it("layers a linear chain into serial waves", () => {
    const l = line([dev("a"), dev("b"), audit("c")], [edge("a", "b"), edge("b", "c")]);
    const waves = compileToWaves(l).map((w) => w.map((n) => n.id));
    expect(waves).toEqual([["a"], ["b"], ["c"]]);
  });

  it("runs a diamond's middle nodes in the same wave", () => {
    // a -> b, a -> c, b -> d, c -> d  =>  [a], [b,c], [d]
    const l = line(
      [dev("a", ["src/a.ts"]), dev("b", ["src/b.ts"]), dev("c", ["src/c.ts"]), audit("d")],
      [edge("a", "b"), edge("a", "c"), edge("b", "d"), edge("c", "d")],
    );
    const waves = compileToWaves(l).map((w) => w.map((n) => n.id).sort());
    expect(waves).toEqual([["a"], ["b", "c"], ["d"]]);
  });
});

describe("validateLine", () => {
  it("accepts a well-formed DAG", () => {
    const l = line([dev("a"), audit("b")], [edge("a", "b")]);
    expect(validateLine(l)).toEqual({ ok: true, errors: [] });
  });

  it("rejects an empty line", () => {
    const r = validateLine(line([]));
    expect(r.ok).toBe(false);
    expect(r.errors.some((e) => e.includes("生产线为空"))).toBe(true);
  });

  it("flags duplicate node ids", () => {
    const r = validateLine(line([dev("a"), dev("a")]));
    expect(r.ok).toBe(false);
    expect(r.errors.some((e) => e.includes("节点 id 重复"))).toBe(true);
  });

  it("flags a missing instruction", () => {
    const n = dev("a");
    n.instruction = "   ";
    const r = validateLine(line([n]));
    expect(r.errors.some((e) => e.includes("缺少 instruction"))).toBe(true);
  });

  it("requires ownedPaths on write nodes", () => {
    const r = validateLine(line([dev("a", [])]));
    expect(r.errors.some((e) => e.includes("必须声明 ownedPaths"))).toBe(true);
  });

  it("rejects illegal owned paths (absolute / traversal)", () => {
    const abs = validateLine(line([dev("a", ["/etc/passwd"])]));
    expect(abs.errors.some((e) => e.includes("非法"))).toBe(true);
    const trav = validateLine(line([dev("b", ["../secret.ts"])]));
    expect(trav.errors.some((e) => e.includes("非法"))).toBe(true);
  });

  it("detects a cycle", () => {
    const l = line([dev("a"), dev("b")], [edge("a", "b"), edge("b", "a")]);
    const r = validateLine(l);
    expect(r.errors.some((e) => e.includes("环"))).toBe(true);
  });

  it("flags edges referencing missing endpoints", () => {
    const r = validateLine(line([dev("a")], [edge("a", "ghost")]));
    expect(r.errors.some((e) => e.includes("目标节点不存在"))).toBe(true);
  });

  it("flags overlapping ownedPaths within the same wave (containment)", () => {
    // two independent write nodes in wave 0 claiming overlapping paths
    const r = validateLine(line([dev("a", ["src/feature"]), dev("b", ["src/feature/x.ts"])]));
    expect(r.ok).toBe(false);
    expect(r.errors.some((e) => e.includes("ownedPaths 冲突"))).toBe(true);
  });

  it("allows the same path in different waves", () => {
    // a (wave0) and b (wave1) may both touch src/shared.ts since they serialize
    const l = line(
      [dev("a", ["src/shared.ts"]), dev("b", ["src/shared.ts"])],
      [edge("a", "b")],
    );
    expect(validateLine(l).ok).toBe(true);
  });
});

describe("parseVerdict (fail-closed)", () => {
  it("reads an explicit VERDICT line", () => {
    expect(parseVerdict("blah\nVERDICT: PASS\n")).toBe("PASS");
    expect(parseVerdict("VERDICT: fail")).toBe("FAIL");
  });
  it("falls back to a bare token", () => {
    expect(parseVerdict("looks good, PASS")).toBe("PASS");
  });
  it("defaults to FAIL on empty/ambiguous input", () => {
    expect(parseVerdict(undefined)).toBe("FAIL");
    expect(parseVerdict("")).toBe("FAIL");
    expect(parseVerdict("no verdict here")).toBe("FAIL");
  });
});

describe("rework helpers", () => {
  it("finds the upstream write nodes feeding an audit node", () => {
    const l = line(
      [dev("a"), dev("b"), audit("gate")],
      [edge("a", "gate"), edge("b", "gate")],
    );
    expect(upstreamWriteNodes(l, "gate").map((n) => n.id).sort()).toEqual(["a", "b"]);
  });
  it("excludes non-write upstream nodes", () => {
    const l = line([dev("a"), audit("pre"), audit("gate")], [edge("pre", "gate"), edge("a", "gate")]);
    expect(upstreamWriteNodes(l, "gate").map((n) => n.id)).toEqual(["a"]);
  });
});

describe("status + helpers", () => {
  it("maps agent-control status classes to circuit status", () => {
    expect(statusClassToCircuit("running")).toBe("running");
    expect(statusClassToCircuit("done")).toBe("done");
    expect(statusClassToCircuit("failed")).toBe("failed");
    expect(statusClassToCircuit("blocked")).toBe("failed");
    expect(statusClassToCircuit("queued")).toBe("pending");
    expect(statusClassToCircuit(undefined)).toBe("neutral");
  });
  it("knows terminal statuses", () => {
    expect(isTerminalCircuitStatus("done")).toBe(true);
    expect(isTerminalCircuitStatus("running")).toBe(false);
  });
  it("normalizes owned paths", () => {
    expect(normalizeOwnedPath("./src/a/")).toBe("src/a");
    expect(normalizeOwnedPath("src\\b\\c")).toBe("src/b/c");
  });
  it("defaults external task id to lineId:nodeId", () => {
    expect(resolveExternalTaskId(dev("a"), "L1")).toBe("L1:a");
    const withId = { ...dev("a"), externalTaskId: "custom" };
    expect(resolveExternalTaskId(withId, "L1")).toBe("custom");
  });
});

describe("factory", () => {
  it("creates dev/audit nodes with the expected mode/executor defaults", () => {
    const d = createLineNode("dev");
    expect(d.kind).toBe("dev");
    expect(d.mode).toBe("write");
    expect(d.executor).toBe("claude");
    const a = createLineNode("audit");
    expect(a.kind).toBe("audit");
    expect(a.mode).toBe("audit");
    expect(a.executor).toBe("codex");
  });
});
