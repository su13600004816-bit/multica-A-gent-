// Map a logic graph → React Flow nodes/edges (plain objects, so this stays free
// of any @xyflow import and can live in packages/core). The canvas surface that
// renders these registers a single "logicNode" type; node.data.kind carries the
// logic type for styling, so the visual language is owned by the renderer, not
// hard-coded here.
import type { LogicGraph } from "./graph";
import { layeredLayout } from "./layout";
// NOTE: edge dashing lives in the renderer (logic-graph-canvas.tsx owns the
// DASHED set), since dashing is a visual concern. This module stays headless.

export type FlowNode = {
  id: string;
  type: "logicNode";
  position: { x: number; y: number };
  data: { label: string; kind: string };
};

export type FlowEdge = {
  id: string;
  source: string;
  target: string;
  label?: string;
  data: { kind: string };
  animated?: boolean;
};

export function graphToFlow(graph: LogicGraph): { nodes: FlowNode[]; edges: FlowEdge[] } {
  const pos = layeredLayout(graph);
  const nodes: FlowNode[] = (graph.nodes ?? []).map((n) => ({
    id: n.id,
    type: "logicNode",
    position: pos[n.id] ?? { x: 0, y: 0 },
    data: { label: n.label || n.id, kind: n.type || "node" },
  }));
  const idSet = new Set(nodes.map((n) => n.id));
  const seen = new Set<string>();
  const edges: FlowEdge[] = [];
  for (const e of graph.edges ?? []) {
    if (!idSet.has(e.from) || !idSet.has(e.to)) continue; // skip dangling (validation reports it)
    let id = `${e.from}->${e.to}:${e.type}`;
    while (seen.has(id)) id += "_"; // keep edge ids unique for parallel relations
    seen.add(id);
    edges.push({
      id,
      source: e.from,
      target: e.to,
      label: e.label || undefined,
      data: { kind: e.type || "edge" },
      animated: e.type === "triggers" || e.type === "escalates",
    });
  }
  return { nodes, edges };
}
