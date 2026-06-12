// Self-contained logic-graph model (portable; mirrors the standalone logicgraph
// service's JSON shape). Pure TS — no react, no @xyflow, no DOM. The headless
// heart of the 逻辑图 feature: it lives on main as an independent package so the
// canvas (and any other surface) INHERITS it via merge, instead of the feature
// being grafted onto a canvas branch.

export type LogicNode = {
  id: string;
  type: string; // brain | actor | gate | watchdog | store | line | decision | ...
  label: string;
  attrs?: Record<string, unknown>;
};

export type LogicEdge = {
  from: string;
  to: string;
  type: string; // flows_to | contains | monitors | interconnects | blocks | triggers | escalates | ...
  label?: string;
};

export type LogicGroup = { id: string; label: string; members: string[] };

export type LogicGraph = {
  meta?: { name?: string; description?: string; direction?: string };
  nodes: LogicNode[];
  edges: LogicEdge[];
  groups?: LogicGroup[];
  rules?: Record<string, unknown>;
};

export type GraphIssue = {
  severity: "error" | "warn" | "info";
  code: string;
  message: string;
  where?: string;
};

export function emptyGraph(name?: string): LogicGraph {
  return { meta: { name }, nodes: [], edges: [], groups: [] };
}

// Light structural validation — the standalone service does the heavy
// completeness rules; here we only catch what the UI needs to render safely.
export function validateGraph(g: LogicGraph): GraphIssue[] {
  const issues: GraphIssue[] = [];
  const ids = new Set<string>();
  for (const n of g.nodes ?? []) {
    if (ids.has(n.id)) {
      issues.push({ severity: "error", code: "DUP_NODE", message: `duplicate node ${n.id}`, where: n.id });
    }
    ids.add(n.id);
  }
  for (const e of g.edges ?? []) {
    for (const end of [e.from, e.to]) {
      if (!ids.has(end)) {
        issues.push({
          severity: "error",
          code: "DANGLING_EDGE",
          message: `edge ${e.from}→${e.to} references missing node ${end}`,
          where: `${e.from}->${e.to}`,
        });
      }
    }
  }
  const connected = new Set<string>();
  for (const e of g.edges ?? []) {
    connected.add(e.from);
    connected.add(e.to);
  }
  for (const n of g.nodes ?? []) {
    if (!connected.has(n.id)) {
      issues.push({ severity: "warn", code: "ORPHAN", message: `node ${n.id} has no connections`, where: n.id });
    }
  }
  return issues;
}
