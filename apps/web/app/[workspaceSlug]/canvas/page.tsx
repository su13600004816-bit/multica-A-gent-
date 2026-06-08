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
} from "./circuit-theme";

const nodeTypes = { circuit: CircuitNode };

// 画布只渲染来自真实数据的关系连线。Project 数据模型(packages/core/types/project.ts)
// 没有任何项目间关系字段(无 parent / depends_on / related),后端也没有对应的关系接口,
// 因此当前没有可画的真实连线 —— 绝不按「状态排序后相邻」之类的排布次序合成假线。
// 等数据源出现真实项目关系时,在此用该字段生成 edges。
const NO_EDGES: Edge[] = [];

// 按状态分泳道(列)排布:同状态的生产线落在同一列,沿生命周期方向从左到右。
const LANE_GAP_X = 360; // 列间距,大于节点宽度(260)让相邻泳道留出间隔
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

  // 取若干条生产线(项目),按生命周期状态排序 —— 仅为把同状态节点排到一起、
  // 便于分泳道与稳定的 PL 编号,排序次序本身不代表任何项目间关系。
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
            : `${lines.length} 条生产线 · 按状态分泳道着色`}
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
          edges={NO_EDGES}
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
