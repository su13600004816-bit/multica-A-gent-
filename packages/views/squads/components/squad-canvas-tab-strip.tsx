"use client";

import type { Squad } from "@multica/core/types";
import { resolvePublicFileUrl } from "@multica/core/workspace/avatar-url";
import { ActorAvatar as ActorAvatarBase } from "@multica/ui/components/common/actor-avatar";
import { Users } from "lucide-react";

// 小队头像 tab 排 (PL-111 增量). The top strip of the canvas orchestration page:
// one avatar tab per existing squad, sourced straight from the squads list — no
// invented entries. Clicking a tab switches the WHOLE page (detail / member list
// / task board / ReactFlow) to that squad. Same rounded-lg border / bg-accent /
// muted-foreground tokens as the squad pages, so it reads as native Multica
// chrome rather than a foreign canvas skin.

function initialsOf(name: string): string {
  return name
    .split(" ")
    .map((w) => w[0])
    .join("")
    .toUpperCase()
    .slice(0, 2);
}

export function SquadCanvasTabStrip({
  squads,
  activeSquadId,
  onSelect,
  label,
}: {
  squads: Squad[];
  activeSquadId: string;
  onSelect: (squadId: string) => void;
  label: string;
}) {
  return (
    <div className="shrink-0 border-b px-3 py-2.5">
      <div className="mb-1.5 px-1 text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
        {label}
      </div>
      <div className="flex gap-1.5 overflow-x-auto pb-1">
        {squads.map((squad) => {
          const active = squad.id === activeSquadId;
          const initials = initialsOf(squad.name);
          return (
            <button
              key={squad.id}
              type="button"
              onClick={() => onSelect(squad.id)}
              title={squad.name}
              aria-pressed={active}
              className={`flex w-[68px] shrink-0 flex-col items-center gap-1 rounded-lg border px-1.5 py-2 text-center transition-colors ${
                active
                  ? "border-foreground/25 bg-accent"
                  : "border-transparent hover:bg-accent/60"
              }`}
            >
              <span className="flex h-9 w-9 items-center justify-center overflow-hidden rounded-md bg-muted">
                {squad.avatar_url ? (
                  <ActorAvatarBase
                    name={squad.name}
                    initials={initials}
                    avatarUrl={resolvePublicFileUrl(squad.avatar_url)}
                    isSquad
                    size={36}
                    className="rounded-none"
                  />
                ) : (
                  <Users className="h-4 w-4 text-muted-foreground" />
                )}
              </span>
              <span
                className={`w-full truncate text-[10px] leading-tight ${
                  active ? "font-medium text-foreground" : "text-muted-foreground"
                }`}
              >
                {squad.name}
              </span>
            </button>
          );
        })}
      </div>
    </div>
  );
}
