// Deterministic layered auto-layout for a logic graph. The model only declares
// relationships; this gives every node an (x, y) so the canvas renders cleanly
// without the LLM (or the user) having to position anything. Pure TS.
import type { LogicGraph } from "./graph";

export type XY = { x: number; y: number };

const GAP_X = 240; // horizontal gap between layers
const GAP_Y = 110; // vertical gap between siblings in a layer

// Assign layers by longest-path from roots (nodes with no incoming edge), then
// place each layer in a column, spreading its nodes vertically and centring.
export function layeredLayout(graph: LogicGraph): Record<string, XY> {
  const nodes = graph.nodes ?? [];
  const edges = graph.edges ?? [];
  if (nodes.length === 0) return {};

  const ids = nodes.map((n) => n.id);
  const idSet = new Set(ids);
  const indeg = new Map<string, number>();
  const out = new Map<string, string[]>();
  for (const id of ids) {
    indeg.set(id, 0);
    out.set(id, []);
  }
  for (const e of edges) {
    if (!idSet.has(e.from) || !idSet.has(e.to) || e.from === e.to) continue;
    out.get(e.from)!.push(e.to);
    indeg.set(e.to, (indeg.get(e.to) ?? 0) + 1);
  }

  // Longest-path layering via Kahn-style relaxation (cycle-safe: a node already
  // seen keeps its first layer; back-edges don't push it further).
  const layer = new Map<string, number>();
  const queue = ids.filter((id) => (indeg.get(id) ?? 0) === 0);
  for (const id of queue) layer.set(id, 0);
  // any node still unlayered (pure cycle) starts at 0
  for (const id of ids) if (!layer.has(id)) layer.set(id, 0);

  let changed = true;
  let guard = 0;
  while (changed && guard < ids.length + 2) {
    changed = false;
    guard++;
    for (const e of edges) {
      if (!idSet.has(e.from) || !idSet.has(e.to) || e.from === e.to) continue;
      const want = (layer.get(e.from) ?? 0) + 1;
      if (want > (layer.get(e.to) ?? 0) && want <= ids.length) {
        layer.set(e.to, want);
        changed = true;
      }
    }
  }

  // group nodes by layer, preserving declaration order for stable y
  const byLayer = new Map<number, string[]>();
  for (const id of ids) {
    const l = layer.get(id) ?? 0;
    (byLayer.get(l) ?? byLayer.set(l, []).get(l)!).push(id);
  }

  // reduce (not Math.max(...spread)) so a very wide layer from an LLM-generated
  // graph can't blow the argument stack with a huge spread.
  const maxCount = [...byLayer.values()].reduce((m, a) => Math.max(m, a.length), 1);
  const pos: Record<string, XY> = {};
  for (const [l, members] of [...byLayer.entries()].sort((a, b) => a[0] - b[0])) {
    const colHeight = (members.length - 1) * GAP_Y;
    const offset = ((maxCount - 1) * GAP_Y - colHeight) / 2; // centre each column
    members.forEach((id, i) => {
      pos[id] = { x: l * GAP_X, y: offset + i * GAP_Y };
    });
  }
  return pos;
}
