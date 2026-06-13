// Shape of a single tool entry in registry-data.json. The JSON is imported
// with `resolveJsonModule`, but its inferred type is too loose (e.g. `status`
// widens to `string`), so we declare the contract explicitly and parse the
// raw import through it once in `data.ts`. Keeping the type here means the
// page/detail components depend on a stable shape, not on the JSON layout.

export interface ToolInterfaces {
  cli: string;
  mcp: string;
  api: string;
}

export interface Tool {
  name: string;
  /** English category key — NOT unique per Chinese label (two zh labels can share one key). */
  category: string;
  /** Chinese category label — the grouping key used across the UI. */
  category_zh: string;
  version: string;
  description: string;
  interfaces: ToolInterfaces;
  deps: string[];
  status: string;
  module_path: string;
}

export interface ToolRegistry {
  total: number;
  tools: Tool[];
}

/** A category section: its label, a stable DOM/anchor id, and its tools. */
export interface ToolCategory {
  /** Chinese label, e.g. "平台编排". */
  label: string;
  /** Stable slug derived from the label, used as section id + scroll anchor. */
  id: string;
  tools: Tool[];
}
