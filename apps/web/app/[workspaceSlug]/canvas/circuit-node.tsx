"use client";

import { Handle, Position, type Node, type NodeProps } from "@xyflow/react";
import { CIRCUIT_COLORS, statusMeta } from "./circuit-theme";
import type { ProjectStatus } from "@multica/core/types";

// 一个电路板节点对应一条生产线(Multica 项目)。
export type CircuitNodeData = {
  code: string;
  title: string;
  issueCount: number;
  doneCount: number;
  status: ProjectStatus;
};

export type CircuitFlowNode = Node<CircuitNodeData, "circuit">;

const handleClass =
  "!h-3 !w-3 !rounded-full !border-2 !border-[#05080d]";

export function CircuitNode({ data }: NodeProps<CircuitFlowNode>) {
  const meta = statusMeta(data.status);
  const accent = meta.accent;
  const pct =
    data.issueCount > 0
      ? Math.round((data.doneCount / data.issueCount) * 100)
      : 0;

  return (
    <div
      className="relative w-[260px] cursor-pointer border p-3 transition-shadow hover:brightness-110"
      style={{
        backgroundColor: CIRCUIT_COLORS.panel,
        borderColor: accent,
        boxShadow: `0 0 24px rgba(0,0,0,0.35), 0 0 18px ${accent}22`,
      }}
      title="点击进入该生产线"
    >
      <Handle
        type="target"
        position={Position.Left}
        className={handleClass}
        style={{ backgroundColor: accent }}
      />

      <div className="mb-3 flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div
            className="font-mono text-[10px] uppercase tracking-[0.16em]"
            style={{ color: accent }}
          >
            {data.code}
          </div>
          <div
            className="mt-1 line-clamp-2 text-sm font-semibold leading-5"
            style={{ color: CIRCUIT_COLORS.slateWhite }}
          >
            {data.title}
          </div>
        </div>
        <div
          className="min-w-9 border px-2 py-1 text-center font-mono text-xs"
          style={{ borderColor: CIRCUIT_COLORS.line, color: "#e2e8f0" }}
          title="任务数"
        >
          {data.issueCount}
        </div>
      </div>

      {/* 任务完成进度条(电路走线) */}
      <div
        className="mb-2 h-1 w-full overflow-hidden"
        style={{ backgroundColor: CIRCUIT_COLORS.line }}
      >
        <div
          className="h-full transition-all"
          style={{ width: `${pct}%`, backgroundColor: accent }}
        />
      </div>

      <div className="flex items-center justify-between gap-2">
        <span
          className="inline-flex items-center gap-1.5 border px-2 py-1 font-mono text-[10px] uppercase tracking-[0.12em]"
          style={{ borderColor: accent, color: accent }}
        >
          <span
            className="h-1.5 w-1.5 rounded-full"
            style={{ backgroundColor: accent }}
          />
          {meta.label}
        </span>
        <span
          className="font-mono text-[10px]"
          style={{ color: CIRCUIT_COLORS.slate }}
        >
          完成 {data.doneCount}/{data.issueCount} · {pct}%
        </span>
      </div>

      <Handle
        type="source"
        position={Position.Right}
        className={handleClass}
        style={{ backgroundColor: accent }}
      />
    </div>
  );
}
