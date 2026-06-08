"use client";

import { useMemo } from "react";
import {
  ReactFlow,
  Background,
  BackgroundVariant,
  Controls,
  Panel,
  Handle,
  Position,
  type Node,
  type Edge,
  type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useTheme } from "@multica/ui/components/common/theme-provider";
import { ActorAvatar as ActorAvatarBase } from "@multica/ui/components/common/actor-avatar";
import { ActorAvatar } from "../../common/actor-avatar";
import { useT } from "../../i18n";
import type { SquadMember } from "@multica/core/types";

// Squad-scoped orchestration canvas. Each squad gets its own board; the squad
// sits at the root and its members fan out as nodes, so the canvas is a live
// picture of "this squad".
//
// Visual baseline = the Multica 小队 pages (squads-page / squad-detail-page /
// squad-profile-card): same card rounding (rounded-lg), border / bg-background /
// text-muted-foreground tokens, the same ActorAvatar usage and the SAME text-only
// leader chip as SquadProfileCard. We deliberately do NOT invent any canvas skin —
// every node / panel / control reads as native Multica chrome, identical to the
// squad pages it lives next to.

type RootNodeData = {
  name: string;
  initials: string;
  avatarUrl: string | null;
  subtitle: string;
};
type MemberNodeData = {
  name: string;
  memberType: SquadMember["member_type"];
  memberId: string;
  role: string;
  isLeader: boolean;
  leaderLabel: string;
};

type SquadRootNode = Node<RootNodeData, "squadRoot">;
type SquadMemberNode = Node<MemberNodeData, "squadMember">;
type SquadFlowNode = SquadRootNode | SquadMemberNode;

const handleClass = "!h-2 !w-2 !border !border-border !bg-muted-foreground/40";

// Leader chip — byte-for-byte the same chip SquadProfileCard renders (text-only,
// no icon), so a leader reads identically on the canvas and in the squad card.
const leaderChipClass =
  "shrink-0 rounded-md bg-amber-100 px-1 py-0.5 text-[10px] font-medium text-amber-700 dark:bg-amber-900/30 dark:text-amber-400";

// Root node — the squad itself, composed exactly like the SquadProfileCard header
// (square-rounded squad avatar + name in text-sm font-semibold).
function SquadRootNodeView({ data }: NodeProps<SquadRootNode>) {
  return (
    <div className="w-[216px] rounded-lg border bg-background px-4 py-3 shadow-sm">
      <div className="flex items-center gap-3">
        <ActorAvatarBase
          name={data.name}
          initials={data.initials}
          avatarUrl={data.avatarUrl ?? undefined}
          isSquad
          size={36}
          className="shrink-0 rounded-md"
        />
        <div className="min-w-0">
          <div className="truncate text-sm font-semibold">{data.name}</div>
          <div className="truncate text-xs text-muted-foreground">{data.subtitle}</div>
        </div>
      </div>
      <Handle type="source" position={Position.Right} className={handleClass} />
    </div>
  );
}

// Member node — one agent / human, composed exactly like a SquadProfileCard
// member row (ActorAvatar + name in text-sm font-medium + muted role).
function SquadMemberNodeView({ data }: NodeProps<SquadMemberNode>) {
  return (
    <div className="w-[200px] rounded-lg border bg-background px-3 py-2.5 shadow-sm">
      <Handle type="target" position={Position.Left} className={handleClass} />
      <div className="flex items-center gap-2.5">
        <ActorAvatar
          actorType={data.memberType}
          actorId={data.memberId}
          size={28}
          showStatusDot={data.memberType === "agent"}
          className="shrink-0"
        />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5">
            <span className="min-w-0 flex-1 truncate text-sm font-medium">{data.name}</span>
            {data.isLeader && <span className={leaderChipClass}>{data.leaderLabel}</span>}
          </div>
          <div className="truncate text-xs capitalize text-muted-foreground">
            {data.role || data.memberType}
          </div>
        </div>
      </div>
    </div>
  );
}

const nodeTypes = {
  squadRoot: SquadRootNodeView,
  squadMember: SquadMemberNodeView,
};

// Vertical rhythm for the member column.
const MEMBER_GAP_Y = 84;
const COLUMN_X = 320;
const ROOT_X = 0;

function initialsOf(name: string): string {
  return name
    .split(" ")
    .map((w) => w[0])
    .join("")
    .toUpperCase()
    .slice(0, 2);
}

export function SquadCanvasBoard({
  squadName,
  squadAvatarUrl,
  leaderId,
  members,
  getEntityName,
}: {
  squadName: string;
  squadAvatarUrl?: string | null;
  leaderId: string;
  members: SquadMember[];
  getEntityName: (type: string, id: string) => string;
}) {
  const { t } = useT("squads");
  const { resolvedTheme } = useTheme();
  const colorMode = resolvedTheme === "dark" ? "dark" : "light";

  const initials = initialsOf(squadName);
  const subtitle = t(($) => $.canvas_tab.root_subtitle, { count: members.length });

  const nodes = useMemo<SquadFlowNode[]>(() => {
    // Center the root vertically against the member column so edges stay tidy.
    const span = Math.max(0, members.length - 1) * MEMBER_GAP_Y;
    const rootY = span / 2;

    const root: SquadRootNode = {
      id: "squad-root",
      type: "squadRoot",
      position: { x: ROOT_X, y: rootY },
      data: { name: squadName, initials, avatarUrl: squadAvatarUrl ?? null, subtitle },
    };

    const memberNodes: SquadMemberNode[] = members.map((m, i) => ({
      id: m.id,
      type: "squadMember",
      position: { x: COLUMN_X, y: i * MEMBER_GAP_Y },
      data: {
        name: getEntityName(m.member_type, m.member_id),
        memberType: m.member_type,
        memberId: m.member_id,
        role: m.role ?? "",
        isLeader: m.member_type === "agent" && m.member_id === leaderId,
        leaderLabel: t(($) => $.members_tab.leader_chip),
      },
    }));

    return [root, ...memberNodes];
  }, [members, leaderId, getEntityName, squadName, initials, squadAvatarUrl, subtitle, t]);

  const edges = useMemo<Edge[]>(
    () =>
      members.map((m) => ({
        id: `squad-root->${m.id}`,
        source: "squad-root",
        target: m.id,
        style: { stroke: "var(--border)", strokeWidth: 1.5 },
      })),
    [members],
  );

  return (
    <div className="relative h-full w-full overflow-hidden rounded-lg border bg-background">
      <ReactFlow
        colorMode={colorMode}
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        fitView
        fitViewOptions={{ padding: 0.25 }}
        proOptions={{ hideAttribution: true }}
        nodesConnectable={false}
        nodesDraggable
        minZoom={0.4}
        maxZoom={1.5}
      >
        <Background
          variant={BackgroundVariant.Dots}
          gap={22}
          size={1}
          color="var(--border)"
        />
        <Controls
          showInteractive={false}
          className="!rounded-lg !border !border-border !bg-background !shadow-sm"
        />
        <Panel position="top-left">
          {/* Mini header card — same rounded-lg border bg-background chrome as the
              squad page cards, with the squad avatar + name like the page header. */}
          <div className="flex items-center gap-2 rounded-lg border bg-background px-3 py-1.5 shadow-sm">
            <ActorAvatarBase
              name={squadName}
              initials={initials}
              avatarUrl={squadAvatarUrl ?? undefined}
              isSquad
              size={20}
              className="shrink-0 rounded-md"
            />
            <span className="text-xs font-medium">{squadName}</span>
          </div>
        </Panel>
      </ReactFlow>
    </div>
  );
}
