"use client";

import { useEffect } from "react";
import type { Project } from "@multica/core/types";
import { CIRCUIT_COLORS, statusMeta } from "./circuit-theme";

// 内联节点详情面板:点击电路板节点后从右侧滑出,展示该生产线(项目)的
// 信息 / 状态 / 快捷操作,而不是跳走到独立详情页。
export type DetailPanelProps = {
  project: Project;
  code: string;
  // 跳转到完整生产线详情页(快捷操作)。
  onOpenDetail: () => void;
  onClose: () => void;
};

const PRIORITY_LABEL: Record<Project["priority"], string> = {
  urgent: "紧急",
  high: "高",
  medium: "中",
  low: "低",
  none: "无",
};

function formatDate(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(
    d.getDate(),
  ).padStart(2, "0")}`;
}

export function DetailPanel({
  project,
  code,
  onOpenDetail,
  onClose,
}: DetailPanelProps) {
  const meta = statusMeta(project.status);
  const accent = meta.accent;
  const pct =
    project.issue_count > 0
      ? Math.round((project.done_count / project.issue_count) * 100)
      : 0;

  // 支持 Esc 关闭面板。
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <aside
      className="absolute right-0 top-0 z-20 flex h-full w-[340px] max-w-[85vw] flex-col border-l shadow-2xl"
      style={{
        backgroundColor: CIRCUIT_COLORS.panel,
        borderColor: accent,
        boxShadow: `-12px 0 40px rgba(0,0,0,0.45), 0 0 24px ${accent}22`,
      }}
      role="dialog"
      aria-label={`${project.title} 详情`}
    >
      <div
        className="flex items-start justify-between gap-3 border-b px-4 py-3"
        style={{ borderColor: CIRCUIT_COLORS.line }}
      >
        <div className="min-w-0 flex-1">
          <div
            className="font-mono text-[10px] uppercase tracking-[0.16em]"
            style={{ color: accent }}
          >
            {code} · 生产线详情
          </div>
          <div
            className="mt-1 truncate text-base font-semibold leading-6"
            style={{ color: CIRCUIT_COLORS.slateWhite }}
            title={project.title}
          >
            {project.title}
          </div>
        </div>
        <button
          type="button"
          onClick={onClose}
          aria-label="关闭面板"
          className="shrink-0 border px-2 py-1 font-mono text-xs leading-none transition-colors hover:brightness-125"
          style={{ borderColor: CIRCUIT_COLORS.line, color: CIRCUIT_COLORS.slate }}
        >
          ✕
        </button>
      </div>

      <div className="flex-1 space-y-4 overflow-y-auto px-4 py-4">
        {/* 状态 */}
        <div className="flex items-center gap-2">
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
            className="border px-2 py-1 font-mono text-[10px]"
            style={{ borderColor: CIRCUIT_COLORS.line, color: CIRCUIT_COLORS.slate }}
          >
            优先级 {PRIORITY_LABEL[project.priority]}
          </span>
        </div>

        {/* 任务完成进度 */}
        <div>
          <div
            className="mb-1 flex items-center justify-between font-mono text-[11px]"
            style={{ color: CIRCUIT_COLORS.slate }}
          >
            <span>任务进度</span>
            <span>
              {project.done_count}/{project.issue_count} · {pct}%
            </span>
          </div>
          <div
            className="h-1.5 w-full overflow-hidden"
            style={{ backgroundColor: CIRCUIT_COLORS.line }}
          >
            <div
              className="h-full transition-all"
              style={{ width: `${pct}%`, backgroundColor: accent }}
            />
          </div>
        </div>

        {/* 关键信息 */}
        <dl className="grid grid-cols-2 gap-3">
          <Stat label="任务数" value={String(project.issue_count)} />
          <Stat label="已完成" value={String(project.done_count)} />
          <Stat label="资源数" value={String(project.resource_count)} />
          <Stat label="更新于" value={formatDate(project.updated_at)} />
        </dl>

        {/* 描述 */}
        <div>
          <div
            className="mb-1 font-mono text-[11px] uppercase tracking-[0.12em]"
            style={{ color: CIRCUIT_COLORS.slate }}
          >
            描述
          </div>
          <p
            className="whitespace-pre-wrap text-sm leading-6"
            style={{ color: CIRCUIT_COLORS.slateWhite }}
          >
            {project.description?.trim() || "暂无描述。"}
          </p>
        </div>
      </div>

      {/* 快捷操作 */}
      <div
        className="border-t px-4 py-3"
        style={{ borderColor: CIRCUIT_COLORS.line }}
      >
        <button
          type="button"
          onClick={onOpenDetail}
          className="w-full border px-3 py-2 text-center text-sm font-semibold transition-colors hover:brightness-125"
          style={{
            borderColor: accent,
            color: accent,
            backgroundColor: `${accent}14`,
          }}
        >
          进入完整生产线详情页 →
        </button>
      </div>
    </aside>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div
      className="border px-2.5 py-2"
      style={{ borderColor: CIRCUIT_COLORS.line }}
    >
      <div
        className="font-mono text-[10px] uppercase tracking-[0.12em]"
        style={{ color: CIRCUIT_COLORS.slate }}
      >
        {label}
      </div>
      <div
        className="mt-0.5 text-sm font-semibold"
        style={{ color: CIRCUIT_COLORS.slateWhite }}
      >
        {value}
      </div>
    </div>
  );
}
