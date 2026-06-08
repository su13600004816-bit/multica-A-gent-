"use client";

import { useCallback, useEffect, useMemo, useRef } from "react";
import { useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import {
  ReactFlow,
  Background,
  BackgroundVariant,
  Controls,
  MiniMap,
  SelectionMode,
  useReactFlow,
  useNodesState,
  useEdgesState,
  type Edge,
  type Viewport,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { projectListOptions } from "@multica/core/projects/queries";
import { useWorkspaceId } from "@multica/core/hooks";
import { useWorkspacePaths } from "@multica/core/paths";
import { CircuitNode, type CircuitFlowNode } from "./circuit-node";
import { CIRCUIT_COLORS, statusMeta } from "./circuit-theme";

const nodeTypes = { circuit: CircuitNode };

// 在画布上一行排开,节点之间留出连线间距。
const NODE_GAP_X = 320;
const ORIGIN_X = 80;
const ORIGIN_Y = 220;

// 视口/手势参数提取为模块级常量,保持引用稳定 ——
// ReactFlow 对每次渲染传入的新对象很敏感(易触发重渲染/死循环告警)。
const FIT_VIEW_OPTIONS = { padding: 0.3, duration: 240 } as const;
const PRO_OPTIONS = { hideAttribution: true } as const;
const MIN_ZOOM = 0.4;
const MAX_ZOOM = 2;
// 触屏轻点容差:小幅移动仍算「点击进入」,而非误判为拖拽。
const TOUCH_DRAG_THRESHOLD = 6;
// 方向键平移步长(屏幕像素)。
const PAN_STEP = 60;
// Shift+拖拽进入框选;Ctrl/⌘ 用于多选叠加。
const SELECTION_KEY_CODE = "Shift";
const MULTI_SELECTION_KEY_CODE = ["Meta", "Control"];

// ── 视口持久化(localStorage)──────────────────────────────
// 缩放比例与平移位置按 workspace 维度记忆,刷新/重进画布后恢复;
// 无记录时回落到 fitView 默认视图。
const VIEWPORT_STORAGE_PREFIX = "pl1.canvas.viewport.";

function viewportStorageKey(wsId: string): string {
  return `${VIEWPORT_STORAGE_PREFIX}${wsId}`;
}

function readStoredViewport(key: string): Viewport | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(key);
    if (!raw) return null;
    const v = JSON.parse(raw) as Partial<Viewport>;
    if (
      v &&
      Number.isFinite(v.x) &&
      Number.isFinite(v.y) &&
      Number.isFinite(v.zoom)
    ) {
      return { x: v.x as number, y: v.y as number, zoom: v.zoom as number };
    }
  } catch {
    // 忽略损坏/无法解析的存储值。
  }
  return null;
}

function writeStoredViewport(key: string, v: Viewport): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(key, JSON.stringify(v));
  } catch {
    // 配额超限 / 隐私模式下静默失败,不影响画布。
  }
}

// 视口控制器:作为 ReactFlow 的子组件,拿到视口操作句柄,
// 负责「初次恢复持久化视口 or fitView」+「键盘快捷键」。
function ViewportController({
  storageKey,
  ready,
}: {
  storageKey: string;
  ready: boolean;
}) {
  const { fitView, zoomIn, zoomOut, getViewport, setViewport } = useReactFlow();
  const restored = useRef(false);

  // 节点首次就绪时恢复视口:有记录用记录,否则 fitView 适配全部。
  useEffect(() => {
    if (restored.current || !ready) return;
    restored.current = true;
    const saved = readStoredViewport(storageKey);
    if (saved) {
      void setViewport(saved);
    } else {
      void fitView(FIT_VIEW_OPTIONS);
    }
  }, [ready, storageKey, fitView, setViewport]);

  // 快捷键:F=适配全部,+/-=缩放,方向键=平移。
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      // 焦点在输入控件里时放行,避免影响打字/搜索。
      if (
        target &&
        (target.tagName === "INPUT" ||
          target.tagName === "TEXTAREA" ||
          target.isContentEditable)
      ) {
        return;
      }
      // 让出带修饰键的组合键(浏览器/系统快捷键)。
      if (e.metaKey || e.ctrlKey || e.altKey) return;

      switch (e.key) {
        case "f":
        case "F":
          e.preventDefault();
          void fitView(FIT_VIEW_OPTIONS);
          break;
        case "+":
        case "=":
          e.preventDefault();
          void zoomIn({ duration: 160 });
          break;
        case "-":
        case "_":
          e.preventDefault();
          void zoomOut({ duration: 160 });
          break;
        case "ArrowUp":
        case "ArrowDown":
        case "ArrowLeft":
        case "ArrowRight": {
          e.preventDefault();
          const vp = getViewport();
          const dx =
            e.key === "ArrowLeft"
              ? PAN_STEP
              : e.key === "ArrowRight"
                ? -PAN_STEP
                : 0;
          const dy =
            e.key === "ArrowUp"
              ? PAN_STEP
              : e.key === "ArrowDown"
                ? -PAN_STEP
                : 0;
          void setViewport(
            { x: vp.x + dx, y: vp.y + dy, zoom: vp.zoom },
            { duration: 120 },
          );
          break;
        }
        default:
          break;
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [fitView, zoomIn, zoomOut, getViewport, setViewport]);

  return null;
}

// 小地图节点取生产线状态强调色,和电路板节点描边保持一致。
function miniMapNodeColor(node: CircuitFlowNode): string {
  return statusMeta(node.data.status).accent;
}

export default function CanvasPage() {
  const wsId = useWorkspaceId();
  const router = useRouter();
  const wsPaths = useWorkspacePaths();
  const { data: projects, isLoading } = useQuery(projectListOptions(wsId));

  // 点击/轻点电路板节点 → 进入对应生产线(项目)详情页。
  const onNodeClick = useCallback(
    (_: unknown, node: CircuitFlowNode) => {
      router.push(wsPaths.projectDetail(node.id));
    },
    [router, wsPaths],
  );

  // 取前 5 条生产线(项目),每条画成一个电路板节点。
  const lines = useMemo(() => (projects ?? []).slice(0, 5), [projects]);

  // 由数据派生出的「期望」节点布局 —— 仅依赖生产线数据。
  const computedNodes = useMemo<CircuitFlowNode[]>(
    () =>
      lines.map((p, i) => ({
        id: p.id,
        type: "circuit",
        position: { x: ORIGIN_X + i * NODE_GAP_X, y: ORIGIN_Y },
        data: {
          code: `PL${i + 1}`,
          title: p.title,
          issueCount: p.issue_count,
          doneCount: p.done_count,
          status: p.status,
        },
      })),
    [lines],
  );

  // 把相邻生产线用电路板样式的连线串起来。
  const computedEdges = useMemo<Edge[]>(() => {
    const out: Edge[] = [];
    for (let i = 0; i < lines.length - 1; i++) {
      const from = lines[i];
      const to = lines[i + 1];
      if (!from || !to) continue;
      out.push({
        id: `${from.id}->${to.id}`,
        source: from.id,
        target: to.id,
        animated: true,
        style: { stroke: CIRCUIT_COLORS.cyan, strokeWidth: 1.5 },
      });
    }
    return out;
  }, [lines]);

  // 用 ReactFlow 的受控 state 承接节点/连线,这样:
  // 1) 节点真正可拖动(桌面鼠标 + 移动端触控,onNodesChange 会落盘位置);
  // 2) 数据刷新时把最新布局同步进来(下方 effect),不会和拖拽互相覆盖成死循环。
  const [nodes, setNodes, onNodesChange] = useNodesState<CircuitFlowNode>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([]);

  useEffect(() => {
    setNodes(computedNodes);
  }, [computedNodes, setNodes]);

  useEffect(() => {
    setEdges(computedEdges);
  }, [computedEdges, setEdges]);

  // 视口持久化:平移/缩放结束后把当前视口写入 localStorage。
  const storageKey = useMemo(() => viewportStorageKey(wsId), [wsId]);
  const onMoveEnd = useCallback(
    (_: unknown, viewport: Viewport) => {
      writeStoredViewport(storageKey, viewport);
    },
    [storageKey],
  );

  // 节点就绪后再交给 ViewportController 决定恢复/适配。
  const viewportReady = nodes.length > 0;

  return (
    <div
      className="relative h-svh w-full overflow-hidden"
      style={{ backgroundColor: CIRCUIT_COLORS.deep }}
    >
      {/* 顶部信息条:折叠屏/窄屏下限宽 + 自动换行,logo 不被截、不溢出。 */}
      <div className="pointer-events-none absolute left-4 right-4 top-4 z-10 max-w-[min(92vw,520px)] sm:left-6 sm:top-5">
        <div
          className="break-words font-mono text-[10px] uppercase tracking-[0.2em] sm:text-[11px] sm:tracking-[0.24em]"
          style={{ color: CIRCUIT_COLORS.cyan }}
        >
          PL1 · CIRCUIT BOARD
        </div>
        <div
          className="mt-1 break-words text-base font-semibold sm:text-lg"
          style={{ color: CIRCUIT_COLORS.slateWhite }}
        >
          生产线电路板画布
        </div>
        <div
          className="mt-0.5 break-words text-[11px] sm:text-xs"
          style={{ color: CIRCUIT_COLORS.slate }}
        >
          {isLoading
            ? "加载生产线中…"
            : `${lines.length} 条生产线 · 节点显示名称 / 任务数 / 状态`}
        </div>
        {!isLoading && lines.length > 0 ? (
          <div
            className="mt-1 break-words font-mono text-[10px] tracking-wide sm:text-[11px]"
            style={{ color: CIRCUIT_COLORS.slate }}
          >
            F 适配全部 · +/- 缩放 · 方向键平移 · Shift 拖拽框选
          </div>
        ) : null}
      </div>

      {!isLoading && lines.length === 0 ? (
        <div
          className="flex h-full items-center justify-center px-6 text-center text-sm"
          style={{ color: CIRCUIT_COLORS.slate }}
        >
          暂无生产线(项目)。去「项目」里创建后即可在此看到电路板节点。
        </div>
      ) : (
        <ReactFlow
          colorMode="dark"
          nodes={nodes}
          edges={edges}
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          onMoveEnd={onMoveEnd}
          nodeTypes={nodeTypes}
          onNodeClick={onNodeClick}
          fitViewOptions={FIT_VIEW_OPTIONS}
          proOptions={PRO_OPTIONS}
          minZoom={MIN_ZOOM}
          maxZoom={MAX_ZOOM}
          nodesDraggable
          nodesConnectable={false}
          nodeDragThreshold={TOUCH_DRAG_THRESHOLD}
          panOnDrag
          panOnScroll
          zoomOnPinch
          zoomOnDoubleClick={false}
          selectionOnDrag={false}
          selectionMode={SelectionMode.Partial}
          selectionKeyCode={SELECTION_KEY_CODE}
          multiSelectionKeyCode={MULTI_SELECTION_KEY_CODE}
          style={{ backgroundColor: CIRCUIT_COLORS.deep }}
        >
          <ViewportController storageKey={storageKey} ready={viewportReady} />
          <Background
            variant={BackgroundVariant.Dots}
            gap={24}
            size={1}
            color={CIRCUIT_COLORS.line}
          />
          <Controls
            showInteractive={false}
            position="bottom-left"
            fitViewOptions={FIT_VIEW_OPTIONS}
          />
          <MiniMap<CircuitFlowNode>
            position="bottom-right"
            pannable
            zoomable
            bgColor={CIRCUIT_COLORS.panel}
            maskColor="rgba(5,8,13,0.6)"
            maskStrokeColor={CIRCUIT_COLORS.line}
            nodeColor={miniMapNodeColor}
            nodeStrokeColor={CIRCUIT_COLORS.cyan}
            nodeStrokeWidth={2}
            nodeBorderRadius={2}
            style={{
              border: `1px solid ${CIRCUIT_COLORS.line}`,
              borderRadius: 4,
            }}
          />
        </ReactFlow>
      )}
    </div>
  );
}
