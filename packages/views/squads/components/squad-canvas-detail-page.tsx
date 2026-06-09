"use client";

import { useMemo, useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import type { Squad, SquadMember, Agent } from "@multica/core/types";
import { api } from "@multica/core/api";
import { useCurrentWorkspace, useWorkspacePaths } from "@multica/core/paths";
import { useWorkspaceId } from "@multica/core/hooks";
import { resolvePublicFileUrl } from "@multica/core/workspace/avatar-url";
import {
  agentListOptions,
  memberListOptions,
  squadListOptions,
  workspaceKeys,
} from "@multica/core/workspace/queries";
import { Users } from "lucide-react";
import { Skeleton } from "@multica/ui/components/ui/skeleton";
import { ActorAvatar as ActorAvatarBase } from "@multica/ui/components/common/actor-avatar";
import { ActorAvatar } from "../../common/actor-avatar";
import { AppLink, useNavigation } from "../../navigation";
import { BreadcrumbHeader } from "../../layout/breadcrumb-header";
import { useT, useTimeAgo } from "../../i18n";
import { SquadCanvasBoard } from "./squad-canvas-board";
import { SquadTaskBoard } from "./squad-task-board";
import { SquadCanvasTabStrip } from "./squad-canvas-tab-strip";

// Canvas orchestration page (PL-111 v2 + 增量). A canvas is a first-class entity,
// peer to a squad: each squad owns one canvas, reachable at /<ws>/canvas/<squadId>.
// The page mirrors the squad DETAIL surface — breadcrumb + left detail bar +
// right main panel — so it reads as native Multica chrome.
//
// A top 小队头像 tab strip (one tab per existing squad) lets you switch the WHOLE
// page between squads without navigating: picking a tab re-points ①小队详情 /
// ②成员列表 / ③小线任务看板 / ④ReactFlow 画布 at that squad. The active squad is
// seeded from the route id (the canvas card that was clicked) and can then be
// changed in-session via the strip. The left bar is split into two stacked blocks
// (上块 小队成员区 / 下块 小线任务看板); the right main panel is one full-bleed,
// manually-editable ReactFlow board with a bottom 画布编排工具栏. No backend canvas
// store yet, so the board seeds from the selected squad + its members and edits
// live in-session.

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
  // Route is /<ws>/canvas/<squadId> — the canvas is keyed by its squad. This is
  // only the INITIAL selection; the tab strip can re-point the page in-session.
  const routeSquadId = pathname.split("/").pop() ?? "";

  // All squads drive the avatar tab strip (one tab per existing squad).
  const { data: squads = [] } = useQuery({
    ...squadListOptions(wsId),
    enabled: !!workspace?.id,
  });

  // Explicit tab pick wins; otherwise fall back to the route squad if it's real,
  // otherwise the first squad. This keeps the deep-link from the list-page canvas
  // card working while still allowing in-session switching.
  const [pickedSquadId, setPickedSquadId] = useState<string | null>(null);
  const activeSquadId = useMemo(() => {
    if (pickedSquadId && squads.some((s) => s.id === pickedSquadId)) return pickedSquadId;
    if (squads.some((s) => s.id === routeSquadId)) return routeSquadId;
    return squads[0]?.id ?? routeSquadId;
  }, [pickedSquadId, squads, routeSquadId]);

  const squad = useMemo<Squad | undefined>(
    () => squads.find((s) => s.id === activeSquadId),
    [squads, activeSquadId],
  );

  const { data: members = [] } = useQuery<SquadMember[]>({
    queryKey: [...workspaceKeys.squads(wsId), activeSquadId, "members"],
    queryFn: () => api.listSquadMembers(activeSquadId),
    enabled: !!workspace?.id && !!activeSquadId,
  });

  const { data: agents = [] } = useQuery(agentListOptions(wsId));
  const { data: wsMembers = [] } = useQuery(memberListOptions(wsId));

  const getEntityName = (type: string, id: string) => {
    if (type === "agent") return agents.find((a: Agent) => a.id === id)?.name ?? id.slice(0, 8);
    return wsMembers.find((m) => m.user_id === id)?.name ?? id.slice(0, 8);
  };

  const memberRole = (type: string, id: string) => {
    if (type === "member") return wsMembers.find((m) => m.user_id === id)?.role ?? null;
    return null;
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

  return (
    <div className="flex flex-1 min-h-0 flex-col">
      <BreadcrumbHeader
        segments={[{ href: p.squads(), label: t(($) => $.canvas_page.breadcrumb_root) }]}
        leaf={
          <>
            <CanvasHeaderAvatar squad={squad} initials={initials} />
            <h1 className="truncate text-sm font-medium text-foreground">
              {t(($) => $.canvas_page.breadcrumb_leaf)}
            </h1>
          </>
        }
      />

      {/* Two-column grid mirrors squad-detail-page: left detail bar, right main
          panel. The main panel is one full-bleed, editable canvas box. */}
      <div className="flex flex-1 min-h-0 flex-col gap-3 overflow-y-auto p-3 md:grid md:grid-cols-[280px_minmax(0,1fr)] md:gap-4 md:overflow-hidden md:p-6 lg:grid-cols-[320px_minmax(0,1fr)]">
        <aside className="flex w-full flex-col rounded-lg border bg-background md:h-full md:min-h-0 md:overflow-hidden">
          {/* 小队头像 tab 排 — switches every block + the canvas to the picked squad */}
          {squads.length > 1 && (
            <SquadCanvasTabStrip
              squads={squads}
              activeSquadId={activeSquadId}
              onSelect={setPickedSquadId}
              label={t(($) => $.canvas_page.squad_switcher)}
            />
          )}

          {/* 上块 — 小队成员区 (identity + details + member list) */}
          <div className="flex min-h-0 flex-col md:flex-[3] md:overflow-y-auto">
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
                <p className="truncate text-sm font-semibold">{squad.name}</p>
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

            {/* Member list — same rows as the squad detail / profile card */}
            <div className="px-3 py-3">
              <div className="mb-1 px-2 text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
                {t(($) => $.members_tab.section_title)}
              </div>
              <div className="flex flex-col gap-0.5">
                {members.map((m) => {
                  const isLeader = m.member_type === "agent" && m.member_id === squad.leader_id;
                  const name = getEntityName(m.member_type, m.member_id);
                  const role = memberRole(m.member_type, m.member_id);
                  const href =
                    m.member_type === "agent" ? p.agentDetail(m.member_id) : p.memberDetail(m.member_id);
                  return (
                    <AppLink
                      key={`${m.member_type}-${m.member_id}`}
                      href={href}
                      className="flex min-w-0 items-center gap-2 rounded-md px-2 py-1.5 text-xs transition-colors hover:bg-accent/60"
                    >
                      <ActorAvatar
                        actorType={m.member_type}
                        actorId={m.member_id}
                        size={20}
                        showStatusDot={m.member_type === "agent"}
                        className="shrink-0"
                      />
                      <span className="min-w-0 flex-1 truncate font-medium">{name}</span>
                      {isLeader && (
                        <span className="max-w-[4rem] shrink-0 truncate rounded-md bg-amber-100 px-1 py-0.5 text-[10px] font-medium text-amber-700 dark:bg-amber-900/30 dark:text-amber-400">
                          {t(($) => $.members_tab.leader_chip)}
                        </span>
                      )}
                      {role && (
                        <span className="max-w-[3.5rem] shrink-0 truncate text-muted-foreground">{role}</span>
                      )}
                    </AppLink>
                  );
                })}
              </div>
            </div>
          </div>

          {/* 下块 — 小线任务看板 (issues board data for this squad / its members) */}
          <div className="flex min-h-0 flex-col border-t md:flex-[2] md:overflow-hidden">
            <SquadTaskBoard squadId={squad.id} memberIds={members.map((m) => m.member_id)} />
          </div>
        </aside>

        {/* Main — one big editable canvas box with bottom toolbar. */}
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
