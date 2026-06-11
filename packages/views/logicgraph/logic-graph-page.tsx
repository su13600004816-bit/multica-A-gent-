"use client";

import { LogicGraphCanvas } from "./logic-graph-canvas";

// 逻辑图 page — a standalone, independent canvas surface (lives on main; the
// squad canvas inherits the feature by merging main, it is NOT grafted onto a
// canvas branch). Native Multica chrome: same border-b header + tokens as the
// other dashboard pages; the body is one full-bleed editable canvas.
export function LogicGraphPage() {
  return (
    <div className="flex flex-1 min-h-0 flex-col">
      <div className="flex h-11 shrink-0 items-center gap-2 border-b px-4">
        <h1 className="truncate text-sm font-medium text-foreground">逻辑图</h1>
        <span className="hidden text-xs text-muted-foreground sm:inline">
          梳理架构 / 逻辑关系 · 模型生成 · 自查漏
        </span>
      </div>
      <div className="min-h-0 flex-1 p-3 md:p-4">
        <LogicGraphCanvas />
      </div>
    </div>
  );
}
