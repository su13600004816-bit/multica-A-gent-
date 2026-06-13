"use client";

// Live tool-call telemetry data layer. Talks to the v12 gateway's /telemetry
// routes (reverse-proxied at /v12 on the production host). This is a SEPARATE
// service from the multica Go backend, so it does not go through the core api
// client — a thin fetch + TanStack Query is the right boundary here.
//
// Per the repo's API Response Compatibility rules: parse defensively (optional
// chaining + explicit defaults), never bare-cast a network body into a type.

import { useQuery } from "@tanstack/react-query";

/** Base path for the v12 telemetry API. Relative → same-origin (Caddy proxies /v12). */
export const TELEMETRY_BASE =
  (typeof process !== "undefined" && process.env?.NEXT_PUBLIC_UTOS_V12_BASE) || "/v12";

export interface ToolCall {
  id: number;
  ts: number;
  tool: string;
  caller: string;
  action: string;
  interface: string; // api | mcp | cli
  status: string; // ok | success | failed | ...
  duration_ms: number;
  error_code?: string;
  error_msg?: string;
  payload_summary?: string;
  trace_id?: string;
}

export interface TopTool {
  tool: string;
  calls: number;
  fails: number;
  avg_ms: number;
}

export interface CallStats {
  total: number;
  by_status: Record<string, number>;
  by_interface: Record<string, number>;
  by_caller: { caller: string; calls: number }[];
  top_tools: TopTool[];
  failing_tools: { tool: string; fails: number }[];
}

export interface ToolProblem {
  problem_id: string;
  tool: string;
  error_code: string;
  count: number;
  first_seen: number;
  last_seen: number;
  callers: string; // JSON array string
  interfaces: string;
  sample_msg: string;
  sample_payload: string;
  status: string;
  slo_breached: number;
  diagnosis?: string;
  proposal?: string;
}

export interface ToolHealth {
  tool: string;
  state: string; // running | degraded | down
  calls: number;
  fails: number;
  fail_rate: number;
}

async function getJSON<T>(path: string): Promise<T> {
  const res = await fetch(`${TELEMETRY_BASE}${path}`, {
    credentials: "include",
    headers: { accept: "application/json" },
  });
  if (!res.ok) throw new Error(`telemetry ${path} → ${res.status}`);
  return (await res.json()) as T;
}

// --- normalizers (fail closed: bad shape → safe empty defaults) ---------------

function asStats(raw: unknown): CallStats {
  const r = (raw ?? {}) as Record<string, unknown>;
  return {
    total: typeof r.total === "number" ? r.total : 0,
    by_status: (r.by_status as Record<string, number>) ?? {},
    by_interface: (r.by_interface as Record<string, number>) ?? {},
    by_caller: Array.isArray(r.by_caller) ? (r.by_caller as CallStats["by_caller"]) : [],
    top_tools: Array.isArray(r.top_tools) ? (r.top_tools as TopTool[]) : [],
    failing_tools: Array.isArray(r.failing_tools)
      ? (r.failing_tools as CallStats["failing_tools"])
      : [],
  };
}

function asCalls(raw: unknown): ToolCall[] {
  const r = (raw ?? {}) as Record<string, unknown>;
  return Array.isArray(r.calls) ? (r.calls as ToolCall[]) : [];
}

// --- hooks --------------------------------------------------------------------

const REFRESH_MS = 5000;

export function useCallStats(windowS = 86400) {
  return useQuery({
    queryKey: ["utos-telemetry", "stats", windowS],
    queryFn: () => getJSON<unknown>(`/telemetry/stats?window=${windowS}`).then(asStats),
    refetchInterval: REFRESH_MS,
  });
}

export function useRecentCalls(opts: { limit?: number; status?: string; interface?: string } = {}) {
  const q = new URLSearchParams();
  q.set("limit", String(opts.limit ?? 80));
  if (opts.status) q.set("status", opts.status);
  if (opts.interface) q.set("interface", opts.interface);
  return useQuery({
    queryKey: ["utos-telemetry", "recent", opts],
    queryFn: () => getJSON<unknown>(`/telemetry/recent?${q.toString()}`).then(asCalls),
    refetchInterval: REFRESH_MS,
  });
}

export function useProblems(status?: string) {
  return useQuery({
    queryKey: ["utos-telemetry", "problems", status],
    queryFn: () =>
      getJSON<{ problems?: ToolProblem[] }>(
        `/telemetry/problems${status ? `?status=${status}` : ""}`,
      ).then((r) => (Array.isArray(r.problems) ? r.problems : [])),
    refetchInterval: REFRESH_MS,
  });
}

export interface ToolTelemetry {
  tool: string;
  calls: ToolCall[];
  health: ToolHealth | null;
  problems: ToolProblem[];
}

export function useToolTelemetry(name: string | null) {
  return useQuery({
    queryKey: ["utos-telemetry", "tool", name],
    enabled: !!name,
    queryFn: async (): Promise<ToolTelemetry> => {
      const r = await getJSON<Record<string, unknown>>(
        `/telemetry/tool/${encodeURIComponent(name as string)}?limit=60`,
      );
      return {
        tool: String(r.tool ?? name),
        calls: Array.isArray(r.calls) ? (r.calls as ToolCall[]) : [],
        health: (r.health as ToolHealth) ?? null,
        problems: Array.isArray(r.problems) ? (r.problems as ToolProblem[]) : [],
      };
    },
    refetchInterval: REFRESH_MS,
  });
}

// --- tool spec (schema / examples / docs / config) -----------------------------

export interface JsonSchemaProp {
  type?: string | string[];
  description?: string;
  enum?: unknown[];
  default?: unknown;
  minimum?: number;
  maximum?: number;
  minLength?: number;
  maxLength?: number;
  pattern?: string;
  items?: JsonSchemaProp;
  properties?: Record<string, JsonSchemaProp>;
}

export interface JsonSchema {
  type?: string;
  required?: string[];
  properties?: Record<string, JsonSchemaProp>;
}

export interface ToolSpec {
  manifest: Record<string, unknown>;
  input_schema: JsonSchema | null;
  output_schema: JsonSchema | null;
  example_input: unknown;
  example_output: unknown;
  readme: string | null;
  docs: { api?: string | null; mcp?: string | null; cli?: string | null };
}

export function useToolSpec(name: string | null) {
  return useQuery({
    queryKey: ["utos-telemetry", "spec", name],
    enabled: !!name,
    staleTime: 5 * 60 * 1000, // spec 基本静态,5min 不重取
    queryFn: async (): Promise<ToolSpec | null> => {
      const r = await getJSON<{ spec?: ToolSpec }>(`/tool-spec/${encodeURIComponent(name as string)}`);
      return r.spec ?? null;
    },
  });
}

/** Relative time label, e.g. "12s 前". Pure, no deps. */
export function timeAgo(ts: number): string {
  const sec = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (sec < 60) return `${sec}s 前`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m 前`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}h 前`;
  return `${Math.floor(sec / 86400)}d 前`;
}

/** Whether a status string counts as a failure. */
export function isFailed(status: string): boolean {
  return status === "failed" || status === "error";
}
