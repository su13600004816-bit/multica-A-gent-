// ReactFlow ⇄ ProductionLine mapping + dagre auto-layout for the canvas
// orchestrator (PL-157 phase 2). Pure, framework-light helpers so the canvas
// state/undo-redo logic can be unit-tested without mounting React.
//
// The compiler IR (`line-ir.ts`) stays the single source of truth for a line;
// these helpers only translate that IR into the @xyflow/react node/edge shape
// the canvas renders, and back.

import dagre from "dagre";
import type { Edge, Node, XYPosition } from "@xyflow/react";

import type { CircuitStatus } from "./circuit-status";
import {
  type LineEdge,
  type LineNode,
  type LineNodeKind,
  type ProductionLine,
  createLineEdge,
  createLineNode,
} from "./line-ir";

export const CIRCUIT_NODE_TYPE = "circuit" as const;

// Data carried on each ReactFlow node. `node` is the canonical IR record;
// `status` is the runtime colour fed from the WS-backed store (never a fetch
// inside the component — see README decoupling point 2).
export interface CircuitNodeData extends Record<string, unknown> {
  node: LineNode;
  status: CircuitStatus;
}

export type CircuitRfNode = Node<CircuitNodeData, typeof CIRCUIT_NODE_TYPE>;
export type CircuitRfEdge = Edge;

// Rendered node footprint — kept in sync with the CircuitNode component so
// dagre lays out without overlap.
export const NODE_WIDTH = 240;
export const NODE_HEIGHT = 96;

function toRfNode(node: LineNode, status: CircuitStatus): CircuitRfNode {
  return {
    id: node.id,
    type: CIRCUIT_NODE_TYPE,
    position: node.position ?? { x: 0, y: 0 },
    data: { node, status },
  };
}

function toRfEdge(edge: LineEdge): CircuitRfEdge {
  return {
    id: edge.id,
    source: edge.source,
    target: edge.target,
    animated: true,
  };
}

export function lineToRf(
  line: ProductionLine,
  statusById: Readonly<Record<string, CircuitStatus>> = {},
): { nodes: CircuitRfNode[]; edges: CircuitRfEdge[] } {
  return {
    nodes: line.nodes.map((n) => toRfNode(n, statusById[n.id] ?? "neutral")),
    edges: line.edges.map(toRfEdge),
  };
}

export function rfToLine(
  nodes: CircuitRfNode[],
  edges: CircuitRfEdge[],
  id: string,
  title: string,
): ProductionLine {
  return {
    id,
    title,
    nodes: nodes.map((rf) => ({ ...rf.data.node, position: rf.position })),
    edges: edges.map((e) => ({ id: e.id, source: e.source, target: e.target })),
  };
}

// Append a fresh node of the given kind, dropped near the canvas centre.
export function addNode(
  nodes: CircuitRfNode[],
  kind: LineNodeKind,
  position?: XYPosition,
): CircuitRfNode[] {
  const node = createLineNode(kind, position ?? { x: 80, y: 80 });
  return [...nodes, toRfNode(node, "draft")];
}

// Connect two nodes, ignoring self-loops and exact duplicates (ReactFlow can
// fire onConnect twice on a fast drag).
export function connectNodes(
  edges: CircuitRfEdge[],
  source: string,
  target: string,
): CircuitRfEdge[] {
  if (source === target) return edges;
  if (edges.some((e) => e.source === source && e.target === target)) return edges;
  return [...edges, toRfEdge(createLineEdge(source, target))];
}

export function removeNode(
  nodes: CircuitRfNode[],
  edges: CircuitRfEdge[],
  nodeId: string,
): { nodes: CircuitRfNode[]; edges: CircuitRfEdge[] } {
  return {
    nodes: nodes.filter((n) => n.id !== nodeId),
    edges: edges.filter((e) => e.source !== nodeId && e.target !== nodeId),
  };
}

// Dagre left-to-right layered layout. Mirrors the legacy console's config
// (rankdir LR, ranksep 120, nodesep 60) so a compiled line reads as waves
// flowing left → right.
export function autoLayout(
  nodes: CircuitRfNode[],
  edges: CircuitRfEdge[],
): CircuitRfNode[] {
  const g = new dagre.graphlib.Graph();
  g.setDefaultEdgeLabel(() => ({}));
  g.setGraph({ rankdir: "LR", ranksep: 120, nodesep: 60 });

  nodes.forEach((n) => g.setNode(n.id, { width: NODE_WIDTH, height: NODE_HEIGHT }));
  edges.forEach((e) => g.setEdge(e.source, e.target));

  dagre.layout(g);

  return nodes.map((n) => {
    const pos = g.node(n.id);
    if (!pos) return n;
    return {
      ...n,
      // dagre returns the node centre; ReactFlow positions the top-left corner.
      position: { x: pos.x - NODE_WIDTH / 2, y: pos.y - NODE_HEIGHT / 2 },
    };
  });
}

// Reset every node colour to a baseline (used before a run, or on clear).
export function applyStatuses(
  nodes: CircuitRfNode[],
  statusById: Readonly<Record<string, CircuitStatus>>,
  fallback: CircuitStatus = "neutral",
): CircuitRfNode[] {
  return nodes.map((n) => ({
    ...n,
    data: { ...n.data, status: statusById[n.id] ?? fallback },
  }));
}

// T01 reference line (ingest → compress → validate → chat-gate → audit), kept
// from the legacy preset so reviewers can lay down the canonical demo line in
// one click. Instructions are deliberately short/benign — the real dispatch
// prompt is assembled by the dispatch adapter.
export const T01_PRESET: ProductionLine = {
  id: "line-t01",
  title: "T01 图片压缩生产线",
  nodes: [
    {
      id: "t01-ingest",
      kind: "dev",
      executor: "codex",
      role: "dev",
      mode: "write",
      instruction: "维护图片候选检测与文件名 MIME 归一化（压缩前置）。",
      ownedPaths: ["src/components/ChatPanel.tsx"],
      position: { x: 0, y: 0 },
    },
    {
      id: "t01-compress",
      kind: "dev",
      executor: "codex",
      role: "dev",
      mode: "write",
      instruction: "将图片压成 WebP，长边 ≤1568px，输出控制在上传字节上限内。",
      ownedPaths: ["src/lib/imageCompress.ts", "src/lib/uploadConfig.ts"],
      position: { x: 0, y: 0 },
    },
    {
      id: "t01-validate",
      kind: "dev",
      executor: "codex",
      role: "dev",
      mode: "write",
      instruction: "拒绝非 WebP、超尺寸、非法 payload 与长边超限的图片。",
      ownedPaths: ["src/app/api/upload/image/route.ts"],
      position: { x: 0, y: 0 },
    },
    {
      id: "t01-chat-gate",
      kind: "dev",
      executor: "codex",
      role: "dev",
      mode: "write",
      instruction: "确保 chat / stream 路由只转发已压缩的 WebP 图片附件。",
      ownedPaths: ["src/app/api/agent-chat/route.ts", "src/app/api/agent-stream/route.ts"],
      position: { x: 0, y: 0 },
    },
    {
      id: "t01-audit",
      kind: "audit",
      executor: "codex",
      role: "audit",
      mode: "audit",
      instruction:
        "审计 T01：WebP-only、上传上限、长边 ≤1568px、状态看板可见、QA 证据齐全。给出 VERDICT: PASS/FAIL。",
      ownedPaths: [],
      position: { x: 0, y: 0 },
    },
  ],
  edges: [
    { id: "e-t01-1", source: "t01-ingest", target: "t01-compress" },
    { id: "e-t01-2", source: "t01-compress", target: "t01-validate" },
    { id: "e-t01-3", source: "t01-validate", target: "t01-chat-gate" },
    { id: "e-t01-4", source: "t01-chat-gate", target: "t01-audit" },
  ],
};
