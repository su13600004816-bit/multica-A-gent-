"use client";

import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  ReactFlow,
  Background,
  BackgroundVariant,
  Controls,
  type Edge,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { projectListOptions } from "@multica/core/projects/queries";
import { useWorkspaceId } from "@multica/core/hooks";
import { CircuitNode, type CircuitFlowNode } from "./circuit-node";
import { CIRCUIT_COLORS } from "./circuit-theme";

const nodeTypes = { circuit: CircuitNode };

// 在画布上一行排开,节点之间留出连线间距。
const NODE_GAP_X = 320;
const ORIGIN_X = 80;
const ORIGIN_Y = 220;

export default function CanvasPage() {
  const wsId = useWorkspaceId();
  const { data: projects, isLoading } = useQuery(projectListOptions(wsId));

  // 取前 5 条生产线(项目),每条画成一个电路板节点。
  const lines = useMemo(() => (projects ?? []).slice(0, 5), [projects]);

  const nodes = useMemo<CircuitFlowNode[]>(
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
  const edges = useMemo<Edge[]>(() => {
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

  return (
    <div
      className="relative h-svh w-full"
      style={{ backgroundColor: CIRCUIT_COLORS.deep }}
    >
      <div className="pointer-events-none absolute left-6 top-5 z-10">
        <div
          className="font-mono text-[11px] uppercase tracking-[0.24em]"
          style={{ color: CIRCUIT_COLORS.cyan }}
        >
          PL1 · CIRCUIT BOARD
        </div>
        <div
          className="mt-1 text-lg font-semibold"
          style={{ color: CIRCUIT_COLORS.slateWhite }}
        >
          生产线电路板画布
        </div>
        <div className="mt-0.5 text-xs" style={{ color: CIRCUIT_COLORS.slate }}>
          {isLoading
            ? "加载生产线中…"
            : `${lines.length} 条生产线 · 节点显示名称 / 任务数 / 状态`}
        </div>
      </div>

      {!isLoading && lines.length === 0 ? (
        <div
          className="flex h-full items-center justify-center text-sm"
          style={{ color: CIRCUIT_COLORS.slate }}
        >
          暂无生产线(项目)。去「项目」里创建后即可在此看到电路板节点。
        </div>
      ) : (
        <ReactFlow
          colorMode="dark"
          nodes={nodes}
          edges={edges}
          nodeTypes={nodeTypes}
          fitView
          fitViewOptions={{ padding: 0.3 }}
          proOptions={{ hideAttribution: true }}
          nodesDraggable
          nodesConnectable={false}
          style={{ backgroundColor: CIRCUIT_COLORS.deep }}
        >
          <Background
            variant={BackgroundVariant.Dots}
            gap={24}
            size={1}
            color={CIRCUIT_COLORS.line}
          />
          <Controls showInteractive={false} />
        </ReactFlow>
      )}
    </div>
  );
}
