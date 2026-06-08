"use client";

import type { ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import type { Squad, SquadMember, Agent } from "@multica/core/types";
import { api } from "@multica/core/api";
import { useCurrentWorkspace, useWorkspacePaths } from "@multica/core/paths";
import { useWorkspaceId } from "@multica/core/hooks";
import { resolvePublicFileUrl } from "@multica/core/workspace/avatar-url";
import { agentListOptions, memberListOptions, workspaceKeys } from "@multica/core/workspace/queries";
import { Users } from "lucide-react";
import { Skeleton } from "@multica/ui/components/ui/skeleton";
import { ActorAvatar as ActorAvatarBase } from "@multica/ui/components/common/actor-avatar";
import { ActorAvatar } from "../../common/actor-avatar";
import { useNavigation } from "../../navigation";
import { BreadcrumbHeader } from "../../layout/breadcrumb-header";
import { useT, useTimeAgo } from "../../i18n";
import { SquadCanvasBoard } from "./squad-canvas-board";

// Independent canvas detail page (finalized PL-89 spec). A canvas is a
// first-class entity, peer to a squad: each squad owns one canvas, reachable
// at /<ws>/canvas/<squadId>. The page deliberately mirrors the squad DETAIL
// page — breadcrumb + left read-only inspector (Leader / Members / Created /
// Updated) + right main panel — so it reads as the same Multica surface. The
// ONLY structural difference vs squad detail: the main panel is one full-bleed
// ReactFlow board instead of the Members/Instructions tabs. No backend canvas
// store yet, so the board is rendered from the squad + its members.

function InspectorRow({ label, children }: { label: string; children: ReactNode }) {
  return (
    <>
      <div className="px-2 py-1 text-xs text-muted-foreground">{label}</div>
      <div className="min-w-0 px-2 py-1 text-xs">{children}</div>
    </>
  );
}

// 16px avatar in the breadcrumb strip; falls back to the Users glyph — mirrors
// SquadHeaderAvatar in squad-detail-page.
function CanvasHeaderAvatar({ squad, initials }: { squad: Squad; initials: string }) {
  if (!squad.avatar_url) {
    return <Users className="h-4 w-4 text-muted-foreground" />;
  }
  return (
    <ActorAvatarBase
      name={squad.name}
      initials={initials}
      avatarUrl={resolvePublicFileUrl(squad.avatar_url)}
      isSquad
      size={16}
      className="rounded"
    />
  );
}

// Initial-load skeleton — mirrors the two-column layout so the swap doesn't
// shift the page.
function CanvasDetailSkeleton() {
  return (
    <div className="flex flex-1 min-h-0 flex-col">
      <div className="flex h-11 shrink-0 items-center gap-2 border-b px-4">
        <Skeleton className="h-4 w-44" />
      </div>
      <div className="flex flex-1 min-h-0 flex-col gap-3 overflow-y-auto p-3 md:grid md:grid-cols-[280px_minmax(0,1fr)] md:gap-4 md:overflow-hidden md:p-6 lg:grid-cols-[320px_minmax(0,1fr)]">
        <div className="flex flex-col gap-4 rounded-lg border p-5">
          <Skeleton className="h-16 w-16 rounded-lg" />
          <Skeleton className="h-5 w-40" />
          <div className="space-y-2">
            <Skeleton className="h-3 w-3/4" />
            <Skeleton className="h-3 w-2/3" />
          </div>
        </div>
        <Skeleton className="min-h-[60vh] w-full rounded-lg" />
      </div>
    </div>
  );
}

export function SquadCanvasDetailPage() {
  const { t } = useT("squads");
  const timeAgo = useTimeAgo();
  const workspace = useCurrentWorkspace();
  const wsId = useWorkspaceId();
  const p = useWorkspacePaths();
  const { pathname } = useNavigation();
  // Route is /<ws>/canvas/<squadId> — the canvas is keyed by its squad.
  const squadId = pathname.split("/").pop() ?? "";

  const { data: squad } = useQuery<Squad>({
    queryKey: [...workspaceKeys.squads(wsId), squadId],
    queryFn: () => api.getSquad(squadId),
    enabled: !!workspace?.id && !!squadId,
  });

  const { data: members = [] } = useQuery<SquadMember[]>({
    queryKey: [...workspaceKeys.squads(wsId), squadId, "members"],
    queryFn: () => api.listSquadMembers(squadId),
    enabled: !!workspace?.id && !!squadId,
  });

  const { data: agents = [] } = useQuery(agentListOptions(wsId));
  const { data: wsMembers = [] } = useQuery(memberListOptions(wsId));

  const getEntityName = (type: string, id: string) => {
    if (type === "agent") return agents.find((a: Agent) => a.id === id)?.name ?? id.slice(0, 8);
    return wsMembers.find((m) => m.user_id === id)?.name ?? id.slice(0, 8);
  };

  if (!squad) {
    return <CanvasDetailSkeleton />;
  }

  const initials = squad.name
    .split(" ")
    .map((w) => w[0])
    .join("")
    .toUpperCase()
    .slice(0, 2);
  const boardTitle = t(($) => $.canvas_tab.board_title, { name: squad.name });

  return (
    <div className="flex flex-1 min-h-0 flex-col">
      <BreadcrumbHeader
        segments={[
          { href: p.squads(), label: t(($) => $.page.title) },
          { href: p.squadDetail(squad.id), label: squad.name },
        ]}
        leaf={
          <>
            <CanvasHeaderAvatar squad={squad} initials={initials} />
            <h1 className="truncate text-sm font-medium text-foreground">{boardTitle}</h1>
          </>
        }
      />

      {/* Two-column grid mirrors squad-detail-page: left read-only inspector,
          right main panel. Here the main panel is one full-bleed canvas box. */}
      <div className="flex flex-1 min-h-0 flex-col gap-3 overflow-y-auto p-3 md:grid md:grid-cols-[280px_minmax(0,1fr)] md:gap-4 md:overflow-hidden md:p-6 lg:grid-cols-[320px_minmax(0,1fr)]">
        <aside className="flex w-full flex-col rounded-lg border bg-background md:h-full md:min-h-0 md:overflow-y-auto">
          {/* Identity */}
          <div className="flex flex-col gap-3 border-b px-5 pb-5 pt-5">
            <div className="flex h-16 w-16 shrink-0 items-center justify-center overflow-hidden rounded-lg bg-muted">
              {squad.avatar_url ? (
                <ActorAvatarBase
                  name={squad.name}
                  initials={initials}
                  avatarUrl={resolvePublicFileUrl(squad.avatar_url)}
                  isSquad
                  size={64}
                  className="rounded-none"
                />
              ) : (
                <Users className="h-7 w-7 text-muted-foreground" />
              )}
            </div>
            <div className="flex flex-col gap-1">
              <p className="truncate text-sm font-semibold">{boardTitle}</p>
              {squad.description && (
                <p className="line-clamp-3 text-xs text-muted-foreground">{squad.description}</p>
              )}
            </div>
          </div>

          {/* Details — read-only, same rows as the squad inspector */}
          <div className="border-b px-5 py-4">
            <div className="mb-1 -mx-2 px-2 text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
              {t(($) => $.inspector.details_section)}
            </div>
            <div className="grid grid-cols-[auto_1fr] gap-x-2 gap-y-0.5">
              <InspectorRow label="Leader">
                <span className="flex min-w-0 items-center gap-1.5">
                  <ActorAvatar actorType="agent" actorId={squad.leader_id} size={14} />
                  <span className="truncate">{getEntityName("agent", squad.leader_id)}</span>
                </span>
              </InspectorRow>
              <InspectorRow label="Members">
                <span className="text-muted-foreground tabular-nums">{members.length}</span>
              </InspectorRow>
              <InspectorRow label="Created">
                <span className="text-muted-foreground">{timeAgo(squad.created_at)}</span>
              </InspectorRow>
              <InspectorRow label="Updated">
                <span className="text-muted-foreground">{timeAgo(squad.updated_at)}</span>
              </InspectorRow>
            </div>
          </div>
        </aside>

        {/* Main — one big canvas box (the "1 个大框" from the spec). */}
        <div className="flex min-h-[60vh] flex-col overflow-hidden rounded-lg border bg-background md:h-full md:min-h-0">
          <div className="h-full p-3 md:p-4">
            <SquadCanvasBoard
              squadName={squad.name}
              squadAvatarUrl={squad.avatar_url}
              leaderId={squad.leader_id}
              members={members}
              getEntityName={getEntityName}
            />
          </div>
        </div>
      </div>
    </div>
  );
}
