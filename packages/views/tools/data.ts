import registry from "./registry-data.json";
import type { Tool, ToolCategory, ToolRegistry } from "./types";

// The imported JSON is structurally compatible with ToolRegistry but TS infers
// a wider type for it (string literals widen, `status` becomes string). One
// localized assertion here keeps the rest of the module fully typed without
// scattering casts through the components.
const REGISTRY = registry as ToolRegistry;

export const tools: Tool[] = REGISTRY.tools;
export const total: number = REGISTRY.total;

/** Count of tools whose status is "built" — surfaced as a Badge in the header. */
export const builtCount: number = tools.filter((t) => t.status === "built").length;

/**
 * Stable, URL/DOM-safe slug for a category id. Category labels are Chinese, so
 * we can't use them directly as element ids / scroll anchors. We derive an id
 * from the English `category` key plus the label, because the key alone is NOT
 * unique (two zh labels share `I_interface_exposure`). Appending an index makes
 * each section deterministic and collision-free.
 */
function categoryId(key: string, index: number): string {
  const base = key.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
  return `tool-cat-${base}-${index}`;
}

/**
 * Tools grouped by their Chinese category label, preserving first-seen order so
 * the left rail and the right content sections stay in the same sequence.
 */
export const categories: ToolCategory[] = (() => {
  const order: string[] = [];
  const byLabel = new Map<string, { key: string; tools: Tool[] }>();
  for (const tool of tools) {
    const existing = byLabel.get(tool.category_zh);
    if (existing) {
      existing.tools.push(tool);
    } else {
      order.push(tool.category_zh);
      byLabel.set(tool.category_zh, { key: tool.category, tools: [tool] });
    }
  }
  return order.map((label, index) => {
    const group = byLabel.get(label)!;
    return { label, id: categoryId(group.key, index), tools: group.tools };
  });
})();

/** Find a single tool by its unique name (used by the standalone detail route). */
export function findTool(name: string): Tool | undefined {
  return tools.find((t) => t.name === name);
}
