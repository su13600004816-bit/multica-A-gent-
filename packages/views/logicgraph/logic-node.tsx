"use client";

import { Handle, Position, type NodeProps, type Node } from "@xyflow/react";

// A logic-graph node, drawn with the SAME chrome as the rest of Multica — the
// squad-canvas nodes' rounded-lg border bg-background card, semantic design
// tokens, a small status-style accent dot. No invented canvas skin or palette:
// the per-kind accent only ever resolves to existing CSS tokens
// (--info / --warning / --success / --muted-foreground / --foreground).
export type LogicNodeData = { label: string; kind: string };
export type LogicFlowNode = Node<LogicNodeData, "logicNode">;

const KIND_ACCENT: Record<string, string> = {
  brain: "var(--info)",
  line: "var(--foreground)",
  watchdog: "var(--warning)",
  decision: "var(--warning)",
  gate: "var(--success)",
  store: "var(--muted-foreground)",
  actor: "var(--muted-foreground)",
  missing: "var(--destructive)",
};

const HANDLE = "!h-2 !w-2 !border-border !bg-muted";

export function LogicNode({ data }: NodeProps<LogicFlowNode>) {
  const accent = KIND_ACCENT[data.kind] ?? "var(--muted-foreground)";
  return (
    <div
      className="min-w-[120px] max-w-[220px] rounded-lg border bg-background px-3 py-1.5 shadow-sm"
      style={{ borderColor: accent }}
    >
      <Handle type="target" position={Position.Left} className={HANDLE} />
      <div className="flex items-center gap-1.5">
        <span className="h-1.5 w-1.5 shrink-0 rounded-full" style={{ background: accent }} />
        <span className="truncate text-xs font-medium">{data.label}</span>
      </div>
      <span className="text-[10px] text-muted-foreground">{data.kind}</span>
      <Handle type="source" position={Position.Right} className={HANDLE} />
    </div>
  );
}
