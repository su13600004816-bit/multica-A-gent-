"use client";

import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  ReactFlow,
  ReactFlowProvider,
  Background,
  BackgroundVariant,
  Controls,
  Panel,
  Handle,
  Position,
  addEdge,
  useNodesState,
  useEdgesState,
  useReactFlow,
  type Node,
  type Edge,
  type Connection,
  type NodeProps,
  type NodeMouseHandler,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useTheme } from "@multica/ui/components/common/theme-provider";
import { ActorAvatar as ActorAvatarBase } from "@multica/ui/components/common/actor-avatar";
import { Button } from "@multica/ui/components/ui/button";
import { Plus, Trash2, Maximize2, RotateCcw } from "lucide-react";
import { issueListOptions } from "@multica/core/issues";
import { STATUS_CONFIG } from "@multica/core/issues/config";
import { useWorkspaceId } from "@multica/core/hooks";
import { api } from "@multica/core/api";
import type { Issue, IssueStatus, SquadMember } from "@multica/core/types";

// Stable empty default so `allIssues` keeps a constant reference while the query is
// loading/idle — otherwise a fresh [] each render re-computes statusByNodeId and
// drives the live-status effect into an infinite setState loop (React #185).
const EMPTY_ISSUES: Issue[] = [];
import { ActorAvatar } from "../../common/actor-avatar";
import { useT } from "../../i18n";

// Squad-scoped orchestration canvas (PL-111 v2). Each squad gets its own board:
// the squad sits at the root and its members fan out as nodes. Unlike the PL-89
// read-only preview, this board keeps the OLD system's manual-edit feel —
// nodes are draggable, you can draw connections between any nodes, add free
// "step" nodes, rename them inline (double-click) and delete the current
// selection — driven by the bottom 画布编排工具栏.
//
// Visual baseline = the Multica 小队 pages (squads-page / squad-detail-page /
// squad-profile-card): same card rounding (rounded-lg), border / bg-background /
// text-muted-foreground tokens, the same ActorAvatar usage and the SAME text-only
// leader chip as SquadProfileCard. No invented canvas skin — every node / panel /
// control / toolbar reads as native Multica chrome.
//
// PL-118 — node status colors + animated status-colored pipeline edges. Each node
// is tinted by the status of the work assigned to it, and the squad→member edges
// animate ("流水线流光") in the matching status color. The palette is NOT a new
// one: it is exactly the task-panel palette (STATUS_CONFIG), reused two ways —
// the node status dot uses the same `dividerColor` Tailwind class the 任务看板
// renders, and the node border / edge stroke use the SAME semantic design tokens
// (--warning / --success / --info / --destructive / --muted-foreground) those
// classes resolve to. The existing card chrome is untouched; status only layers
// a border tint + a status dot on top.

// status → semantic CSS token, mirroring STATUS_CONFIG exactly
// (in_progress=warning, in_review=success, done=info, blocked=destructive, rest=muted).
// Used for the node border tint and the ReactFlow edge stroke, which need a raw
// color value rather than a Tailwind class.
const STATUS_COLOR_VAR: Record<IssueStatus, string> = {
  backlog: "var(--muted-foreground)",
  todo: "var(--muted-foreground)",
  in_progress: "var(--warning)",
  in_review: "var(--success)",
  done: "var(--info)",
  blocked: "var(--destructive)",
  cancelled: "var(--muted-foreground)",
};

// Which status a node surfaces when its work spans several. Alerts first
// (blocked), then live work, then the rest. `cancelled` never surfaces.
const STATUS_PRECEDENCE: IssueStatus[] = [
  "blocked",
  "in_progress",
  "in_review",
  "todo",
  "backlog",
  "done",
];

function dominantStatus(issues: Issue[]): IssueStatus | null {
  for (const s of STATUS_PRECEDENCE) {
    if (issues.some((i) => i.status === s)) return s;
  }
  return null;
}

// Small status dot — same dividerColor class the 任务看板 paints, so a status
// reads identically on the canvas and in the task panel.
function StatusDot({ status }: { status: IssueStatus | null }) {
  if (!status) return null;
  return (
    <span
      className={`h-1.5 w-1.5 shrink-0 rounded-full ${STATUS_CONFIG[status].dividerColor}`}
    />
  );
}

// Border tint for a node carrying a status; idle nodes keep the default border.
function statusBorderStyle(status: IssueStatus | null): CSSProperties | undefined {
  return status ? { borderColor: STATUS_COLOR_VAR[status] } : undefined;
}

type RootNodeData = {
  name: string;
  initials: string;
  avatarUrl: string | null;
  subtitle: string;
  status: IssueStatus | null;
};
type MemberNodeData = {
  name: string;
  memberType: SquadMember["member_type"];
  memberId: string;
  role: string;
  isLeader: boolean;
  leaderLabel: string;
  status: IssueStatus | null;
};
type StepNodeData = {
  label: string;
};

type SquadRootNode = Node<RootNodeData, "squadRoot">;
type SquadMemberNode = Node<MemberNodeData, "squadMember">;
type FlowStepNode = Node<StepNodeData, "flowStep">;
type SquadFlowNode = SquadRootNode | SquadMemberNode | FlowStepNode;

const handleClass = "!h-2 !w-2 !border !border-border !bg-muted-foreground/40";

// Leader chip — byte-for-byte the same chip SquadProfileCard renders (text-only,
// no icon), so a leader reads identically on the canvas and in the squad card.
const leaderChipClass =
  "shrink-0 rounded-md bg-amber-100 px-1 py-0.5 text-[10px] font-medium text-amber-700 dark:bg-amber-900/30 dark:text-amber-400";

// Root node — the squad itself, composed exactly like the SquadProfileCard header
// (square-rounded squad avatar + name in text-sm font-semibold). Both handles are
// exposed so it can be freely wired to any node during manual editing.
function SquadRootNodeView({ data }: NodeProps<SquadRootNode>) {
  return (
    <div
      className="w-[216px] rounded-lg border bg-background px-4 py-3 shadow-sm"
      style={statusBorderStyle(data.status)}
    >
      <Handle type="target" position={Position.Top} className={handleClass} />
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
          <div className="flex items-center gap-1.5">
            <StatusDot status={data.status} />
            <span className="truncate text-xs text-muted-foreground">{data.subtitle}</span>
          </div>
        </div>
      </div>
      <Handle type="source" position={Position.Bottom} className={handleClass} />
    </div>
  );
}

// Member node — one agent / human, composed exactly like a SquadProfileCard
// member row (ActorAvatar + name in text-sm font-medium + muted role). Agent
// members get a clickable affordance (cursor + hover border/bg) because a click
// opens their 智能体详情 panel (PL-120); humans have no such panel so they stay
// inert.
function SquadMemberNodeView({ data }: NodeProps<SquadMemberNode>) {
  const clickable = data.memberType === "agent";
  return (
    <div
      className={`w-[200px] rounded-lg border bg-background px-3 py-2.5 shadow-sm ${
        clickable
          ? "cursor-pointer transition-colors hover:border-ring/60 hover:bg-accent/40"
          : ""
      }`}
      style={statusBorderStyle(data.status)}
    >
      <Handle type="target" position={Position.Top} className={handleClass} />
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
          <div className="flex items-center gap-1.5">
            <StatusDot status={data.status} />
            <span className="truncate text-xs capitalize text-muted-foreground">
              {data.role || data.memberType}
            </span>
          </div>
        </div>
      </div>
      <Handle type="source" position={Position.Bottom} className={handleClass} />
    </div>
  );
}

// Free step node — a user-added box for "manual" orchestration steps. Double-click
// to rename inline; it persists in the in-session board state. Same card chrome
// as every other node so it never reads as a foreign element.
function FlowStepNodeView({ id, data, selected }: NodeProps<FlowStepNode>) {
  const { setNodes } = useReactFlow();
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(data.label);

  useEffect(() => {
    setValue(data.label);
  }, [data.label]);

  const commit = useCallback(() => {
    setEditing(false);
    const next = value.trim() || data.label;
    setNodes((ns) =>
      ns.map((n) => (n.id === id ? { ...n, data: { ...n.data, label: next } } : n)),
    );
  }, [value, data.label, id, setNodes]);

  return (
    <div
      className={`min-w-[140px] max-w-[220px] rounded-lg border bg-background px-3 py-2 shadow-sm ${
        selected ? "ring-2 ring-ring" : ""
      }`}
      onDoubleClick={() => setEditing(true)}
    >
      <Handle type="target" position={Position.Top} className={handleClass} />
      {editing ? (
        <input
          autoFocus
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onBlur={commit}
          onKeyDown={(e) => {
            if (e.key === "Enter") commit();
            if (e.key === "Escape") {
              setValue(data.label);
              setEditing(false);
            }
          }}
          className="w-full bg-transparent text-sm font-medium outline-none"
        />
      ) : (
        <div className="truncate text-sm font-medium">{data.label}</div>
      )}
      <Handle type="source" position={Position.Bottom} className={handleClass} />
    </div>
  );
}

const nodeTypes = {
  squadRoot: SquadRootNodeView,
  squadMember: SquadMemberNodeView,
  flowStep: FlowStepNodeView,
};

// Vertical rhythm for the member column.
const MEMBER_GAP_Y = 84;
const COLUMN_X = 320;
const ROOT_X = 0;

// ============ 线小队生产线工作流布局引擎 ============
// 把扁平成员列表排成「主流程竖向 + 审计左合格右不合格 + 返工模块回路」的流程图。
const WF_GAP_Y = 96;
const WF_MAIN_X = 0;
const WF_REWORK_X = 340;

function wfRoleOf(s: string): string | null {
  if (/返工.*深挖|返工深挖/.test(s)) return "reworkDig";
  if (/代码深挖|深挖/.test(s)) return "codeDig";
  if (/主线主脑|主线|负责人|Leader/.test(s)) return "leader";
  if (/写码|写代码/.test(s)) return "write";
  if (/审计/.test(s)) return "audit";
  if (/代码优化|优化/.test(s)) return "optimize";
  if (/截图验/.test(s)) return "shotVerify";
  if (/虚机验|虚拟机/.test(s)) return "vmVerify";
  if (/拟人验|拟人/.test(s)) return "humanVerify";
  if (/PR推送|推送/.test(s)) return "push";
  if (/危机/.test(s)) return "crisis";
  if (/记忆/.test(s)) return "memory";
  if (/问题/.test(s)) return "problem";
  if (/看门狗/.test(s)) return "watchdog";
  return null;
}

// 主流程模板:role=角色,label=工序名,audit=判定节点(左合格右不合格),reworkMid=返工链中段角色
const LINE_FLOW: Array<{ role: string; label: string; audit?: boolean; reworkMid?: string }> = [
  { role: "leader", label: "主脑·任务推进" },
  { role: "write", label: "写代码" },
  { role: "audit", label: "审计 ①", audit: true, reworkMid: "write" },
  { role: "codeDig", label: "代码深挖" },
  { role: "optimize", label: "代码优化" },
  { role: "audit", label: "审计 ②", audit: true, reworkMid: "optimize" },
  { role: "shotVerify", label: "截图验证" },
  { role: "vmVerify", label: "虚拟机验证" },
  { role: "humanVerify", label: "拟人验收" },
  { role: "leader", label: "主脑签收", audit: true, reworkMid: "optimize" },
  { role: "push", label: "PR 推送(github/谷歌)" },
  { role: "crisis", label: "危机处理签收" },
  { role: "problem", label: "问题收集签收" },
  { role: "memory", label: "记忆储存·清除签收" },
  { role: "watchdog", label: "看门狗收口 ⏹" },
];

type WfDeps = {
  members: SquadMember[];
  nameOf: (m: SquadMember) => string;
  leaderId: string;
  statusOf: (nodeId: string) => IssueStatus | null;
  leaderLabel: string;
};

function buildLineWorkflow(d: WfDeps): { nodes: SquadFlowNode[]; edges: Edge[] } | null {
  const byRole: Record<string, SquadMember> = {};
  for (const m of d.members) {
    const r = wfRoleOf(`${d.nameOf(m)} ${m.role ?? ""}`);
    if (r && !byRole[r]) byRole[r] = m;
  }
  // 不是标准线小队(缺核心角色)就不用工作流布局
  if (!byRole.leader || !byRole.write || !byRole.audit) return null;

  const nodes: SquadFlowNode[] = [];
  const edges: Edge[] = [];
  const mkNode = (id: string, m: SquadMember, label: string, x: number, y: number): void => {
    nodes.push({
      id,
      type: "squadMember",
      position: { x, y },
      data: {
        name: d.nameOf(m),
        memberType: m.member_type,
        memberId: m.member_id,
        role: label,
        isLeader: m.member_type === "agent" && m.member_id === d.leaderId,
        leaderLabel: d.leaderLabel,
        status: d.statusOf(m.id),
      },
    } as SquadMemberNode);
  };
  const flowEdge = (id: string, src: string, tgt: string, label?: string, bad?: boolean): void => {
    edges.push({
      id, source: src, target: tgt,
      label,
      animated: false,
      style: { stroke: bad ? "#ef4444" : "#22c55e", strokeWidth: 1.5 },
      labelStyle: { fill: bad ? "#ef4444" : "#16a34a", fontSize: 11, fontWeight: 600 },
      labelBgStyle: { fill: "var(--background)", fillOpacity: 0.9 },
    } as Edge);
  };

  // 只保留有对应成员的工序
  const steps = LINE_FLOW.filter((st) => byRole[st.role]);
  let y = 0;
  const mainIds: string[] = [];
  steps.forEach((st, i) => {
    const mainM = byRole[st.role];
    if (!mainM) return;
    const id = `wf-m-${i}`;
    mainIds.push(id);
    mkNode(id, mainM, st.label, WF_MAIN_X, y);
    if (i > 0) {
      const prev = steps[i - 1];
      flowEdge(`wf-e-${i}`, mainIds[i - 1], id, prev.audit ? "✅合格" : undefined, false);
    }
    // 审计/签收 → 返工模块(右,不合格)
    const midRole = st.reworkMid ?? "write";
    const digM = byRole.reworkDig;
    const midM = byRole[midRole];
    const audM = byRole.audit;
    if (st.audit && digM && midM && audM) {
      const digId = `wf-r-${i}-dig`;
      const midId = `wf-r-${i}-mid`;
      const audId = `wf-r-${i}-aud`;
      mkNode(digId, digM, "返工深挖(BOM三视角·9层)", WF_REWORK_X, y);
      mkNode(midId, midM, midRole === "write" ? "返工写代码" : "返工代码优化", WF_REWORK_X, y + WF_GAP_Y);
      mkNode(audId, audM, "返工审计", WF_REWORK_X, y + WF_GAP_Y * 2);
      flowEdge(`wf-re-${i}-1`, id, digId, "❌不合格", true);
      flowEdge(`wf-re-${i}-2`, digId, midId);
      flowEdge(`wf-re-${i}-3`, midId, audId);
      flowEdge(`wf-re-${i}-back`, audId, digId, "❌不合格 ↺最高9次", true);
      // 返工审计 合格 → 下一主工序
      if (i + 1 < steps.length) flowEdge(`wf-re-${i}-ok`, audId, `wf-m-${i + 1}`, "✅合格", false);
    }
    y += WF_GAP_Y;
  });

  return { nodes, edges };
}


const IDLE_EDGE_STYLE = { stroke: "var(--border)", strokeWidth: 1.5 } as const;

// A squad→member edge: status drives both the stroke color and the animated
// "流水线" flow. Idle members get a plain static line.
function statusEdgeStyle(status: IssueStatus | null) {
  if (!status) return { ...IDLE_EDGE_STYLE };
  return { stroke: STATUS_COLOR_VAR[status], strokeWidth: 2 };
}

function initialsOf(name: string): string {
  return name
    .split(" ")
    .map((w) => w[0])
    .join("")
    .toUpperCase()
    .slice(0, 2);
}

interface BoardProps {
  squadId: string;
  squadName: string;
  squadAvatarUrl?: string | null;
  leaderId: string;
  members: SquadMember[];
  getEntityName: (type: string, id: string) => string;
  // Fired when an agent member node is clicked — the page opens that agent's
  // 智能体详情 panel (PL-120). Human members don't trigger it.
  onSelectAgent?: (agentId: string) => void;
}

function SquadCanvasFlow({
  squadId,
  squadName,
  squadAvatarUrl,
  leaderId,
  members,
  getEntityName,
  onSelectAgent,
}: BoardProps) {
  const { t } = useT("squads");
  const { resolvedTheme } = useTheme();
  const colorMode = resolvedTheme === "dark" ? "dark" : "light";
  const { fitView, getNodes, getEdges } = useReactFlow();
  // P3 手工编排持久化:按 squad 存 localStorage,拖完即存,刷新套用。
  const storageKey = `mca-canvas-layout-${squadId}`;
  const stepCounter = useRef(0);
  const wsId = useWorkspaceId();

  const initials = initialsOf(squadName);
  const subtitle = t(($) => $.canvas_tab.root_subtitle, { count: members.length });

  // Same board feed the 任务看板 uses, so node/edge status matches the panel.
  const { data: allIssues = EMPTY_ISSUES } = useQuery({
    ...issueListOptions(wsId),
    enabled: !!wsId,
  });

  // P3 服务端持久化:取该小队保存的布局(跨设备)。undefined=加载中。
  const { data: serverLayoutResp } = useQuery({
    queryKey: ["squadCanvasLayout", squadId],
    queryFn: () => api.getSquadCanvasLayout(squadId),
    enabled: !!squadId,
    staleTime: 30_000,
  });
  const serverLayout =
    serverLayoutResp === undefined
      ? undefined
      : ((serverLayoutResp.layout as { positions?: Record<string, { x: number; y: number }>; steps?: SquadFlowNode[]; edges?: Edge[] } | null) ?? null);

  // Status per member NODE id (member rows are keyed by m.id, issues by m.member_id),
  // plus an aggregate status for the squad root.
  const { statusByNodeId, rootStatus } = useMemo(() => {
    const byAssignee = new Map<string, Issue[]>();
    for (const issue of allIssues as Issue[]) {
      if (!issue.assignee_id) continue;
      const list = byAssignee.get(issue.assignee_id);
      if (list) list.push(issue);
      else byAssignee.set(issue.assignee_id, [issue]);
    }
    const byNode: Record<string, IssueStatus | null> = {};
    const relevant: Issue[] = byAssignee.get(squadId) ?? [];
    for (const m of members) {
      const mine = byAssignee.get(m.member_id) ?? [];
      byNode[m.id] = dominantStatus(mine);
      relevant.push(...mine);
    }
    return { statusByNodeId: byNode, rootStatus: dominantStatus(relevant) };
  }, [allIssues, members, squadId]);

  // Seed the board from the squad + its members. This is the starting picture;
  // the user can then drag / connect / add / rename / delete on top of it.
  const seed = useMemo(() => {
    const wf = buildLineWorkflow({
      members,
      nameOf: (m) => getEntityName(m.member_type, m.member_id),
      leaderId,
      statusOf: (id) => statusByNodeId[id] ?? null,
      leaderLabel: t(($) => $.members_tab.leader_chip),
    });
    if (wf) return wf;

    const span = Math.max(0, members.length - 1) * MEMBER_GAP_Y;
    const rootY = span / 2;

    const root: SquadRootNode = {
      id: "squad-root",
      type: "squadRoot",
      position: { x: ROOT_X, y: rootY },
      data: {
        name: squadName,
        initials,
        avatarUrl: squadAvatarUrl ?? null,
        subtitle,
        status: rootStatus,
      },
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
        status: statusByNodeId[m.id] ?? null,
      },
    }));

    const seedEdges: Edge[] = members.map((m) => {
      const st = statusByNodeId[m.id] ?? null;
      return {
        id: `squad-root->${m.id}`,
        source: "squad-root",
        target: m.id,
        animated: st != null,
        style: statusEdgeStyle(st),
      };
    });

    return { nodes: [root, ...memberNodes] as SquadFlowNode[], edges: seedEdges };
    // statusByNodeId/rootStatus deliberately excluded — status refreshes are
    // applied in place (below) so live status updates never clobber manual edits.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [members, leaderId, getEntityName, squadName, initials, squadAvatarUrl, subtitle, t]);

  const [nodes, setNodes, onNodesChange] = useNodesState<SquadFlowNode>(seed.nodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(seed.edges);

  // Re-seed when the underlying squad/members change (e.g. async load or a
  // member added elsewhere). Keyed on the member id set so in-session edits to
  // positions/labels aren't clobbered on every refetch.
  const seedKey = members.map((m) => m.id).join(",");
  const lastSeedKey = useRef<string | null>(null);
  useEffect(() => {
    // seedKey(成员加载)或服务端布局就绪 任一变化都重应用;绝不阻塞节点渲染。
    const sig = seedKey + "|" + (serverLayout === undefined ? "loading" : "ready");
    if (lastSeedKey.current === sig) return;
    lastSeedKey.current = sig;
    let saved: { positions?: Record<string, { x: number; y: number }>; steps?: SquadFlowNode[]; edges?: Edge[] } | null =
      serverLayout && serverLayout.positions ? serverLayout : null;
    if (!saved) {
      try {
        const raw = typeof window !== "undefined" ? window.localStorage.getItem(storageKey) : null;
        saved = raw ? JSON.parse(raw) : null;
      } catch {
        saved = null;
      }
    }
    if (!saved) {
      setNodes(seed.nodes);
      setEdges(seed.edges);
      if (seed.nodes.length > 1) {
        window.requestAnimationFrame(() => fitView({ padding: 0.25 }));
      }
      return;
    }
    const pos = saved.positions ?? {};
    const seedIds = new Set(seed.nodes.map((n) => n.id));
    const placed = seed.nodes.map((n) => (pos[n.id] ? { ...n, position: { ...pos[n.id] } } : n));
    const steps = Array.isArray(saved.steps)
      ? saved.steps.filter((sn) => sn && sn.id && !seedIds.has(sn.id))
      : [];
    const customEdges = Array.isArray(saved.edges)
      ? saved.edges.filter((e) => e && e.id && !/^squad-root->/.test(e.id))
      : [];
    setNodes([...placed, ...steps] as SquadFlowNode[]);
    setEdges([...seed.edges, ...customEdges]);
    if (seed.nodes.length > 1) {
      window.requestAnimationFrame(() => fitView({ padding: 0.25 }));
    }
  }, [seedKey, seed, serverLayout, setNodes, setEdges, storageKey, fitView]);

  // Live status refresh — patch node tint/dot and the squad→member edge flow in
  // place when statuses change, WITHOUT touching positions, manually-added nodes
  // or manually-drawn edges. Only the seed `squad-root->*` edges carry status.
  useEffect(() => {
    setNodes((ns) =>
      ns.map((n) => {
        if (n.id.startsWith("wf-")) return n; // 工作流节点状态在 seed 已设,跳过
        if (n.type === "squadMember") {
          const st = statusByNodeId[n.id] ?? null;
          const data = n.data as MemberNodeData;
          return data.status === st ? n : { ...n, data: { ...data, status: st } };
        }
        if (n.type === "squadRoot") {
          const data = n.data as RootNodeData;
          return data.status === rootStatus ? n : { ...n, data: { ...data, status: rootStatus } };
        }
        return n;
      }),
    );
    setEdges((es) =>
      es.map((e) => {
        const m = /^squad-root->(.+)$/.exec(e.id);
        const nodeId = m?.[1];
        if (!nodeId) return e;
        const st = statusByNodeId[nodeId] ?? null;
        return { ...e, animated: st != null, style: statusEdgeStyle(st) };
      }),
    );
  }, [statusByNodeId, rootStatus, setNodes, setEdges]);

  const saveLayout = useCallback(() => {
    try {
      const ns = getNodes();
      const es = getEdges();
      const positions: Record<string, { x: number; y: number }> = {};
      for (const n of ns) positions[n.id] = { x: n.position.x, y: n.position.y };
      const steps = ns
        .filter((n) => n.type === "flowStep")
        .map((n) => ({ id: n.id, type: n.type, position: n.position, data: n.data }));
      const customEdges = es
        .filter((e) => !/^squad-root->/.test(e.id))
        .map((e) => ({ id: e.id, source: e.source, target: e.target, style: e.style }));
      const payload = { positions, steps, edges: customEdges };
      window.localStorage.setItem(storageKey, JSON.stringify(payload));
      if (squadId) {
        void api.setSquadCanvasLayout(squadId, payload).catch(() => {});
      }
    } catch {
      /* localStorage 不可用时静默 */
    }
  }, [getNodes, getEdges, storageKey, squadId]);
  const saveSoon = useCallback(() => {
    window.requestAnimationFrame(() => saveLayout());
  }, [saveLayout]);

  const onConnect = useCallback(
    (connection: Connection) => {
      setEdges((eds) => addEdge({ ...connection, style: { ...IDLE_EDGE_STYLE } }, eds));
      saveSoon();
    },
    [setEdges, saveSoon],
  );

  // Click an agent member node → open its 智能体详情 panel. Dragging does NOT
  // fire onNodeClick (ReactFlow distinguishes click from drag), so the board
  // stays fully editable while clicks open the panel. Humans / root / free
  // step nodes have no detail panel and are ignored.
  const onNodeClick = useCallback<NodeMouseHandler<SquadFlowNode>>(
    (_, node) => {
      if (node.type === "squadMember" && node.data.memberType === "agent") {
        onSelectAgent?.(node.data.memberId);
      }
    },
    [onSelectAgent],
  );

  const handleAddNode = useCallback(() => {
    stepCounter.current += 1;
    const n = stepCounter.current;
    const newNode: FlowStepNode = {
      id: `step-${Date.now()}-${n}`,
      type: "flowStep",
      position: { x: COLUMN_X + 60, y: n * 70 },
      data: { label: t(($) => $.canvas_page.new_node_label) },
    };
    setNodes((ns) => [...ns, newNode]);
    saveSoon();
  }, [setNodes, t, saveSoon]);

  const handleDeleteSelected = useCallback(() => {
    setNodes((ns) => ns.filter((n) => !n.selected));
    setEdges((es) => es.filter((e) => !e.selected));
    saveSoon();
  }, [setNodes, setEdges, saveSoon]);

  const handleReset = useCallback(() => {
    try {
      window.localStorage.removeItem(storageKey);
    } catch {
      /* ignore */
    }
    setNodes(seed.nodes);
    setEdges(seed.edges);
    stepCounter.current = 0;
    window.requestAnimationFrame(() => fitView({ padding: 0.25 }));
  }, [seed, setNodes, setEdges, fitView, storageKey]);

  return (
    <div className="relative h-full w-full overflow-hidden rounded-lg border bg-background">
      <ReactFlow
        colorMode={colorMode}
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onConnect={onConnect}
        onNodeClick={onNodeClick}
        onNodeDragStop={() => saveSoon()}
        fitView
        fitViewOptions={{ padding: 0.25 }}
        proOptions={{ hideAttribution: true }}
        nodesConnectable
        nodesDraggable
        elementsSelectable
        minZoom={0.4}
        maxZoom={1.5}
        deleteKeyCode={["Backspace", "Delete"]}
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

        {/* 画布编排工具栏 — bottom toolbar. Multica Button tokens; the same chrome
            as the rest of the page so it never reads as a foreign canvas skin. */}
        <Panel position="bottom-center">
          <div className="flex items-center gap-1 rounded-lg border bg-background px-1.5 py-1 shadow-sm">
            <Button size="xs" variant="ghost" onClick={handleAddNode}>
              <Plus className="size-3.5 mr-1" />
              {t(($) => $.canvas_page.toolbar_add_node)}
            </Button>
            <Button size="xs" variant="ghost" onClick={handleDeleteSelected}>
              <Trash2 className="size-3.5 mr-1" />
              {t(($) => $.canvas_page.toolbar_delete)}
            </Button>
            <span className="mx-0.5 h-4 w-px bg-border" />
            <Button size="xs" variant="ghost" onClick={() => fitView({ padding: 0.25 })}>
              <Maximize2 className="size-3.5 mr-1" />
              {t(($) => $.canvas_page.toolbar_fit)}
            </Button>
            <Button size="xs" variant="ghost" onClick={handleReset}>
              <RotateCcw className="size-3.5 mr-1" />
              {t(($) => $.canvas_page.toolbar_reset)}
            </Button>
            <span className="ml-1 mr-1 hidden text-[10px] text-muted-foreground sm:inline">
              {t(($) => $.canvas_page.toolbar_hint)}
            </span>
          </div>
        </Panel>
      </ReactFlow>
    </div>
  );
}

export function SquadCanvasBoard(props: BoardProps) {
  // Provider so custom nodes / the toolbar can call useReactFlow().
  return (
    <ReactFlowProvider>
      <SquadCanvasFlow {...props} />
    </ReactFlowProvider>
  );
}
