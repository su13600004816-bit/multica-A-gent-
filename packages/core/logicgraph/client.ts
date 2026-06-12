// Thin client for the standalone logicgraph service (the canvas store + builder).
// Same-origin by default (`/tu`), so the multica app reaches it without a second
// login; auth is the page's. Keeping the transport here means the UI components
// stay dumb and the service can move without touching them.
import type { LogicGraph } from "./graph";

export type LogicGraphClientOptions = {
  baseUrl?: string;
  fetchImpl?: typeof fetch;
};

type RpcResult = { ok?: boolean; result?: { nodes?: unknown[]; edges?: unknown[]; groups?: unknown[] } };

export class LogicGraphClient {
  private base: string;
  private f: typeof fetch;

  constructor(opts: LogicGraphClientOptions = {}) {
    this.base = (opts.baseUrl ?? "/tu").replace(/\/+$/, "");
    this.f = opts.fetchImpl ?? fetch;
  }

  /** Names of all stored graphs (each is a saved canvas). */
  async listGraphs(): Promise<string[]> {
    try {
      const r = await this.f(`${this.base}/graphs`, { credentials: "include" });
      if (!r.ok) return [];
      const d = (await r.json()) as { graphs?: string[] };
      return Array.isArray(d.graphs) ? d.graphs : [];
    } catch {
      return [];
    }
  }

  /** Load one graph's nodes/edges/groups. */
  async getGraph(name: string): Promise<LogicGraph | null> {
    try {
      const r = await this.f(`${this.base}/rpc`, {
        method: "POST",
        credentials: "include",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ file: `${name}.json`, method: "list" }),
      });
      if (!r.ok) return null;
      const d = (await r.json()) as RpcResult;
      if (!d.ok || !d.result) return null;
      const res = d.result;
      type RawEdge = { from?: string; to?: string; type?: string; label?: string };
      return {
        nodes: (res.nodes ?? []) as LogicGraph["nodes"],
        edges: ((res.edges ?? []) as RawEdge[]).map((e) => ({
          from: e.from ?? "",
          to: e.to ?? "",
          type: e.type ?? "edge",
          label: e.label,
        })),
        groups: (res.groups ?? []) as LogicGraph["groups"],
      };
    } catch {
      return null;
    }
  }

  /**
   * Ask the service (LLM-backed) to build a graph from a prose description.
   * Returns the graph name to poll with getGraph(); resolves once queued.
   */
  async buildFromText(text: string, name?: string): Promise<{ name: string } | null> {
    try {
      const body = new URLSearchParams();
      body.set("text", text);
      if (name) body.set("name", name);
      const r = await this.f(`${this.base}/design`, {
        method: "POST",
        credentials: "include",
        headers: { accept: "application/json" },
        body,
      });
      if (!r.ok) return null;
      const ct = r.headers.get("content-type") ?? "";
      if (ct.includes("application/json")) {
        const d = (await r.json()) as { name?: string };
        return { name: d.name ?? name ?? "" };
      }
      return { name: name ?? "" };
    } catch {
      return null;
    }
  }

  /** Poll getGraph until it has nodes or the timeout elapses. */
  async waitForGraph(name: string, opts: { tries?: number; intervalMs?: number } = {}): Promise<LogicGraph | null> {
    const tries = opts.tries ?? 40;
    const interval = opts.intervalMs ?? 3000;
    for (let i = 0; i < tries; i++) {
      const g = await this.getGraph(name);
      if (g && g.nodes.length > 0) return g;
      await new Promise((res) => setTimeout(res, interval));
    }
    return null;
  }
}
