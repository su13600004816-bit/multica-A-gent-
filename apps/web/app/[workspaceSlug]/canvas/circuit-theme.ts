import type { ProjectStatus } from "@multica/core/types";

// PL1 暗色科技风调色板(取自 /home/fleet/canvas 的电路板画布):
// 深色底 + 青色描边,不同状态用不同强调色。
export const CIRCUIT_COLORS = {
  deep: "#05080d",
  panel: "rgba(9,17,29,0.95)",
  line: "rgba(148, 163, 184, 0.22)",
  slate: "#94a3b8",
  slateWhite: "#e6edf3",
  cyan: "#22d3ee",
  green: "#22c55e",
  amber: "#f59e0b",
} as const;

export interface StatusMeta {
  label: string;
  accent: string;
}

// 把 Multica 项目(生产线)状态映射到电路板节点的中文标签与强调色。
export const STATUS_META: Record<ProjectStatus, StatusMeta> = {
  planned: { label: "规划中", accent: CIRCUIT_COLORS.amber },
  in_progress: { label: "进行中", accent: CIRCUIT_COLORS.cyan },
  paused: { label: "已暂停", accent: CIRCUIT_COLORS.amber },
  completed: { label: "已完成", accent: CIRCUIT_COLORS.green },
  cancelled: { label: "已取消", accent: CIRCUIT_COLORS.slate },
};

export function statusMeta(status: ProjectStatus): StatusMeta {
  return STATUS_META[status] ?? { label: status, accent: CIRCUIT_COLORS.slate };
}

// 生产线生命周期顺序(规划 → 进行 → 暂停 → 完成 → 取消)。
// 仅用于把同状态的节点分到同一条「泳道」并决定泳道从左到右的排列次序,
// 不据此合成任何连线 —— 项目数据里没有真实的项目间关系。
export const STATUS_FLOW_ORDER: ProjectStatus[] = [
  "planned",
  "in_progress",
  "paused",
  "completed",
  "cancelled",
];

// 取某状态在生命周期里的序号;未知状态排到最后,保证泳道排列稳定。
export function statusFlowRank(status: ProjectStatus): number {
  const i = STATUS_FLOW_ORDER.indexOf(status);
  return i === -1 ? STATUS_FLOW_ORDER.length : i;
}
