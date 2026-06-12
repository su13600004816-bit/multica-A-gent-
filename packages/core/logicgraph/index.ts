// 自包含逻辑图模块（独立体，落主干 main；画布/其它前端从 main 继承,不寄生画布分支）。
// 头部无 react / @xyflow 依赖：图模型 + 自动布局 + → React Flow 映射 + 服务客户端,
// 全是纯 TS,任何画布表面拿来即用。
export type { LogicNode, LogicEdge, LogicGroup, LogicGraph, GraphIssue } from "./graph";
export { emptyGraph, validateGraph } from "./graph";
export { layeredLayout, type XY } from "./layout";
export { graphToFlow, type FlowNode, type FlowEdge } from "./to-flow";
export { LogicGraphClient, type LogicGraphClientOptions } from "./client";
