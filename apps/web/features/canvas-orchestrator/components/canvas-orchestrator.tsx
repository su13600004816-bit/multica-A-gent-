"use client";

// Canvas line-orchestrator (PL-157 phase 2): draw a production line as a DAG of
// dev/audit nodes, auto-layout with dagre, undo/redo, then dispatch each
// topological wave onto the MULTICA TASK QUEUE (one quick-create issue per node
// → an `agent_task_queue` row) and colour nodes live from `task:*` WS events.
// No legacy agent-control transport, no DB persistence / autonomous rework
// (those are phase 3).

import "@xyflow/react/dist/style.css";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Background,
  Controls,
  MiniMap,
  ReactFlow,
  ReactFlowProvider,
  applyEdgeChanges,
  applyNodeChanges,
  type Connection,
  type EdgeChange,
  type NodeChange,
} from "@xyflow/react";
import {
  ListRestart,
  Play,
  Plus,
  Redo2,
  ShieldPlus,
  Square,
  Trash2,
  Undo2,
  Wand2,
} from "lucide-react";

import { api } from "@multica/core/api";
import { useWorkspaceId } from "@multica/core/hooks";
import { useWSEvent } from "@multica/core/realtime";
import type { Agent } from "@multica/core/types";
import { useQuery } from "@tanstack/react-query";
import { Button } from "@multica/ui/components/ui/button";
import { ScrollArea } from "@multica/ui/components/ui/scroll-area";
import { Separator } from "@multica/ui/components/ui/separator";
import { cn } from "@multica/ui/lib/utils";

import type { CircuitStatus } from "../lib/circuit-status";
import {
  compileToWaves,
  freshLineId,
  validateLine,
  type LineNode,
} from "../lib/line-ir";
import {
  compileNodeToQueueRequest,
  isTerminal,
  resolveAgentForExecutor,
  resolveExternalTaskId,
  wsEventToCircuit,
} from "../lib/dispatch-adapter";
import {
  addNode,
  applyStatuses,
  autoLayout,
  connectNodes,
  lineToRf,
  removeNode,
  rfToLine,
  T01_PRESET,
  type CircuitNodeData,
  type CircuitRfEdge,
  type CircuitRfNode,
} from "../lib/rf-mapping";
import {
  canRedo,
  canUndo,
  commit,
  initHistory,
  redo,
  undo,
  type CanvasHistory,
  type CanvasSnapshot,
} from "../lib/canvas-history";
import { CircuitNode } from "./circuit-node";
import { NodeInspector } from "./node-inspector";

const nodeTypes = { circuit: CircuitNode };

const EDGE_STROKE: Partial<Record<CircuitStatus, string>> = {
  running: "var(--info)",
  done: "var(--success)",
  failed: "var(--destructive)",
};

// Poll cadence + ceiling while waiting for a wave's tasks to reach a terminal
// status over WS. This polls a local ref (NOT the server) — the server pushes
// status via WS; we just gate wave N+1 on wave N completing.
const WAVE_POLL_MS = 700;
const WAVE_TIMEOUT_MS = 15 * 60 * 1000;

const EMPTY_SNAPSHOT: CanvasSnapshot = { nodes: [], edges: [] };

function CanvasOrchestratorInner() {
  const wsId = useWorkspaceId();
  const { data: agents = [] } = useQuery<Agent[]>({
    queryKey: ["agents", wsId],
    queryFn: () => api.listAgents({ workspace_id: wsId }),
  });

  const [history, setHistory] = useState<CanvasHistory>(() => initHistory(EMPTY_SNAPSHOT));
  const [statusById, setStatusById] = useState<Record<string, CircuitStatus>>({});
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [lineId, setLineId] = useState<string>(() => "line-draft");
  const [lineTitle] = useState<string>("画布生产线");
  const [running, setRunning] = useState(false);
  const [log, setLog] = useState<string[]>([]);

  // Live status mirror + task→node index for the WS-driven colouring path,
  // and a cancel flag the execute loop checks between waves.
  const statusRef = useRef<Record<string, CircuitStatus>>({});
  const taskToNode = useRef<Map<string, string>>(new Map());
  const cancelRef = useRef(false);
  const dragBaseline = useRef<CanvasSnapshot | null>(null);

  // Assign a stable client-side line id after mount (avoids SSR hydration
  // mismatch from a random id).
  useEffect(() => {
    setLineId(freshLineId());
  }, []);

  const present = history.present;

  const pushLog = useCallback((line: string) => {
    setLog((prev) => [...prev.slice(-199), line]);
  }, []);

  const setStatus = useCallback((nodeId: string, status: CircuitStatus) => {
    statusRef.current = { ...statusRef.current, [nodeId]: status };
    setStatusById((prev) => ({ ...prev, [nodeId]: status }));
  }, []);

  // --- live colouring from task:* WS events --------------------------------
  const handleTaskEvent = useCallback(
    (event: Parameters<typeof wsEventToCircuit>[0]) =>
      (payload: unknown) => {
        const taskId = (payload as { task_id?: string } | null)?.task_id;
        if (!taskId) return;
        const nodeId = taskToNode.current.get(taskId);
        if (!nodeId) return;
        const status = wsEventToCircuit(event);
        if (status) setStatus(nodeId, status);
      },
    [setStatus],
  );

  useWSEvent("task:queued", useMemo(() => handleTaskEvent("task:queued"), [handleTaskEvent]));
  useWSEvent("task:dispatch", useMemo(() => handleTaskEvent("task:dispatch"), [handleTaskEvent]));
  useWSEvent("task:running", useMemo(() => handleTaskEvent("task:running"), [handleTaskEvent]));
  useWSEvent("task:progress", useMemo(() => handleTaskEvent("task:progress"), [handleTaskEvent]));
  useWSEvent("task:completed", useMemo(() => handleTaskEvent("task:completed"), [handleTaskEvent]));
  useWSEvent("task:failed", useMemo(() => handleTaskEvent("task:failed"), [handleTaskEvent]));
  useWSEvent("task:cancelled", useMemo(() => handleTaskEvent("task:cancelled"), [handleTaskEvent]));

  // --- canvas mutations -----------------------------------------------------
  const onNodesChange = useCallback((changes: NodeChange[]) => {
    setHistory((h) => {
      const nodes = applyNodeChanges(changes, h.present.nodes) as CircuitRfNode[];
      const next: CanvasSnapshot = { nodes, edges: h.present.edges };
      const structural = changes.some((c) => c.type === "remove");
      return structural ? commit(h, next) : { ...h, present: next };
    });
  }, []);

  const onEdgesChange = useCallback((changes: EdgeChange[]) => {
    setHistory((h) => {
      const edges = applyEdgeChanges(changes, h.present.edges) as CircuitRfEdge[];
      const next: CanvasSnapshot = { nodes: h.present.nodes, edges };
      const structural = changes.some((c) => c.type === "remove");
      return structural ? commit(h, next) : { ...h, present: next };
    });
  }, []);

  const onConnect = useCallback((c: Connection) => {
    if (!c.source || !c.target) return;
    setHistory((h) =>
      commit(h, { nodes: h.present.nodes, edges: connectNodes(h.present.edges, c.source!, c.target!) }),
    );
  }, []);

  const onNodeDragStart = useCallback(() => {
    setHistory((h) => {
      dragBaseline.current = h.present;
      return h;
    });
  }, []);

  const onNodeDragStop = useCallback(() => {
    setHistory((h) => {
      const baseline = dragBaseline.current;
      dragBaseline.current = null;
      if (!baseline || baseline === h.present) return h;
      // Push the pre-drag baseline into history; keep the dragged present.
      return { past: [...h.past, baseline].slice(-50), present: h.present, future: [] };
    });
  }, []);

  const commitNodes = useCallback((nodes: CircuitRfNode[], edges: CircuitRfEdge[]) => {
    setHistory((h) => commit(h, { nodes, edges }));
  }, []);

  const handleAddNode = useCallback(
    (kind: LineNode["kind"]) => {
      setHistory((h) => {
        const offset = h.present.nodes.length * 24;
        const nodes = addNode(h.present.nodes, kind, { x: 80 + offset, y: 80 + offset });
        return commit(h, { nodes, edges: h.present.edges });
      });
    },
    [],
  );

  const handleDelete = useCallback(() => {
    if (!selectedId) return;
    setHistory((h) => commit(h, removeNode(h.present.nodes, h.present.edges, selectedId)));
    setSelectedId(null);
  }, [selectedId]);

  const handleAutoLayout = useCallback(() => {
    setHistory((h) => commit(h, { nodes: autoLayout(h.present.nodes, h.present.edges), edges: h.present.edges }));
  }, []);

  const handleLoadPreset = useCallback(() => {
    const { nodes, edges } = lineToRf(T01_PRESET);
    const laid = autoLayout(nodes, edges);
    setStatusById({});
    statusRef.current = {};
    setSelectedId(null);
    setLineId(T01_PRESET.id);
    setHistory((h) => commit(h, { nodes: laid, edges }));
  }, []);

  const handleClear = useCallback(() => {
    setStatusById({});
    statusRef.current = {};
    setSelectedId(null);
    setHistory((h) => commit(h, EMPTY_SNAPSHOT));
  }, []);

  const handleInspectorChange = useCallback(
    (patch: Partial<LineNode>) => {
      if (!selectedId) return;
      setHistory((h) =>
        commit(h, {
          nodes: h.present.nodes.map((n) =>
            n.id === selectedId
              ? { ...n, data: { ...n.data, node: { ...n.data.node, ...patch } } as CircuitNodeData }
              : n,
          ),
          edges: h.present.edges,
        }),
      );
    },
    [selectedId],
  );

  // --- undo / redo (buttons + keyboard) ------------------------------------
  const doUndo = useCallback(() => setHistory((h) => undo(h)), []);
  const doRedo = useCallback(() => setHistory((h) => redo(h)), []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement | null)?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA") return;
      if (!(e.metaKey || e.ctrlKey)) return;
      if (e.key.toLowerCase() === "z") {
        e.preventDefault();
        if (e.shiftKey) doRedo();
        else doUndo();
      } else if (e.key.toLowerCase() === "y") {
        e.preventDefault();
        doRedo();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [doUndo, doRedo]);

  // --- execution: dispatch each wave onto the multica task queue ------------
  const waitForWaveTerminal = useCallback((nodeIds: string[]): Promise<boolean> => {
    return new Promise((resolve) => {
      const start = Date.now();
      const check = () => {
        if (cancelRef.current) return resolve(false);
        const done = nodeIds.every((id) => isTerminal(statusRef.current[id] ?? "pending"));
        if (done) return resolve(true);
        if (Date.now() - start > WAVE_TIMEOUT_MS) return resolve(false);
        setTimeout(check, WAVE_POLL_MS);
      };
      check();
    });
  }, []);

  const execute = useCallback(async () => {
    const line = rfToLine(present.nodes, present.edges, lineId, lineTitle);
    const verdict = validateLine(line);
    if (!verdict.ok) {
      verdict.errors.forEach((err) => pushLog(`✗ ${err}`));
      return;
    }
    if (agents.length === 0) {
      pushLog("✗ 工作区没有可用 agent，无法派发");
      return;
    }
    if (typeof window !== "undefined" && !window.confirm("将向 multica 任务队列派发真实任务（会触发 agent 运行）。确认执行?")) {
      return;
    }

    cancelRef.current = false;
    setRunning(true);
    taskToNode.current = new Map();
    statusRef.current = {};
    setStatusById({});
    pushLog(`▶ 开始执行「${line.title}」(${line.nodes.length} 节点)`);

    const waves = compileToWaves(line);
    try {
      for (let w = 0; w < waves.length; w += 1) {
        if (cancelRef.current) {
          pushLog("⏹ 已停止");
          break;
        }
        const wave = waves[w]!;
        pushLog(`层 ${w + 1}/${waves.length}: 派发 ${wave.length} 个节点`);
        const waveNodeIds: string[] = [];
        for (const node of wave) {
          const agent = resolveAgentForExecutor(node.executor, agents);
          if (!agent) {
            pushLog(`  ✗ ${node.id}: 找不到匹配的 agent`);
            continue;
          }
          const req = compileNodeToQueueRequest(node, line.id, line.title, agent.id);
          try {
            const { task_id } = await api.quickCreateIssue(req);
            taskToNode.current.set(task_id, node.id);
            setStatus(node.id, "pending");
            waveNodeIds.push(node.id);
            pushLog(`  ${node.id} → task ${task_id.slice(0, 8)} @ ${agent.name} (${resolveExternalTaskId(node, line.id)})`);
          } catch (err) {
            setStatus(node.id, "failed");
            pushLog(`  ✗ ${node.id} 入队失败: ${(err as Error).message}`);
          }
        }

        const ok = await waitForWaveTerminal(waveNodeIds);
        if (cancelRef.current) {
          pushLog("⏹ 已停止");
          break;
        }
        const failed = waveNodeIds.filter((id) => statusRef.current[id] === "failed");
        if (!ok || failed.length > 0) {
          pushLog(`层 ${w + 1} 未全部通过${failed.length ? `（失败: ${failed.join(", ")}）` : "（超时）"}，停止后续层`);
          break;
        }
        pushLog(`层 ${w + 1} 全部完成 ✓`);
      }
    } finally {
      setRunning(false);
      pushLog("执行结束");
    }
  }, [present, lineId, lineTitle, agents, pushLog, setStatus, waitForWaveTerminal]);

  const stop = useCallback(() => {
    cancelRef.current = true;
    setRunning(false);
  }, []);

  // --- render ---------------------------------------------------------------
  const renderNodes = useMemo(
    () => applyStatuses(present.nodes, statusById),
    [present.nodes, statusById],
  );

  const renderEdges = useMemo(() => {
    const statusOf = (id: string): CircuitStatus | undefined => statusById[id];
    return present.edges.map((e) => {
      const stroke = EDGE_STROKE[statusOf(e.source) ?? "neutral"];
      return stroke ? { ...e, style: { ...e.style, stroke, strokeWidth: 2 } } : e;
    });
  }, [present.edges, statusById]);

  const selectedNode = useMemo<LineNode | null>(() => {
    const found = present.nodes.find((n) => n.id === selectedId);
    return found ? found.data.node : null;
  }, [present.nodes, selectedId]);

  return (
    <div className="flex h-full w-full flex-col">
      {/* toolbar */}
      <div className="flex flex-wrap items-center gap-1.5 border-b border-border px-3 py-2">
        <Button size="sm" variant="outline" onClick={() => handleAddNode("dev")}>
          <Plus /> dev 节点
        </Button>
        <Button size="sm" variant="outline" onClick={() => handleAddNode("audit")}>
          <ShieldPlus /> audit 节点
        </Button>
        <Separator orientation="vertical" className="mx-1 h-5" />
        <Button size="sm" variant="ghost" onClick={handleAutoLayout}>
          <Wand2 /> 自动布局
        </Button>
        <Button size="sm" variant="ghost" onClick={doUndo} disabled={!canUndo(history)}>
          <Undo2 /> 撤销
        </Button>
        <Button size="sm" variant="ghost" onClick={doRedo} disabled={!canRedo(history)}>
          <Redo2 /> 重做
        </Button>
        <Button size="sm" variant="ghost" onClick={handleDelete} disabled={!selectedId}>
          <Trash2 /> 删除
        </Button>
        <Separator orientation="vertical" className="mx-1 h-5" />
        <Button size="sm" variant="ghost" onClick={handleLoadPreset}>
          <ListRestart /> T01 预设
        </Button>
        <Button size="sm" variant="ghost" onClick={handleClear}>
          清空
        </Button>
        <div className="ml-auto flex items-center gap-1.5">
          {running ? (
            <Button size="sm" variant="destructive" onClick={stop}>
              <Square /> 停止
            </Button>
          ) : (
            <Button size="sm" onClick={execute} disabled={present.nodes.length === 0}>
              <Play /> 执行
            </Button>
          )}
        </div>
      </div>

      <div className="flex min-h-0 flex-1">
        {/* canvas */}
        <div className="relative min-w-0 flex-1">
          <ReactFlow
            nodes={renderNodes}
            edges={renderEdges}
            nodeTypes={nodeTypes}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            onConnect={onConnect}
            onNodeDragStart={onNodeDragStart}
            onNodeDragStop={onNodeDragStop}
            onNodeClick={(_, node) => setSelectedId(node.id)}
            onPaneClick={() => setSelectedId(null)}
            deleteKeyCode={["Backspace", "Delete"]}
            fitView
            proOptions={{ hideAttribution: true }}
          >
            <Background />
            <Controls />
            <MiniMap pannable zoomable className="!bg-card" />
          </ReactFlow>
        </div>

        {/* right rail: inspector + log */}
        <div className="flex w-80 shrink-0 flex-col border-l border-border">
          <div className="border-b border-border">
            <NodeInspector node={selectedNode} onChange={handleInspectorChange} />
          </div>
          <div className="flex min-h-0 flex-1 flex-col">
            <div className="px-3 py-2 text-xs font-medium text-muted-foreground">执行日志</div>
            <ScrollArea className="min-h-0 flex-1 px-3 pb-3">
              <div className="flex flex-col gap-0.5 font-mono text-[11px] leading-relaxed">
                {log.length === 0 ? (
                  <span className="text-muted-foreground">尚未执行。</span>
                ) : (
                  log.map((line, i) => (
                    <span
                      key={i}
                      className={cn(
                        line.startsWith("  ✗") || line.startsWith("✗") ? "text-destructive" : "text-foreground/80",
                      )}
                    >
                      {line}
                    </span>
                  ))
                )}
              </div>
            </ScrollArea>
          </div>
        </div>
      </div>
    </div>
  );
}

export function CanvasOrchestrator() {
  return (
    <ReactFlowProvider>
      <CanvasOrchestratorInner />
    </ReactFlowProvider>
  );
}
