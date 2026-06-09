"use client";

import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { issueListOptions } from "@multica/core/issues";
import { BOARD_STATUSES, STATUS_CONFIG } from "@multica/core/issues/config";
import { useWorkspaceId } from "@multica/core/hooks";
import { useWorkspacePaths } from "@multica/core/paths";
import type { Issue, IssueStatus } from "@multica/core/types";
import { AppLink } from "../../navigation";
import { useT } from "../../i18n";

// 小线任务看板 — the lower-left block of the canvas orchestration page.
//
// Data source is the SAME board feed the issues board uses
// (`issueListOptions`, which fetches the board-status buckets). We then keep
// only the issues whose assignee is this squad or one of its members — the
// closest "该小队 / 线相关 issues" filter the existing data exposes (issues carry
// `assignee_id`; there is no separate line/team field). If nothing is assigned to
// the squad or its members the section simply shows the empty state — no fake
// data is synthesized.

// i18n status labels, keyed off our canvas_page strings so the board reads in
// the active locale (STATUS_CONFIG.label is English-only).
const STATUS_LABEL_KEY: Record<IssueStatus, "status_backlog" | "status_todo" | "status_in_progress" | "status_in_review" | "status_done" | "status_blocked" | "status_cancelled"> = {
  backlog: "status_backlog",
  todo: "status_todo",
  in_progress: "status_in_progress",
  in_review: "status_in_review",
  done: "status_done",
  blocked: "status_blocked",
  cancelled: "status_cancelled",
};

export function SquadTaskBoard({
  squadId,
  memberIds,
}: {
  squadId: string;
  memberIds: string[];
}) {
  const { t } = useT("squads");
  const wsId = useWorkspaceId();
  const p = useWorkspacePaths();

  const { data: allIssues = [], isLoading } = useQuery({
    ...issueListOptions(wsId),
    enabled: !!wsId,
  });

  const idSet = useMemo(() => new Set([squadId, ...memberIds]), [squadId, memberIds]);

  const grouped = useMemo(() => {
    const relevant = allIssues.filter(
      (i: Issue) => i.assignee_id != null && idSet.has(i.assignee_id),
    );
    const buckets = new Map<IssueStatus, Issue[]>();
    for (const status of BOARD_STATUSES) buckets.set(status, []);
    for (const issue of relevant) {
      const bucket = buckets.get(issue.status);
      if (bucket) bucket.push(issue);
    }
    return buckets;
  }, [allIssues, idSet]);

  const total = useMemo(
    () => Array.from(grouped.values()).reduce((sum, list) => sum + list.length, 0),
    [grouped],
  );

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex shrink-0 items-center justify-between px-5 pb-2 pt-4">
        <div className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
          {t(($) => $.canvas_page.board_section)}
        </div>
        {total > 0 && (
          <span className="font-mono text-[10px] tabular-nums text-muted-foreground/70">{total}</span>
        )}
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-3 pb-3">
        {isLoading ? (
          <div className="space-y-2 px-2">
            {Array.from({ length: 3 }).map((_, i) => (
              <div key={i} className="h-9 animate-pulse rounded-md bg-muted/60" />
            ))}
          </div>
        ) : total === 0 ? (
          <p className="px-2 py-6 text-center text-xs text-muted-foreground">
            {t(($) => $.canvas_page.board_empty)}
          </p>
        ) : (
          <div className="space-y-3">
            {BOARD_STATUSES.map((status) => {
              const list = grouped.get(status) ?? [];
              if (list.length === 0) return null;
              return (
                <div key={status}>
                  <div className="flex items-center gap-1.5 px-2 py-1">
                    <span className={`h-1.5 w-1.5 rounded-full ${STATUS_CONFIG[status].dividerColor}`} />
                    <span className="text-xs font-medium">{t(($) => $.canvas_page[STATUS_LABEL_KEY[status]])}</span>
                    <span className="font-mono text-[10px] tabular-nums text-muted-foreground/60">{list.length}</span>
                  </div>
                  <div className="space-y-1">
                    {list.map((issue) => (
                      <AppLink
                        key={issue.id}
                        href={p.issueDetail(issue.id)}
                        className="flex items-center gap-2 rounded-md px-2 py-1.5 transition-colors hover:bg-accent/60"
                      >
                        <span className="shrink-0 font-mono text-[10px] tabular-nums text-muted-foreground">
                          {issue.identifier}
                        </span>
                        <span className="min-w-0 flex-1 truncate text-xs">{issue.title}</span>
                      </AppLink>
                    ))}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
