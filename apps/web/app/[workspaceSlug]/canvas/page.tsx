"use client";

import { useCallback, useEffect, useMemo } from "react";
import { useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import {
  ReactFlow,
  Background,
  BackgroundVariant,
  Controls,
  useNodesState,
  useEdgesState,
  type Edge,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { projectListOptions } from "@multica/core/projects/queries";
import { useWorkspaceId } from "@multica/core/hooks";
import { useWorkspacePaths } from "@multica/core/paths";
import { CircuitNode, type CircuitFlowNode } from "./circuit-node";
import { CIRCUIT_COLORS } from "./circuit-theme";

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
          nodeTypes={nodeTypes}
          onNodeClick={onNodeClick}
          fitView
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
          style={{ backgroundColor: CIRCUIT_COLORS.deep }}
        >
          <Background
            variant={BackgroundVariant.Dots}
            gap={24}
            size={1}
            color={CIRCUIT_COLORS.line}
          />
          <Controls
            showInteractive={false}
            position="bottom-right"
            fitViewOptions={FIT_VIEW_OPTIONS}
          />
        </ReactFlow>
      )}
    </div>
  );
}
