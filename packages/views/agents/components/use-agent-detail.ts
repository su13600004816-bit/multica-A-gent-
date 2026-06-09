"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import type {
  Agent,
  AgentRuntime,
  MemberWithUser,
  UpdateAgentRequest,
} from "@multica/core/types";
import {
  type AgentPresenceDetail,
  useWorkspacePresenceMap,
} from "@multica/core/agents";
import { api, ApiError } from "@multica/core/api";
import type { Decision } from "@multica/core/permissions";
import { useAuthStore } from "@multica/core/auth";
import { useWorkspaceId } from "@multica/core/hooks";
import {
  agentListOptions,
  memberListOptions,
  workspaceKeys,
} from "@multica/core/workspace/queries";
import { runtimeListOptions } from "@multica/core/runtimes";
import { useAgentPermissions } from "@multica/core/permissions";
import { useT } from "../../i18n";

/**
 * Shared data layer for every agent-detail surface — the full-page
 * `AgentDetailPage` and the canvas node-click `AgentDetailDialog` (PL-120) both
 * consume this hook, so identity / properties / activity / the optimistic
 * update flow stay byte-identical between the two. Keeping a single source of
 * truth here is the whole point: the dialog is the agent detail page, just in a
 * modal frame.
 */
export interface AgentDetailData {
  agent: Agent | null;
  agentsLoading: boolean;
  agentsError: unknown;
  refetchAgents: () => void;
  runtimes: AgentRuntime[];
  members: MemberWithUser[];
  presence: AgentPresenceDetail | null;
  runtime: AgentRuntime | null;
  owner: MemberWithUser | null;
  currentUserId: string | null;
  canEdit: Decision;
  isForbidden: boolean;
  handleUpdate: (id: string, data: Record<string, unknown>) => Promise<void>;
}

export function useAgentDetail(agentId: string): AgentDetailData {
  const { t } = useT("agents");
  const wsId = useWorkspaceId();
  const qc = useQueryClient();
  const currentUser = useAuthStore((s) => s.user);

  const {
    data: agents = [],
    isLoading: agentsLoading,
    error: agentsError,
    refetch: refetchAgents,
  } = useQuery(agentListOptions(wsId));
  const { data: runtimes = [] } = useQuery(runtimeListOptions(wsId));
  const { data: members = [] } = useQuery(memberListOptions(wsId));

  // Single workspace-level presence pass; this hook just reads its slot.
  // The hook owns the 30s tick so the failed-window auto-clears here too.
  const { byAgent: presenceMap } = useWorkspacePresenceMap(wsId);

  const agent = agents.find((a) => a.id === agentId) ?? null;
  const presence: AgentPresenceDetail | null =
    agent ? presenceMap.get(agent.id) ?? null : null;

  // Fallback fetch: when the agent is missing from the workspace list, hit
  // GET /api/agents/{id} directly to disambiguate "doesn't exist" (404) from
  // "you can't see this private agent" (403). Only fires after the list has
  // settled, so the common path makes zero extra requests.
  const { error: detailError } = useQuery({
    queryKey: ["agent-detail-probe", wsId, agentId],
    queryFn: () => api.getAgent(agentId),
    enabled: !agentsLoading && !agent && !!agentId,
    retry: false,
  });
  const isForbidden =
    detailError instanceof ApiError && detailError.status === 403;

  // Permission hook MUST be called unconditionally — its `agent | null`
  // signature handles the not-found / loading case internally. Backend gates
  // archive and restore identically to edit, so a single `canEdit` covers all.
  const { canEdit } = useAgentPermissions(agent, wsId);

  const handleUpdate = async (id: string, data: Record<string, unknown>) => {
    // Optimistic update: patch the matching agent in the cached list
    // BEFORE the network round-trip so the inspector picker chips flip to
    // the new value immediately on click. Without this, every inspector
    // picker (thinking / visibility / concurrency / model / runtime) waits
    // 0.5-2s for the API response + invalidate + refetch before the trigger
    // updates — readable as obvious lag in the UI.
    //
    // On error we rollback only the fields THIS call wrote, leaving any
    // other concurrently-mutated fields untouched, then invalidate so the
    // cache converges with the server. A whole-list snapshot rollback
    // would clobber a concurrent successful mutation if the failing call
    // resolves last (e.g. flipping visibility then runtime simultaneously
    // and only the visibility PATCH fails).
    const queryKey = workspaceKeys.agents(wsId);
    const prevAgents = qc.getQueryData<Agent[]>(queryKey);
    const prevAgent = prevAgents?.find((a) => a.id === id);
    const prevFields: Record<string, unknown> = {};
    if (prevAgent) {
      for (const key of Object.keys(data)) {
        prevFields[key] = (prevAgent as unknown as Record<string, unknown>)[key];
      }
    }
    qc.setQueryData<Agent[]>(queryKey, (old) =>
      old?.map((a) => (a.id === id ? ({ ...a, ...data } as Agent) : a)),
    );
    try {
      await api.updateAgent(id, data as UpdateAgentRequest);
      qc.invalidateQueries({ queryKey });
      toast.success(t(($) => $.detail.agent_updated_toast));
    } catch (e) {
      if (prevAgent) {
        qc.setQueryData<Agent[]>(queryKey, (old) =>
          old?.map((a) =>
            a.id === id ? ({ ...a, ...prevFields } as Agent) : a,
          ),
        );
      }
      qc.invalidateQueries({ queryKey });
      toast.error(e instanceof Error ? e.message : t(($) => $.detail.update_failed_toast));
      throw e;
    }
  };

  const runtime = agent?.runtime_id
    ? runtimes.find((r) => r.id === agent.runtime_id) ?? null
    : null;
  const owner = agent?.owner_id
    ? members.find((m) => m.user_id === agent.owner_id) ?? null
    : null;

  return {
    agent,
    agentsLoading,
    agentsError,
    refetchAgents,
    runtimes,
    members,
    presence,
    runtime,
    owner,
    currentUserId: currentUser?.id ?? null,
    canEdit,
    isForbidden,
    handleUpdate,
  };
}
