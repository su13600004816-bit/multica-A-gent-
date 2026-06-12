// Self-contained logic-graph module (independent package on main; the canvas and
// other frontends inherit it from main rather than living on a canvas branch).
// No react / @xyflow dependency at the top: graph model + auto-layout +
// React Flow mapping + service client, all pure TS — any canvas surface can use it.
export type { LogicNode, LogicEdge, LogicGroup, LogicGraph, GraphIssue } from "./graph";
export { emptyGraph, validateGraph } from "./graph";
export { layeredLayout, type XY } from "./layout";
export { graphToFlow, type FlowNode, type FlowEdge } from "./to-flow";
export { LogicGraphClient, type LogicGraphClientOptions } from "./client";
