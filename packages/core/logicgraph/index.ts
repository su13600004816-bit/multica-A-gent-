// Self-contained logic-graph module (standalone; lands on main. The canvas and
// other frontends inherit it from main instead of parasitizing a canvas branch).
// No react / @xyflow at the top: graph model + auto-layout + → React Flow mapping
// + service client, all pure TS, ready for any canvas surface to use.
export type { LogicNode, LogicEdge, LogicGroup, LogicGraph, GraphIssue } from "./graph";
export { emptyGraph, validateGraph } from "./graph";
export { layeredLayout, type XY } from "./layout";
export { graphToFlow, type FlowNode, type FlowEdge } from "./to-flow";
export { LogicGraphClient, type LogicGraphClientOptions } from "./client";
