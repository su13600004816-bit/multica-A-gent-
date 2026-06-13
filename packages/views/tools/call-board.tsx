"use client";

import { useState } from "react";
import { Activity, AlertTriangle, RefreshCw } from "lucide-react";
import { cn } from "@multica/ui/lib/utils";
import { Badge } from "@multica/ui/components/ui/badge";
import { Card, CardHeader, CardTitle } from "@multica/ui/components/ui/card";
import { ScrollArea } from "@multica/ui/components/ui/scroll-area";
import { Skeleton } from "@multica/ui/components/ui/skeleton";
import {
  useCallStats,
  useProblems,
  useRecentCalls,
  isFailed,
  timeAgo,
  type ToolCall,
  type ToolProblem,
} from "./telemetry";
import { ToolDetailSheet } from "./tool-detail";

/** Small labelled stat tile. Semantic tokens only. */
function Stat({ label, value, hint }: { label: string; value: string | number; hint?: string }) {
  return (
    <div className="flex flex-col gap-0.5 rounded-md border bg-card px-3 py-2">
      <span className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
        {label}
      </span>
      <span className="font-mono text-lg tabular-nums text-foreground">{value}</span>
      {hint ? <span className="text-[10px] text-muted-foreground">{hint}</span> : null}
    </div>
  );
}

function InterfaceBadge({ iface }: { iface: string }) {
  // api/mcp/cli — distinct outline badges, no hardcoded colors.
  const label = (iface || "api").toUpperCase();
  return (
    <Badge variant="outline" className="font-mono text-[10px]">
      {label}
    </Badge>
  );
}

function StatusBadge({ status }: { status: string }) {
  if (isFailed(status)) {
    return (
      <Badge variant="destructive" className="font-mono text-[10px]">
        失败
      </Badge>
    );
  }
  return (
    <Badge variant="secondary" className="font-mono text-[10px]">
      成功
    </Badge>
  );
}

function CallRow({ call, onSelect }: { call: ToolCall; onSelect: (t: string) => void }) {
  return (
    <button
      type="button"
      onClick={() => onSelect(call.tool)}
      className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left transition-colors hover:bg-accent/50"
    >
      <span className="min-w-0 flex-1 truncate font-mono text-xs text-foreground">{call.tool}</span>
      <span className="hidden shrink-0 truncate text-[11px] text-muted-foreground sm:block sm:max-w-28">
        {call.caller}
      </span>
      <InterfaceBadge iface={call.interface} />
      <StatusBadge status={call.status} />
      <span className="shrink-0 font-mono text-[10px] tabular-nums text-muted-foreground">
        {timeAgo(call.ts)}
      </span>
    </button>
  );
}

function ProblemCard({ problem, onSelect }: { problem: ToolProblem; onSelect: (t: string) => void }) {
  let callers: string[] = [];
  try {
    callers = JSON.parse(problem.callers || "[]");
  } catch {
    callers = [];
  }
  return (
    <button
      type="button"
      onClick={() => onSelect(problem.tool)}
      className="flex w-full flex-col gap-1 rounded-md border bg-card px-3 py-2 text-left transition-colors hover:bg-accent/40"
    >
      <div className="flex items-center gap-2">
        {problem.slo_breached ? (
          <AlertTriangle className="size-3.5 shrink-0 text-muted-foreground" />
        ) : null}
        <span className="min-w-0 flex-1 truncate font-mono text-xs text-foreground">
          {problem.tool}
        </span>
        <Badge variant="destructive" className="font-mono text-[10px]">
          ×{problem.count}
        </Badge>
        <Badge variant="outline" className="font-mono text-[10px]">
          {problem.status}
        </Badge>
      </div>
      <span className="truncate font-mono text-[11px] text-muted-foreground">
        {problem.error_code}
      </span>
      {callers.length > 0 ? (
        <span className="truncate text-[10px] text-muted-foreground">
          调用方: {callers.join(", ")}
        </span>
      ) : null}
    </button>
  );
}

export function CallBoard() {
  const [selected, setSelected] = useState<string | null>(null);
  const stats = useCallStats();
  const recent = useRecentCalls({ limit: 80 });
  const problems = useProblems();

  const s = stats.data;
  const okCount = s ? (s.by_status.ok ?? 0) + (s.by_status.success ?? 0) + (s.by_status.succeeded ?? 0) : 0;
  const failCount = s ? (s.by_status.failed ?? 0) + (s.by_status.error ?? 0) : 0;
  const iface = s?.by_interface ?? {};

  return (
    <div className="flex flex-1 min-h-0 flex-col">
      {/* Header */}
      <div className="flex items-center gap-2 border-b px-4 py-3">
        <Activity className="size-4 text-muted-foreground" />
        <h1 className="truncate text-sm font-medium text-foreground">调用看板</h1>
        <Badge variant="secondary" className="font-mono">
          {s?.total ?? 0}
        </Badge>
        <span className="ml-auto flex items-center gap-1 text-[10px] text-muted-foreground">
          <RefreshCw className={cn("size-3", (stats.isFetching || recent.isFetching) && "animate-spin")} />
          实时
        </span>
      </div>

      <ScrollArea className="min-h-0 flex-1">
        <div className="flex flex-col gap-4 p-4 md:p-6">
          {/* Stat tiles */}
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
            {stats.isLoading ? (
              Array.from({ length: 6 }).map((_, i) => <Skeleton key={i} className="h-14 rounded-md" />)
            ) : (
              <>
                <Stat label="24h 调用" value={s?.total ?? 0} />
                <Stat label="成功" value={okCount} />
                <Stat label="失败" value={failCount} />
                <Stat label="API" value={iface.api ?? 0} hint="网关 REST" />
                <Stat label="MCP" value={iface.mcp ?? 0} hint="模型直调" />
                <Stat label="CLI" value={iface.cli ?? 0} hint="命令行" />
              </>
            )}
          </div>

          {/* Two columns: live stream + problems. Stacks on narrow/foldable. */}
          <div className="flex flex-col gap-4 lg:flex-row">
            {/* Live call stream */}
            <Card size="sm" className="min-w-0 flex-1">
              <CardHeader>
                <CardTitle className="flex items-center gap-2 text-sm">
                  实时调用流
                  <span className="font-mono text-xs font-normal text-muted-foreground">
                    谁在调 · 经哪个接口 · 状态
                  </span>
                </CardTitle>
              </CardHeader>
              <div className="flex flex-col gap-0.5 px-2 pb-2">
                {recent.isLoading ? (
                  Array.from({ length: 8 }).map((_, i) => (
                    <Skeleton key={i} className="h-7 rounded-md" />
                  ))
                ) : (recent.data?.length ?? 0) === 0 ? (
                  <p className="px-2 py-6 text-center text-xs text-muted-foreground">
                    暂无调用记录
                  </p>
                ) : (
                  recent.data!.map((c) => <CallRow key={c.id} call={c} onSelect={setSelected} />)
                )}
              </div>
            </Card>

            {/* Problems / failing tools */}
            <Card size="sm" className="min-w-0 lg:w-80">
              <CardHeader>
                <CardTitle className="flex items-center gap-2 text-sm">
                  问题工具
                  {problems.data && problems.data.length > 0 ? (
                    <Badge variant="destructive" className="font-mono text-[10px]">
                      {problems.data.length}
                    </Badge>
                  ) : null}
                </CardTitle>
              </CardHeader>
              <div className="flex flex-col gap-2 px-3 pb-3">
                {problems.isLoading ? (
                  Array.from({ length: 3 }).map((_, i) => (
                    <Skeleton key={i} className="h-14 rounded-md" />
                  ))
                ) : (problems.data?.length ?? 0) === 0 ? (
                  <p className="py-6 text-center text-xs text-muted-foreground">暂无问题 ✓</p>
                ) : (
                  problems.data!.map((p) => (
                    <ProblemCard key={p.problem_id} problem={p} onSelect={setSelected} />
                  ))
                )}
              </div>
            </Card>
          </div>
        </div>
      </ScrollArea>

      <ToolDetailSheet
        toolName={selected}
        onOpenChange={(open) => {
          if (!open) setSelected(null);
        }}
      />
    </div>
  );
}
