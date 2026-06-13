"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { Search } from "lucide-react";
import { cn } from "@multica/ui/lib/utils";
import { Badge } from "@multica/ui/components/ui/badge";
import {
  Card,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@multica/ui/components/ui/card";
import { Input } from "@multica/ui/components/ui/input";
import { builtCount, categories, total } from "./data";
import { ToolDetailSheet } from "./tool-detail";
import type { Tool, ToolCategory } from "./types";

/** A category section after applying the search filter — empty groups drop out. */
interface FilteredCategory extends ToolCategory {
  /** Index in the unfiltered `categories` array, used for stable scroll targets. */
  sourceIndex: number;
}

export function ToolsPage() {
  const [query, setQuery] = useState("");
  const [activeId, setActiveId] = useState<string | null>(categories[0]?.id ?? null);
  const [selected, setSelected] = useState<string | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  // True while a click-to-scroll animation is in flight — suppresses the
  // IntersectionObserver from fighting the programmatic scroll (which would
  // flicker the active highlight between the source and target sections).
  const isProgrammaticScroll = useRef(false);

  const filtered: FilteredCategory[] = useMemo(() => {
    const q = query.trim().toLowerCase();
    return categories
      .map((cat, sourceIndex): FilteredCategory => {
        if (!q) return { ...cat, sourceIndex };
        const tools = cat.tools.filter(
          (t) =>
            t.name.toLowerCase().includes(q) ||
            t.description.toLowerCase().includes(q),
        );
        return { ...cat, tools, sourceIndex };
      })
      .filter((cat) => cat.tools.length > 0);
  }, [query]);

  const visibleCount = useMemo(
    () => filtered.reduce((sum, cat) => sum + cat.tools.length, 0),
    [filtered],
  );

  // Scroll-spy: highlight the category whose section is nearest the top of the
  // scroll container. Re-registers whenever the filtered set changes (sections
  // mount/unmount on search).
  useEffect(() => {
    const root = scrollRef.current;
    if (!root) return;
    const sections = filtered
      .map((cat) => root.querySelector<HTMLElement>(`#${CSS.escape(cat.id)}`))
      .filter((el): el is HTMLElement => el !== null);
    if (sections.length === 0) return;

    const observer = new IntersectionObserver(
      (entries) => {
        if (isProgrammaticScroll.current) return;
        const visible = entries
          .filter((e) => e.isIntersecting)
          .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top);
        if (visible[0]) setActiveId(visible[0].target.id);
      },
      // Bias the observation band toward the top so the "current" section is the
      // one the user is reading, not whatever last scrolled into the bottom.
      { root, rootMargin: "0px 0px -70% 0px", threshold: 0 },
    );
    sections.forEach((el) => observer.observe(el));
    return () => observer.disconnect();
  }, [filtered]);

  const scrollToCategory = (id: string) => {
    const root = scrollRef.current;
    const target = root?.querySelector<HTMLElement>(`#${CSS.escape(id)}`);
    if (!target) return;
    setActiveId(id);
    isProgrammaticScroll.current = true;
    target.scrollIntoView({ behavior: "smooth", block: "start" });
    window.setTimeout(() => {
      isProgrammaticScroll.current = false;
    }, 600);
  };

  return (
    <div className="flex flex-1 min-h-0 flex-col">
      {/* Header */}
      <div className="flex flex-col gap-3 border-b px-4 py-3 sm:flex-row sm:items-center sm:gap-4">
        <div className="flex items-center gap-2">
          <h1 className="truncate text-sm font-medium text-foreground">工具库</h1>
          <Badge variant="secondary" className="font-mono">{total}</Badge>
          <Badge variant="outline" className="font-mono">已建 {builtCount}</Badge>
          {query.trim() && (
            <Badge variant="outline" className="font-mono">匹配 {visibleCount}</Badge>
          )}
        </div>
        <div className="relative sm:ml-auto sm:w-72">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="搜索工具名 / 描述"
            className="h-8 pl-8"
          />
        </div>
      </div>

      {/* Body: left category rail + right grouped grid */}
      <div className="flex min-h-0 flex-1 flex-col md:flex-row">
        {/* Category rail — horizontal scroll strip on narrow/foldable, sticky
            sidebar on md+. */}
        <nav
          aria-label="工具类目"
          className="shrink-0 border-b md:w-48 md:border-b-0 md:border-r"
        >
          <div className="flex gap-1 overflow-x-auto p-2 md:sticky md:top-0 md:flex-col md:overflow-x-visible md:p-3">
            {categories.map((cat) => {
              const isActive = cat.id === activeId;
              return (
                <button
                  key={cat.id}
                  type="button"
                  onClick={() => scrollToCategory(cat.id)}
                  className={cn(
                    "flex shrink-0 items-center gap-1.5 rounded-md px-2.5 py-1.5 text-left text-xs transition-colors md:w-full",
                    isActive
                      ? "bg-accent text-accent-foreground"
                      : "text-muted-foreground hover:bg-accent/60 hover:text-foreground",
                  )}
                >
                  <span className="min-w-0 flex-1 truncate">{cat.label}</span>
                  <span className="shrink-0 font-mono text-[10px] tabular-nums text-muted-foreground">
                    {cat.tools.length}
                  </span>
                </button>
              );
            })}
          </div>
        </nav>

        {/* Right content */}
        <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto scroll-smooth">
          {filtered.length === 0 ? (
            <div className="flex h-full items-center justify-center p-10 text-sm text-muted-foreground">
              未找到匹配「{query}」的工具
            </div>
          ) : (
            <div className="flex flex-col gap-8 p-4 md:p-6">
              {filtered.map((cat) => (
                <section key={cat.id} id={cat.id} className="scroll-mt-4">
                  <div className="mb-3 flex items-center gap-2">
                    <h2 className="font-heading text-sm font-medium text-foreground">
                      {cat.label}
                    </h2>
                    <span className="font-mono text-xs tabular-nums text-muted-foreground">
                      {cat.tools.length}
                    </span>
                  </div>
                  <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
                    {cat.tools.map((tool) => (
                      <ToolCard
                        key={tool.name}
                        tool={tool}
                        onSelect={() => setSelected(tool.name)}
                      />
                    ))}
                  </div>
                </section>
              ))}
              {/* Tail spacer so the last section can scroll to the top of the
                  viewport and win scroll-spy. */}
              <div aria-hidden className="h-[40vh] shrink-0" />
            </div>
          )}
        </div>
      </div>

      <ToolDetailSheet
        toolName={selected}
        onOpenChange={(open) => {
          if (!open) setSelected(null);
        }}
      />
    </div>
  );
}

function ToolCard({ tool, onSelect }: { tool: Tool; onSelect: () => void }) {
  const isBuilt = tool.status === "built";
  return (
    <Card
      size="sm"
      role="button"
      tabIndex={0}
      onClick={onSelect}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onSelect();
        }
      }}
      className="cursor-pointer transition-colors hover:bg-accent/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
    >
      <CardHeader>
        <CardTitle className="truncate font-mono text-sm">{tool.name}</CardTitle>
        <CardDescription className="line-clamp-2 min-h-8">
          {tool.description}
        </CardDescription>
      </CardHeader>
      <div className="flex flex-wrap items-center gap-1.5 px-3">
        <Badge variant="outline">{tool.category_zh}</Badge>
        <span className="font-mono text-[10px] text-muted-foreground">v{tool.version}</span>
        <Badge
          variant={isBuilt ? "default" : "secondary"}
          className="ml-auto font-mono"
        >
          {isBuilt ? "已建成" : tool.status === "planned" ? "规划中" : tool.status}
        </Badge>
      </div>
    </Card>
  );
}
