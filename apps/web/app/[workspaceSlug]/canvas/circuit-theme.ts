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
