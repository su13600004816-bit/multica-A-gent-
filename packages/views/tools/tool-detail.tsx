"use client";

import { useState, type ReactNode } from "react";
import { Check, Copy } from "lucide-react";
import { cn } from "@multica/ui/lib/utils";
import { Badge } from "@multica/ui/components/ui/badge";
import { ScrollArea } from "@multica/ui/components/ui/scroll-area";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@multica/ui/components/ui/sheet";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@multica/ui/components/ui/tabs";
import { Tooltip, TooltipContent, TooltipTrigger } from "@multica/ui/components/ui/tooltip";
import { findTool } from "./data";
import {
  useToolTelemetry,
  useToolSpec,
  timeAgo,
  isFailed,
  type ToolCall,
  type ToolProblem,
  type JsonSchema,
  type JsonSchemaProp,
} from "./telemetry";
import type { Tool } from "./types";

// ---------------------------------------------------------------------------
// Layout helpers (mirrors agent-detail-inspector's Section pattern)
// ---------------------------------------------------------------------------

function Section({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-2 border-b px-5 py-4 last:border-b-0">
      <div className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
        {label}
      </div>
      {children}
    </div>
  );
}

/** Status badge: built uses the success-leaning primary token, others muted. */
function StatusBadge({ status }: { status: string }) {
  const isBuilt = status === "built";
  return (
    <Badge variant={isBuilt ? "default" : "secondary"} className="font-mono">
      {isBuilt ? "已建成" : status === "planned" ? "规划中" : status}
    </Badge>
  );
}

/**
 * A monospace code line with a click-to-copy affordance. Uses semantic tokens
 * only (bg-muted / text-muted-foreground). The clipboard write is best-effort —
 * if the environment denies it, the affordance just doesn't flip to the check
 * state, which is acceptable for a read-only inspector.
 */
function CopyableCode({ label, value }: { label: string; value: string }) {
  const [copied, setCopied] = useState(false);

  const handleCopy = () => {
    void navigator.clipboard
      ?.writeText(value)
      .then(() => {
        setCopied(true);
        window.setTimeout(() => setCopied(false), 1200);
      })
      .catch(() => {
        /* clipboard unavailable — leave state unchanged */
      });
  };

  return (
    <div className="flex flex-col gap-1">
      <span className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground/80">
        {label}
      </span>
      <div className="group/code flex items-start gap-2 rounded-md bg-muted px-2.5 py-1.5">
        <code className="min-w-0 flex-1 break-all font-mono text-xs text-foreground">
          {value}
        </code>
        <Tooltip>
          <TooltipTrigger
            render={<button type="button" />}
            onClick={handleCopy}
            className="shrink-0 rounded p-0.5 text-muted-foreground transition-colors hover:text-foreground"
            aria-label="复制"
          >
            {copied ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
          </TooltipTrigger>
          <TooltipContent side="left">{copied ? "已复制" : "复制"}</TooltipContent>
        </Tooltip>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// ToolDetail — the read-only inspector body (also used by the standalone route)
// ---------------------------------------------------------------------------

interface ToolDetailProps {
  /** Tool name (unique). When the name doesn't resolve, a not-found state shows. */
  name: string;
}

export function ToolDetail({ name }: ToolDetailProps) {
  const tool = findTool(name);
  if (!tool) {
    return (
      <div className="flex flex-1 items-center justify-center p-10 text-sm text-muted-foreground">
        未找到工具「{name}」
      </div>
    );
  }
  return <ToolDetailBody tool={tool} />;
}

function ToolDetailBody({ tool }: { tool: Tool }) {
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {/* Header */}
      <div className="flex flex-col gap-2 border-b px-5 pb-4 pt-1">
        <div className="flex items-center gap-2">
          <h2 className="min-w-0 break-all font-heading text-base font-medium text-foreground">
            {tool.name}
          </h2>
          <span className="shrink-0 font-mono text-xs text-muted-foreground">
            v{tool.version}
          </span>
        </div>
        <div className="flex flex-wrap items-center gap-1.5">
          <Badge variant="outline">{tool.category_zh}</Badge>
          <StatusBadge status={tool.status} />
        </div>
      </div>

      {/* Level-3 tabs: 概览 / 调用记录 / 配置 / 日志 */}
      <Tabs defaultValue="overview" className="flex min-h-0 flex-1 flex-col">
        <TabsList className="mx-5 mt-3 flex w-auto flex-wrap">
          <TabsTrigger value="overview">概览</TabsTrigger>
          <TabsTrigger value="params">参数</TabsTrigger>
          <TabsTrigger value="calls">调用</TabsTrigger>
          <TabsTrigger value="config">配置</TabsTrigger>
          <TabsTrigger value="logs">日志</TabsTrigger>
        </TabsList>

        <TabsContent value="overview" className="mt-0 min-h-0 flex-1">
          <OverviewTab tool={tool} />
        </TabsContent>
        <TabsContent value="params" className="mt-0 min-h-0 flex-1">
          <ParamsTab tool={tool} />
        </TabsContent>
        <TabsContent value="calls" className="mt-0 min-h-0 flex-1">
          <CallsTab tool={tool} />
        </TabsContent>
        <TabsContent value="config" className="mt-0 min-h-0 flex-1">
          <ConfigTab tool={tool} />
        </TabsContent>
        <TabsContent value="logs" className="mt-0 min-h-0 flex-1">
          <LogsTab tool={tool} />
        </TabsContent>
      </Tabs>
    </div>
  );
}

function OverviewTab({ tool }: { tool: Tool }) {
  const callExample = [
    `POST /v12/${tool.name}`,
    "{",
    '  "request_id": "<uuid>",',
    '  "action": "execute",',
    '  "payload": {}',
    "}",
  ].join("\n");
  return (
    <div className="flex flex-col">
      <Section label="三接口 API / MCP / CLI">
        <div className="flex flex-col gap-2.5">
          <CopyableCode label="API" value={tool.interfaces.api} />
          <CopyableCode label="MCP" value={tool.interfaces.mcp} />
          <CopyableCode label="CLI" value={tool.interfaces.cli} />
        </div>
      </Section>
      <Section label="依赖">
        {tool.deps.length > 0 ? (
          <div className="flex flex-wrap gap-1.5">
            {tool.deps.map((dep) => (
              <Badge key={dep} variant="secondary" className="font-mono">
                {dep}
              </Badge>
            ))}
          </div>
        ) : (
          <span className="text-sm italic text-muted-foreground/60">无</span>
        )}
      </Section>
      <Section label="调用方式">
        <pre className="overflow-x-auto rounded-md bg-muted px-3 py-2 font-mono text-xs leading-relaxed text-foreground">
          {callExample}
        </pre>
      </Section>
      <Section label="描述 / 说明">
        <p className="text-sm leading-relaxed text-foreground">{tool.description}</p>
      </Section>
    </div>
  );
}

function EmptyHint({ children }: { children: ReactNode }) {
  return <p className="px-5 py-8 text-center text-xs text-muted-foreground">{children}</p>;
}

/** Copyable JSON code block (examples). */
function JsonBlock({ label, value }: { label: string; value: unknown }) {
  const text = JSON.stringify(value, null, 2);
  return (
    <div className="flex flex-col gap-1">
      <span className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground/80">
        {label}
      </span>
      <pre className="max-h-64 overflow-auto rounded-md bg-muted px-3 py-2 font-mono text-xs leading-relaxed text-foreground">
        {text}
      </pre>
    </div>
  );
}

/** Render JSON-Schema constraints (enum/min/max/pattern/default) as compact chips. */
function propConstraints(p: JsonSchemaProp): string[] {
  const out: string[] = [];
  if (p.enum) out.push(`枚举: ${p.enum.map(String).slice(0, 6).join(" | ")}`);
  if (p.default !== undefined) out.push(`默认 ${JSON.stringify(p.default)}`);
  if (p.minimum !== undefined) out.push(`≥${p.minimum}`);
  if (p.maximum !== undefined) out.push(`≤${p.maximum}`);
  if (p.minLength !== undefined) out.push(`长度≥${p.minLength}`);
  if (p.maxLength !== undefined) out.push(`长度≤${p.maxLength}`);
  if (p.pattern) out.push(`匹配 /${p.pattern}/`);
  return out;
}

function typeLabel(p: JsonSchemaProp): string {
  const t = Array.isArray(p.type) ? p.type.join("|") : p.type;
  if (t === "array" && p.items?.type) return `${p.items.type}[]`;
  return t || "any";
}

/** A JSON-Schema property table: 字段 / 类型 / 必填 / 说明 / 约束. */
function SchemaTable({ schema }: { schema: JsonSchema | null | undefined }) {
  const props = schema?.properties ?? {};
  const required = new Set(schema?.required ?? []);
  const keys = Object.keys(props);
  if (keys.length === 0) {
    return <span className="text-sm italic text-muted-foreground/60">无字段定义</span>;
  }
  return (
    <div className="flex flex-col divide-y rounded-md border">
      {keys.map((k) => {
        const p = props[k]!;
        const cons = propConstraints(p);
        return (
          <div key={k} className="flex flex-col gap-1 px-3 py-2">
            <div className="flex flex-wrap items-center gap-1.5">
              <code className="font-mono text-xs text-foreground">{k}</code>
              <Badge variant="outline" className="font-mono text-[10px]">
                {typeLabel(p)}
              </Badge>
              {required.has(k) ? (
                <Badge variant="destructive" className="font-mono text-[10px]">
                  必填
                </Badge>
              ) : (
                <span className="text-[10px] text-muted-foreground">可选</span>
              )}
            </div>
            {p.description ? (
              <span className="text-[11px] text-muted-foreground">{p.description}</span>
            ) : null}
            {cons.length > 0 ? (
              <div className="flex flex-wrap gap-1">
                {cons.map((c, i) => (
                  <span key={i} className="rounded bg-muted px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground">
                    {c}
                  </span>
                ))}
              </div>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}

/** 参数 — input/output JSON-Schema + 示例(this is the big missing piece). */
function ParamsTab({ tool }: { tool: Tool }) {
  const { data, isLoading } = useToolSpec(tool.name);
  if (isLoading) return <EmptyHint>加载参数规格…</EmptyHint>;
  if (!data) return <EmptyHint>暂无参数规格</EmptyHint>;
  return (
    <div className="flex flex-col">
      <Section label="输入参数 (input schema)">
        <SchemaTable schema={data.input_schema} />
      </Section>
      {data.output_schema?.properties ? (
        <Section label="输出 (output schema)">
          <SchemaTable schema={data.output_schema} />
        </Section>
      ) : null}
      {data.example_input != null ? (
        <Section label="示例">
          <div className="flex flex-col gap-3">
            <JsonBlock label="请求示例" value={data.example_input} />
            {data.example_output != null ? (
              <JsonBlock label="响应示例" value={data.example_output} />
            ) : null}
          </div>
        </Section>
      ) : null}
    </div>
  );
}

/** 调用记录 — live recent calls for this tool (who/interface/status/time). */
function CallsTab({ tool }: { tool: Tool }) {
  const { data, isLoading } = useToolTelemetry(tool.name);
  const calls = data?.calls ?? [];
  if (isLoading) return <EmptyHint>加载调用记录…</EmptyHint>;
  if (calls.length === 0) return <EmptyHint>该工具暂无调用记录</EmptyHint>;
  return (
    <div className="flex flex-col gap-0.5 px-3 py-2">
      {calls.map((c: ToolCall) => (
        <div key={c.id} className="flex items-center gap-2 rounded-md px-2 py-1.5 hover:bg-accent/40">
          <span className="min-w-0 flex-1 truncate text-[11px] text-muted-foreground">{c.caller}</span>
          <Badge variant="outline" className="font-mono text-[10px]">
            {(c.interface || "api").toUpperCase()}
          </Badge>
          <Badge variant={isFailed(c.status) ? "destructive" : "secondary"} className="font-mono text-[10px]">
            {isFailed(c.status) ? "失败" : "成功"}
          </Badge>
          <span className="shrink-0 font-mono text-[10px] tabular-nums text-muted-foreground">
            {c.duration_ms}ms
          </span>
          <span className="shrink-0 font-mono text-[10px] tabular-nums text-muted-foreground">
            {timeAgo(c.ts)}
          </span>
        </div>
      ))}
    </div>
  );
}

/** A key/value config row. */
function KV({ k, v }: { k: string; v: ReactNode }) {
  return (
    <div className="flex items-start gap-3 py-0.5">
      <span className="w-20 shrink-0 text-[11px] text-muted-foreground">{k}</span>
      <span className="min-w-0 flex-1 break-all font-mono text-xs text-foreground">{v}</span>
    </div>
  );
}

/** 配置 — health + governance/lifecycle + dependency relations + runtime config + docs. */
function ConfigTab({ tool }: { tool: Tool }) {
  const { data: tel } = useToolTelemetry(tool.name);
  const { data: spec } = useToolSpec(tool.name);
  const health = tel?.health;
  const manifest = (spec?.manifest ?? {}) as Record<string, unknown>;
  const stateLabel =
    health?.state === "down" ? "宕机" : health?.state === "degraded" ? "降级" : "运行中";
  // Consumers = distinct callers seen calling this tool (depended-on-by).
  const consumers = Array.from(new Set((tel?.calls ?? []).map((c) => c.caller))).slice(0, 12);
  const deps = (manifest.deps as string[]) ?? tool.deps ?? [];
  const docs = spec?.docs ?? {};
  const docList = (["api", "mcp", "cli"] as const).filter((k) => docs[k]);

  return (
    <div className="flex flex-col">
      <Section label="运行时健康态">
        {health ? (
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant={health.state === "running" ? "secondary" : "destructive"} className="font-mono">
              {stateLabel}
            </Badge>
            <span className="font-mono text-xs text-muted-foreground">
              失败率 {(health.fail_rate * 100).toFixed(0)}% · {health.calls} 次调用 · {health.fails} 失败
            </span>
          </div>
        ) : (
          <span className="text-sm italic text-muted-foreground/60">暂无健康数据</span>
        )}
      </Section>

      <Section label="治理 / 生命周期">
        <div className="flex flex-col">
          <KV k="状态" v={String(manifest.status ?? tool.status)} />
          <KV k="版本" v={`v${manifest.version ?? tool.version}`} />
          <KV k="类目" v={tool.category_zh} />
          {manifest.pluggable !== undefined ? (
            <KV k="可插拔" v={manifest.pluggable ? "是" : "否"} />
          ) : null}
        </div>
      </Section>

      <Section label="依赖关系">
        <div className="flex flex-col gap-2">
          <div className="flex flex-col gap-1">
            <span className="text-[10px] uppercase tracking-wide text-muted-foreground/70">
              依赖 (depends-on)
            </span>
            {deps.length > 0 ? (
              <div className="flex flex-wrap gap-1.5">
                {deps.map((d) => (
                  <Badge key={d} variant="secondary" className="font-mono">
                    {d}
                  </Badge>
                ))}
              </div>
            ) : (
              <span className="text-sm italic text-muted-foreground/60">无</span>
            )}
          </div>
          <div className="flex flex-col gap-1">
            <span className="text-[10px] uppercase tracking-wide text-muted-foreground/70">
              被调用方 (consumers · 近期实测)
            </span>
            {consumers.length > 0 ? (
              <div className="flex flex-wrap gap-1.5">
                {consumers.map((c) => (
                  <Badge key={c} variant="outline" className="font-mono text-[10px]">
                    {c}
                  </Badge>
                ))}
              </div>
            ) : (
              <span className="text-sm italic text-muted-foreground/60">近期无调用</span>
            )}
          </div>
        </div>
      </Section>

      <Section label="运行配置">
        <div className="flex flex-col">
          {manifest.sandbox_cmd ? <KV k="沙盒自测" v={String(manifest.sandbox_cmd)} /> : null}
          {manifest.test_cmd ? <KV k="证伪测试" v={String(manifest.test_cmd)} /> : null}
          <KV k="module" v={tool.module_path} />
        </div>
      </Section>

      {docList.length > 0 || spec?.readme ? (
        <Section label="文档">
          <div className="flex flex-wrap gap-1.5">
            {spec?.readme ? <Badge variant="secondary">README</Badge> : null}
            {docList.map((k) => (
              <Badge key={k} variant="secondary" className="font-mono">
                {k.toUpperCase()}.md
              </Badge>
            ))}
          </div>
        </Section>
      ) : null}
    </div>
  );
}

/** 日志 — failures / problems for this tool (error logs + diagnosis). */
function LogsTab({ tool }: { tool: Tool }) {
  const { data, isLoading } = useToolTelemetry(tool.name);
  const problems = data?.problems ?? [];
  const failedCalls = (data?.calls ?? []).filter((c) => isFailed(c.status));
  if (isLoading) return <EmptyHint>加载日志…</EmptyHint>;
  if (problems.length === 0 && failedCalls.length === 0)
    return <EmptyHint>无错误日志 ✓</EmptyHint>;
  return (
    <div className="flex flex-col">
      {problems.length > 0 ? (
        <Section label="问题(聚类)">
          <div className="flex flex-col gap-2">
            {problems.map((p: ToolProblem) => (
              <div key={p.problem_id} className="flex flex-col gap-1 rounded-md border bg-card px-3 py-2">
                <div className="flex items-center gap-2">
                  <span className="min-w-0 flex-1 truncate font-mono text-xs text-foreground">
                    {p.error_code}
                  </span>
                  <Badge variant="destructive" className="font-mono text-[10px]">
                    ×{p.count}
                  </Badge>
                </div>
                {p.sample_msg ? (
                  <span className="break-all text-[11px] text-muted-foreground">{p.sample_msg}</span>
                ) : null}
                {p.diagnosis ? (
                  <span className="break-all text-[11px] text-muted-foreground/80">
                    诊断: {p.diagnosis}
                  </span>
                ) : null}
              </div>
            ))}
          </div>
        </Section>
      ) : null}
      {failedCalls.length > 0 ? (
        <Section label="近期失败调用">
          <div className="flex flex-col gap-1">
            {failedCalls.slice(0, 15).map((c) => (
              <div key={c.id} className="flex items-center gap-2 font-mono text-[11px]">
                <span className="shrink-0 text-muted-foreground">{timeAgo(c.ts)}</span>
                <span className="min-w-0 flex-1 truncate text-muted-foreground">
                  {c.error_code || c.error_msg || "failed"}
                </span>
                <Badge variant="outline" className="text-[10px]">
                  {(c.interface || "api").toUpperCase()}
                </Badge>
              </div>
            ))}
          </div>
        </Section>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// ToolDetailSheet — right-side drawer used by the list page (level-3 view)
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// ToolDetailPage — standalone route chrome (direct-link / level-3 deep link)
// ---------------------------------------------------------------------------

export function ToolDetailPage({ name }: ToolDetailProps) {
  return (
    <div className="flex flex-1 min-h-0 flex-col">
      <div className="flex h-11 shrink-0 items-center gap-2 border-b px-4">
        <h1 className="truncate text-sm font-medium text-foreground">工具详情</h1>
      </div>
      <ScrollArea className="min-h-0 flex-1">
        <div className="mx-auto w-full max-w-2xl pt-4">
          <ToolDetail name={name} />
        </div>
      </ScrollArea>
    </div>
  );
}

interface ToolDetailSheetProps {
  /** The tool name to show; null closes the sheet. */
  toolName: string | null;
  onOpenChange: (open: boolean) => void;
}

export function ToolDetailSheet({ toolName, onOpenChange }: ToolDetailSheetProps) {
  const tool = toolName ? findTool(toolName) : undefined;
  return (
    <Sheet open={!!toolName} onOpenChange={onOpenChange}>
      <SheetContent
        side="right"
        // Full-screen on narrow/foldable; a comfortable drawer on desktop.
        className={cn("w-full gap-0 p-0 sm:max-w-md md:max-w-lg")}
      >
        <SheetHeader className="sr-only">
          <SheetTitle>{tool?.name ?? "工具详情"}</SheetTitle>
          <SheetDescription>{tool?.description ?? ""}</SheetDescription>
        </SheetHeader>
        <ScrollArea className="min-h-0 flex-1 pt-5">
          {toolName ? <ToolDetail name={toolName} /> : null}
        </ScrollArea>
      </SheetContent>
    </Sheet>
  );
}
