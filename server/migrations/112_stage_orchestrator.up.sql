-- PL-106 / PL-103: Stage advancement orchestrator / watchdog.
--
-- Two small tables back an event-driven orchestrator that auto-advances the
-- dev -> audit -> rework -> audit -> gate -> done pipeline and reminds the
-- line leader when a stage completes but the next action stalls.

-- Per-issue lightweight state machine + watchdog bookkeeping. One row per
-- orchestrated issue; created lazily the first time the issue produces a
-- stage event (e.g. dev -> in_review).
CREATE TABLE stage_orchestration (
    issue_id          UUID PRIMARY KEY REFERENCES issue(id) ON DELETE CASCADE,
    workspace_id      UUID NOT NULL REFERENCES workspace(id) ON DELETE CASCADE,
    -- dev | audit | rework | gate | done
    stage             TEXT NOT NULL DEFAULT 'dev',
    -- The auditor agent chosen for the first review. Reused for every
    -- re-review so the same reviewer follows the issue through rework.
    audit_agent_id    UUID REFERENCES agent(id) ON DELETE SET NULL,
    -- Timestamp of the last stage-advancing event. The watchdog measures
    -- "no next action" against this.
    last_event_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Identifies the current stuck point the watchdog has already reminded
    -- about, so the same stall is only reminded once (stage + last_event_at).
    -- When a new event advances the issue this key changes and a fresh stall
    -- can be reminded again.
    reminded_key      TEXT NOT NULL DEFAULT '',
    reminded_at       TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT stage_orchestration_stage_check
        CHECK (stage IN ('dev', 'audit', 'rework', 'gate', 'done'))
);

CREATE INDEX idx_stage_orch_workspace ON stage_orchestration(workspace_id);
-- Watchdog scan: open stages ordered by staleness.
CREATE INDEX idx_stage_orch_open ON stage_orchestration(last_event_at)
    WHERE stage <> 'done';

-- Config switches. A row with agent_id IS NULL is the workspace default; a
-- row with a concrete agent_id overrides it for issues assigned to that agent
-- (the developer). No row at all means the orchestrator is OFF for the
-- workspace (opt-in), so existing workspaces are unaffected by the rollout.
CREATE TABLE stage_orchestrator_config (
    workspace_id UUID NOT NULL REFERENCES workspace(id) ON DELETE CASCADE,
    agent_id     UUID REFERENCES agent(id) ON DELETE CASCADE,
    enabled      BOOLEAN NOT NULL DEFAULT TRUE,
    -- Per-event-action toggles, e.g. {"audit":true,"gate":false,"deepdig":true,
    -- "rework":true,"reminder":true}. A missing key defaults to enabled, so an
    -- empty object means "all actions on".
    actions      JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One config row per (workspace, agent). COALESCE folds the workspace-default
-- (NULL agent) row into the same uniqueness domain.
CREATE UNIQUE INDEX idx_stage_orch_cfg_ws_agent
    ON stage_orchestrator_config(workspace_id, COALESCE(agent_id, '00000000-0000-0000-0000-000000000000'::uuid));
