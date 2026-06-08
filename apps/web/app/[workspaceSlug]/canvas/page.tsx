"use client";

import { useCallback, useMemo } from "react";
import { useRouter } from "next/navigation";
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
import { useWorkspacePaths } from "@multica/core/paths";
import { CircuitNode, type CircuitFlowNode } from "./circuit-node";
import {
  CIRCUIT_COLORS,
  STATUS_FLOW_ORDER,
  statusFlowRank,
  statusMeta,
  edgeAccent,
} from "./circuit-theme";

const nodeTypes = { circuit: CircuitNode };

// 按状态分泳道(列)排布:同状态的生产线落在同一列,沿生命周期方向从左到右。
const LANE_GAP_X = 360; // 列间距,需大于节点宽度(260)留出走线空间
const ROW_GAP_Y = 196; // 同一泳道内节点的纵向间距
const ORIGIN_X = 80;
const ORIGIN_Y = 140;
// 最多画几条生产线,超出的截断(画布是概览,不堆满)。
const MAX_LINES = 8;

export default function CanvasPage() {
  const wsId = useWorkspaceId();
  const router = useRouter();
  const wsPaths = useWorkspacePaths();
  const { data: projects, isLoading } = useQuery(projectListOptions(wsId));

  // 点击电路板节点 → 进入对应生产线(项目)详情页。
  const onNodeClick = useCallback(
    (_: unknown, node: CircuitFlowNode) => {
      router.push(wsPaths.projectDetail(node.id));
    },
    [router, wsPaths],
  );

  // 取若干条生产线(项目),按生命周期状态排序 —— 同状态相邻,便于分泳道。
  const lines = useMemo(() => {
    const list = (projects ?? []).slice(0, MAX_LINES);
    return [...list].sort(
      (a, b) => statusFlowRank(a.status) - statusFlowRank(b.status),
    );
  }, [projects]);

  // 按真实状态把节点分到泳道:同状态一列,列序沿生命周期(规划→进行→…→取消)。
  // 节点位置 = 列(状态)× 行(同状态内的序号),既「按状态分组」又留出走线空间。
  const nodes = useMemo<CircuitFlowNode[]>(() => {
    // 只为「确实有节点」的状态分配列,避免空列拉开间距。
    const lanesWithNodes = STATUS_FLOW_ORDER.filter((s) =>
      lines.some((p) => p.status === s),
    );
    const laneCol = new Map(lanesWithNodes.map((s, col) => [s, col]));
    const rowInLane = new Map<string, number>();

    return lines.map((p, i) => {
      const col = laneCol.get(p.status) ?? lanesWithNodes.length;
      const row = rowInLane.get(p.status) ?? 0;
      rowInLane.set(p.status, row + 1);
      return {
        id: p.id,
        type: "circuit",
        position: {
          x: ORIGIN_X + col * LANE_GAP_X,
          y: ORIGIN_Y + row * ROW_GAP_Y,
        },
        data: {
          code: `PL${i + 1}`,
          title: p.title,
          issueCount: p.issue_count,
          doneCount: p.done_count,
          status: p.status,
        },
      };
    });
  }, [lines]);

  // 走线体现真实状态流转:节点已按生命周期排序,沿此序串成一条信号链。
  // 同状态相邻 → 同泳道内的纵向连线(同阶段并行);跨状态相邻 → 跨泳道的流转连线
  // (用 animated 高亮信号方向)。每条线的颜色取源节点状态的强调色,和节点着色一致。
  const edges = useMemo<Edge[]>(() => {
    const out: Edge[] = [];
    for (let i = 0; i < lines.length - 1; i++) {
      const from = lines[i];
      const to = lines[i + 1];
      if (!from || !to) continue;
      const crossStage = from.status !== to.status;
      out.push({
        id: `${from.id}->${to.id}`,
        source: from.id,
        target: to.id,
        type: "smoothstep",
        animated: crossStage,
        style: {
          stroke: edgeAccent(from.status),
          strokeWidth: crossStage ? 2 : 1.5,
          opacity: crossStage ? 0.9 : 0.55,
        },
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
            : `${lines.length} 条生产线 · 按状态分泳道 · 走线沿生命周期流转`}
        </div>
      </div>

      {/* 状态图例:节点 / 走线颜色与状态的对应关系(只读叠层,不参与平移缩放)。 */}
      {!isLoading && lines.length > 0 ? (
        <div
          className="pointer-events-none absolute right-6 top-5 z-10 border px-3 py-2"
          style={{
            backgroundColor: CIRCUIT_COLORS.panel,
            borderColor: CIRCUIT_COLORS.line,
          }}
        >
          <div
            className="mb-1.5 font-mono text-[10px] uppercase tracking-[0.16em]"
            style={{ color: CIRCUIT_COLORS.slate }}
          >
            STATUS
          </div>
          <div className="flex flex-col gap-1">
            {STATUS_FLOW_ORDER.map((s) => {
              const meta = statusMeta(s);
              return (
                <div key={s} className="flex items-center gap-2">
                  <span
                    className="h-2 w-2 rounded-full"
                    style={{ backgroundColor: meta.accent }}
                  />
                  <span
                    className="text-[11px]"
                    style={{ color: CIRCUIT_COLORS.slateWhite }}
                  >
                    {meta.label}
                  </span>
                </div>
              );
            })}
          </div>
        </div>
      ) : null}

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
          onNodeClick={onNodeClick}
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
