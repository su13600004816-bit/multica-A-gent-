"use client";

// Canvas node renderer (PL-157 phase 2). A pure presentational @xyflow/react
// custom node — status colour arrives via props (data.status) from the
// WS-backed store; the node itself does NOT fetch (README decoupling point 2).

import { Handle, Position, type NodeProps } from "@xyflow/react";
import { CheckCircle2, CircleDashed, Loader2, ShieldCheck, XCircle, type LucideIcon } from "lucide-react";

import { cn } from "@multica/ui/lib/utils";

import type { CircuitStatus } from "../lib/circuit-status";
import type { CircuitNodeData } from "../lib/rf-mapping";

interface StatusStyle {
  border: string;
  text: string;
  dot: string;
  icon: LucideIcon;
  spin?: boolean;
  label: string;
}

const STATUS_STYLES: Record<CircuitStatus, StatusStyle> = {
  running: { border: "border-info", text: "text-info", dot: "bg-info", icon: Loader2, spin: true, label: "运行中" },
  done: { border: "border-success", text: "text-success", dot: "bg-success", icon: CheckCircle2, label: "完成" },
  active: { border: "border-success", text: "text-success", dot: "bg-success", icon: CheckCircle2, label: "活跃" },
  failed: { border: "border-destructive", text: "text-destructive", dot: "bg-destructive", icon: XCircle, label: "失败" },
  blocked: { border: "border-destructive", text: "text-destructive", dot: "bg-destructive", icon: XCircle, label: "阻塞" },
  pending: { border: "border-warning", text: "text-warning", dot: "bg-warning", icon: Loader2, spin: true, label: "排队中" },
  draft: { border: "border-border", text: "text-muted-foreground", dot: "bg-muted-foreground", icon: CircleDashed, label: "草稿" },
  unmapped: { border: "border-border", text: "text-muted-foreground", dot: "bg-muted-foreground", icon: CircleDashed, label: "未映射" },
  neutral: { border: "border-border", text: "text-muted-foreground", dot: "bg-muted-foreground", icon: CircleDashed, label: "待派发" },
};

export function CircuitNode({ data, selected }: NodeProps) {
  const { node, status } = data as CircuitNodeData;
  const style = STATUS_STYLES[status] ?? STATUS_STYLES.neutral;
  const Icon = style.icon;
  const isAudit = node.kind === "audit";

  return (
    <div
      className={cn(
        "w-60 rounded-xl border bg-card px-3 py-2.5 text-card-foreground shadow-sm transition-colors",
        style.border,
        selected && "ring-2 ring-ring",
      )}
      data-status={status}
    >
      <Handle
        type="target"
        position={Position.Left}
        className="!h-2.5 !w-2.5 !border-2 !border-background !bg-muted-foreground"
      />

      <div className="flex items-center justify-between gap-2">
        <span
          className={cn(
            "rounded px-1.5 py-0.5 text-[10px] font-semibold tracking-wide uppercase",
            isAudit ? "bg-warning/15 text-warning" : "bg-primary/10 text-primary",
          )}
        >
          {isAudit ? "AUDIT" : "DEV"}
        </span>
        <span className={cn("flex items-center gap-1 text-[11px] font-medium", style.text)}>
          <Icon className={cn("size-3", style.spin && "animate-spin")} aria-hidden />
          {style.label}
        </span>
      </div>

      <p className="mt-1.5 line-clamp-2 text-xs leading-snug text-foreground" title={node.instruction}>
        {node.instruction.trim() || <span className="text-muted-foreground italic">（未填写指令）</span>}
      </p>

      <div className="mt-1.5 flex items-center justify-between text-[10px] text-muted-foreground">
        <span className="flex items-center gap-1">
          {isAudit && <ShieldCheck className="size-3" aria-hidden />}
          {node.executor} · {node.mode}
        </span>
        {node.mode === "write" && (
          <span className={cn("h-1.5 w-1.5 rounded-full", style.dot)} aria-hidden />
        )}
      </div>

      {node.mode === "write" && node.ownedPaths.length > 0 && (
        <p className="mt-1 truncate text-[10px] text-muted-foreground/80" title={node.ownedPaths.join(", ")}>
          {node.ownedPaths.length} 路径：{node.ownedPaths.join(", ")}
        </p>
      )}

      <Handle
        type="source"
        position={Position.Right}
        className="!h-2.5 !w-2.5 !border-2 !border-background !bg-muted-foreground"
      />
    </div>
  );
}
