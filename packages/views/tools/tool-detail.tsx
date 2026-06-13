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
import { Tooltip, TooltipContent, TooltipTrigger } from "@multica/ui/components/ui/tooltip";
import { findTool } from "./data";
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
  const callExample = [
    `POST /v12/${tool.name}`,
    "{",
    '  "request_id": "<uuid>",',
    '  "action": "execute",',
    '  "payload": {}',
    "}",
  ].join("\n");

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
        <div className="flex flex-col gap-1 pt-1">
          <span className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground/80">
            module_path
          </span>
          <code className="break-all font-mono text-xs text-muted-foreground">
            {tool.module_path}
          </code>
        </div>
      </Section>
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
