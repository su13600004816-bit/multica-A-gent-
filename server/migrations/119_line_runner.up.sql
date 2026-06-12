-- Line runner: deterministic DAG pipeline execution over multica issues.
-- A `line` stores a reusable pipeline graph (nodes = stages, edges = deps).
-- A `line_run` is one execution: the backend line runner goroutine creates an
-- issue per node when its predecessors complete, gating advancement on issue
-- terminal status (done = pass; cancelled = fail → bounded rework of upstream).
CREATE TABLE IF NOT EXISTS line (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id    uuid NOT NULL REFERENCES workspace(id) ON DELETE CASCADE,
  project_id      uuid REFERENCES project(id) ON DELETE SET NULL,
  title           text NOT NULL,
  graph           jsonb NOT NULL DEFAULT '{"nodes":[],"edges":[]}'::jsonb,
  status          text NOT NULL DEFAULT 'active',
  created_by_type text NOT NULL DEFAULT 'agent',
  created_by_id   uuid,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS line_run (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  line_id      uuid NOT NULL REFERENCES line(id) ON DELETE CASCADE,
  workspace_id uuid NOT NULL REFERENCES workspace(id) ON DELETE CASCADE,
  status       text NOT NULL DEFAULT 'running',
  graph        jsonb NOT NULL,
  node_state   jsonb NOT NULL DEFAULT '{}'::jsonb,
  error        text,
  started_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now(),
  finished_at  timestamptz
);

CREATE INDEX IF NOT EXISTS idx_line_workspace ON line (workspace_id);
CREATE INDEX IF NOT EXISTS idx_line_run_line ON line_run (line_id);
CREATE INDEX IF NOT EXISTS idx_line_run_active ON line_run (status) WHERE status = 'running';

-- Allow issues created by the line runner to carry origin_type='line'.
ALTER TABLE issue DROP CONSTRAINT IF EXISTS issue_origin_type_check;
ALTER TABLE issue ADD CONSTRAINT issue_origin_type_check
  CHECK (origin_type = ANY (ARRAY['autopilot'::text, 'quick_create'::text, 'lark_chat'::text, 'line'::text]));
