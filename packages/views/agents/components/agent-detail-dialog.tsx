"use client";

import { AlertCircle, Lock } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogTitle,
} from "@multica/ui/components/ui/dialog";
import { Skeleton } from "@multica/ui/components/ui/skeleton";
import { ActorAvatar } from "../../common/actor-avatar";
import { availabilityConfig } from "../presence";
import { AgentDetailInspector } from "./agent-detail-inspector";
import { AgentOverviewPane } from "./agent-overview-pane";
import { useAgentDetail } from "./use-agent-detail";
import { useT } from "../../i18n";

interface AgentDetailDialogProps {
  /** Agent to show. When null the dialog mounts no body (and fetches nothing). */
  agentId: string | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

/**
 * Agent detail surface in a modal frame — opened by clicking an agent node on
 * the squad canvas (PL-120). This is deliberately *the agent detail page*, not
 * a bespoke popup: it consumes the same `useAgentDetail` data hook and renders
 * the same `AgentDetailInspector` (left) + `AgentOverviewPane` (right) in the
 * same `[320px_minmax(0,1fr)]` grid, so identity / properties / Skills / the
 * Tabs (动态 / Tasks / 指令 / Skills / 环境变量 / 自定义参数 / MCP) / 近30天 stats /
 * 最近工作 list all match the standalone page byte-for-byte. No invented skin —
 * every token reads as native Multica chrome.
 *
 * The body is mounted only while `agentId` is set, so a closed dialog runs no
 * queries and the hook starts fresh each open.
 */
export function AgentDetailDialog({
  agentId,
  open,
  onOpenChange,
}: AgentDetailDialogProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className="flex h-[86vh] max-h-[860px] w-[94vw] max-w-[1120px] flex-col gap-0 overflow-hidden p-0 sm:max-w-[1120px]"
      >
        {agentId ? (
          <AgentDetailDialogBody agentId={agentId} />
        ) : (
          <DialogTitle className="sr-only">Agent detail</DialogTitle>
        )}
      </DialogContent>
    </Dialog>
  );
}

function AgentDetailDialogBody({ agentId }: { agentId: string }) {
  const { t } = useT("agents");
  const {
    agent,
    agentsLoading,
    isForbidden,
    runtimes,
    members,
    presence,
    runtime,
    owner,
    currentUserId,
    canEdit,
    handleUpdate,
  } = useAgentDetail(agentId);

  // --- Loading ---
  if (agentsLoading && !agent) {
    return (
      <>
        <DialogTitle className="sr-only">{t(($) => $.page.title)}</DialogTitle>
        <div className="flex shrink-0 items-center gap-2.5 border-b px-5 py-3.5">
          <Skeleton className="h-9 w-9 rounded-lg" />
          <Skeleton className="h-4 w-40" />
        </div>
        <div className="grid flex-1 min-h-0 grid-cols-[320px_minmax(0,1fr)] gap-4 overflow-hidden p-4">
          <div className="flex flex-col gap-4 rounded-lg border p-5">
            <Skeleton className="h-14 w-14 rounded-lg" />
            <Skeleton className="h-5 w-40" />
            <Skeleton className="h-3 w-full" />
            <div className="space-y-2">
              <Skeleton className="h-3 w-3/4" />
              <Skeleton className="h-3 w-2/3" />
              <Skeleton className="h-3 w-1/2" />
            </div>
          </div>
          <div className="flex flex-col gap-4 rounded-lg border p-6">
            <Skeleton className="h-6 w-64" />
            <Skeleton className="h-4 w-full" />
            <Skeleton className="h-4 w-5/6" />
            <Skeleton className="h-4 w-4/6" />
          </div>
        </div>
      </>
    );
  }

  // --- No permission / not found ---
  if (!agent) {
    return (
      <>
        <DialogTitle className="sr-only">{t(($) => $.page.title)}</DialogTitle>
        <div className="flex flex-1 flex-col items-center justify-center gap-3 px-6 py-16 text-center">
          {isForbidden ? (
            <>
              <Lock className="h-8 w-8 text-muted-foreground" />
              <div>
                <p className="text-sm font-medium">{t(($) => $.detail.no_access_title)}</p>
                <p className="mt-1 text-xs text-muted-foreground">
                  {t(($) => $.detail.no_access_hint)}
                </p>
              </div>
            </>
          ) : (
            <>
              <AlertCircle className="h-8 w-8 text-destructive" />
              <div>
                <p className="text-sm font-medium">{t(($) => $.detail.not_found_title)}</p>
                <p className="mt-1 text-xs text-muted-foreground">
                  {t(($) => $.detail.not_found_default)}
                </p>
              </div>
            </>
          )}
        </div>
      </>
    );
  }

  const isArchived = !!agent.archived_at;
  const av = presence
    ? {
        ...availabilityConfig[presence.availability],
        label: t(($) => $.availability[presence.availability]),
      }
    : null;

  return (
    <>
      {/* Header — avatar + name + presence, mirroring the detail page leaf. The
          dialog's own close button sits top-right, so content stays left. */}
      <div className="flex shrink-0 items-center gap-2.5 border-b px-5 py-3.5 pr-12">
        <div className="h-9 w-9 shrink-0 overflow-hidden rounded-lg">
          <ActorAvatar
            actorType="agent"
            actorId={agent.id}
            size={36}
            className="rounded-none"
          />
        </div>
        <DialogTitle className="min-w-0 truncate text-sm font-medium text-foreground">
          {agent.name}
        </DialogTitle>
        {!isArchived && av && presence && (
          <span
            className={`inline-flex shrink-0 items-center gap-1.5 rounded-md border px-1.5 py-0.5 text-xs ${av.textClass}`}
          >
            <span className={`h-1.5 w-1.5 rounded-full ${av.dotClass}`} />
            {av.label}
          </span>
        )}
      </div>

      {isArchived && (
        <div className="flex shrink-0 items-center gap-2 border-b bg-muted/50 px-5 py-2 text-xs text-muted-foreground">
          <AlertCircle className="h-3.5 w-3.5 shrink-0" />
          <span className="flex-1">{t(($) => $.detail.archived_banner)}</span>
        </div>
      )}

      {/* Body — same two-column grid as the standalone agent detail page. */}
      <div className="flex flex-1 min-h-0 flex-col gap-3 overflow-y-auto p-3 md:grid md:grid-cols-[320px_minmax(0,1fr)] md:gap-4 md:overflow-hidden md:p-4">
        <AgentDetailInspector
          agent={agent}
          runtime={runtime}
          owner={owner}
          presence={presence}
          runtimes={runtimes}
          members={members}
          currentUserId={currentUserId}
          canEdit={canEdit.allowed}
          onUpdate={handleUpdate}
        />

        <AgentOverviewPane
          agent={agent}
          runtimes={runtimes}
          onUpdate={handleUpdate}
        />
      </div>
    </>
  );
}
