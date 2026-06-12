"use client";

import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  ReactFlow,
  ReactFlowProvider,
  Background,
  BackgroundVariant,
  Controls,
  Panel,
  addEdge,
  useNodesState,
  useEdgesState,
  useReactFlow,
  type Edge,
  type Connection,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useTheme } from "@multica/ui/components/common/theme-provider";
import { Button } from "@multica/ui/components/ui/button";
import { Textarea } from "@multica/ui/components/ui/textarea";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@multica/ui/components/ui/dialog";
import { Sparkles, Maximize2, FolderOpen, Loader2 } from "lucide-react";
import { LogicGraphClient, graphToFlow, type FlowEdge, type LogicGraph } from "@multica/core/logicgraph";
import { LogicNode, type LogicFlowNode } from "./logic-node";

const nodeTypes = { logicNode: LogicNode };

// Edge kinds that read as dashed/secondary lines, mirroring the logic model.
const DASHED = new Set(["monitors", "blocks", "interconnects"]);

function toRfEdge(e: FlowEdge): Edge {
  const dashed = DASHED.has(e.data.kind);
  return {
    id: e.id,
    source: e.source,
    target: e.target,
    label: e.label,
    animated: e.animated,
    style: {
      stroke: "var(--muted-foreground)",
      strokeWidth: 1.5,
      strokeDasharray: dashed ? "4 3" : undefined,
    },
    labelStyle: { fill: "var(--muted-foreground)", fontSize: 10 },
    labelBgStyle: { fill: "var(--background)" },
  };
}

function LogicGraphFlow({ baseUrl }: { baseUrl?: string }) {
  const { resolvedTheme } = useTheme();
  const colorMode = resolvedTheme === "dark" ? "dark" : "light";
  const { fitView } = useReactFlow();
  const client = useMemo(() => new LogicGraphClient({ baseUrl }), [baseUrl]);

  const [nodes, setNodes, onNodesChange] = useNodesState<LogicFlowNode>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([]);
  const [graphs, setGraphs] = useState<string[]>([]);
  const [activeName, setActiveName] = useState<string | null>(null);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [desc, setDesc] = useState("");
  const [busy, setBusy] = useState<string | null>(null); // status text while generating
  const [error, setError] = useState<string | null>(null); // one-shot failure notice (separate from busy)

  // Guard async setState after unmount: loadGraph/generate await network calls
  // (generate can poll up to ~120s) and must not touch state once gone.
  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const applyGraph = useCallback(
    (graph: LogicGraph) => {
      const flow = graphToFlow(graph);
      setNodes(flow.nodes as LogicFlowNode[]);
      setEdges(flow.edges.map(toRfEdge));
      window.requestAnimationFrame(() => fitView({ padding: 0.2 }));
    },
    [setNodes, setEdges, fitView],
  );

  const loadGraph = useCallback(
    async (name: string) => {
      setBusy("加载中…");
      const g = await client.getGraph(name);
      if (!mountedRef.current) return;
      setBusy(null);
      if (g) {
        setActiveName(name);
        applyGraph(g);
      }
    },
    [client, applyGraph],
  );

  // initial: list graphs, open the first (e.g. the live line architecture).
  useEffect(() => {
    let alive = true;
    void client.listGraphs().then((names) => {
      if (!alive) return;
      setGraphs(names);
      const first = names.includes("lines") ? "lines" : names[0];
      if (first) void loadGraph(first);
    });
    return () => {
      alive = false;
    };
  }, [client, loadGraph]);

  const onConnect = useCallback(
    (c: Connection) => setEdges((eds) => addEdge({ ...c, style: { stroke: "var(--muted-foreground)" } }, eds)),
    [setEdges],
  );

  const generate = useCallback(async () => {
    const text = desc.trim();
    if (!text) return;
    setError(null);
    setBusy("模型生成中…(约 30–60 秒)");
    const queued = await client.buildFromText(text);
    if (!mountedRef.current) return;
    if (!queued) {
      // Clear busy (not leave it set) so the buttons re-enable and the user can retry.
      setBusy(null);
      setError("生成失败,请重试");
      return;
    }
    const g = await client.waitForGraph(queued.name);
    if (!mountedRef.current) return;
    setBusy(null);
    if (g && g.nodes.length > 0) {
      setDialogOpen(false);
      setDesc("");
      setActiveName(queued.name);
      applyGraph(g);
      void client.listGraphs().then((names) => {
        if (mountedRef.current) setGraphs(names);
      });
    } else {
      setError("生成超时,稍后在列表里查看");
    }
  }, [desc, client, applyGraph]);

  return (
    <div className="relative h-full w-full overflow-hidden rounded-lg border bg-background">
      <ReactFlow
        colorMode={colorMode}
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onConnect={onConnect}
        fitView
        fitViewOptions={{ padding: 0.2 }}
        proOptions={{ hideAttribution: true }}
        nodesDraggable
        nodesConnectable
        elementsSelectable
        minZoom={0.3}
        maxZoom={1.6}
      >
        <Background variant={BackgroundVariant.Dots} gap={22} size={1} color="var(--border)" />
        <Controls showInteractive={false} className="!rounded-lg !border !border-border !bg-background !shadow-sm" />

        <Panel position="top-left">
          <div className="flex items-center gap-2 rounded-lg border bg-background px-3 py-1.5 shadow-sm">
            <span className="text-xs font-medium">{activeName ?? "逻辑图"}</span>
            {busy && (
              <span className="flex items-center gap-1 text-[11px] text-muted-foreground">
                <Loader2 className="size-3 animate-spin" />
                {busy}
              </span>
            )}
          </div>
        </Panel>

        {/* Logic-graph toolbar — same chrome as the canvas-orchestration toolbar, using
            Multica Button tokens; no separate skin. */}
        <Panel position="bottom-center">
          <div className="flex items-center gap-1 rounded-lg border bg-background px-1.5 py-1 shadow-sm">
            <Button size="xs" variant="ghost" onClick={() => setDialogOpen(true)}>
              <Sparkles className="size-3.5 mr-1" />
              生成逻辑图
            </Button>
            {graphs.length > 0 && (
              <>
                <span className="mx-0.5 h-4 w-px bg-border" />
                <select
                  aria-label="切换逻辑图"
                  value={activeName ?? ""}
                  onChange={(e) => void loadGraph(e.target.value)}
                  className="h-6 max-w-[140px] truncate rounded-md border border-input bg-background px-1.5 text-xs text-foreground"
                >
                  {graphs.map((g) => (
                    <option key={g} value={g}>
                      {g}
                    </option>
                  ))}
                </select>
              </>
            )}
            <span className="mx-0.5 h-4 w-px bg-border" />
            <Button size="xs" variant="ghost" onClick={() => fitView({ padding: 0.2 })}>
              <Maximize2 className="size-3.5 mr-1" />
              适配
            </Button>
          </div>
        </Panel>
      </ReactFlow>

      <Dialog
        open={dialogOpen}
        onOpenChange={(open) => {
          setDialogOpen(open);
          if (open) setError(null);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle className="flex items-center gap-1.5">
              <Sparkles className="size-4" />
              用一段话生成逻辑图
            </DialogTitle>
            <DialogDescription>
              描述你的架构/逻辑关系,模型会建成一张图、自查漏,画进画布。
            </DialogDescription>
          </DialogHeader>
          <Textarea
            value={desc}
            onChange={(e) => setDesc(e.target.value)}
            placeholder="例:有个总指挥派活到三条线,每条线有写码员和审计员,审计不过退回返工;看门狗只盯不派活,卡住升级给总指挥。"
            className="min-h-[120px]"
          />
          {error && <p className="text-[11px] text-destructive">{error}</p>}
          <DialogFooterRow>
            <Button variant="ghost" size="sm" onClick={() => setDialogOpen(false)} disabled={!!busy}>
              取消
            </Button>
            <Button size="sm" onClick={() => void generate()} disabled={!desc.trim() || !!busy}>
              {busy ? <Loader2 className="size-3.5 mr-1 animate-spin" /> : <FolderOpen className="size-3.5 mr-1" />}
              {busy ?? "生成"}
            </Button>
          </DialogFooterRow>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function DialogFooterRow({ children }: { children: ReactNode }) {
  return <div className="mt-3 flex items-center justify-end gap-2">{children}</div>;
}

export function LogicGraphCanvas({ baseUrl }: { baseUrl?: string }) {
  return (
    <ReactFlowProvider>
      <LogicGraphFlow baseUrl={baseUrl} />
    </ReactFlowProvider>
  );
}
