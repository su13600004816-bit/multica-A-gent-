-- Memory archiving + context-bleeding control (PL-91).
--
-- Token usage spiked because the backend silently resumes the prior
-- chat_session / issue-agent provider session on every follow-up and, for
-- assignment tasks, instructs the agent to read the FULL comment history
-- (server cap 2000). Nothing ever compacts that history or cuts the old
-- provider session loose, so each turn re-ingests an ever-growing context.
--
-- This migration is the data layer for the fix. It is intentionally
-- ADDITIVE and FAIL-SAFE: it never drops or rewrites chat_message /
-- comment rows. Compaction produces a separate, lower-gradient summary in
-- memory_archive and only sets nullable marker columns. The original
-- transcript always survives and stays drill-down reachable (T4 -> T1).

-- T1..T4 archive store. One row per (scope, level) generation. Older
-- generations are kept (not overwritten) so a scope can be re-compacted
-- incrementally and every layer remains source-traceable.
CREATE TABLE memory_archive (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL REFERENCES workspace(id) ON DELETE CASCADE,
    -- What was archived. 'task' covers a single agent_task_queue run;
    -- 'squad' covers a squad leader thread.
    scope_type TEXT NOT NULL CHECK (scope_type IN ('chat_session', 'issue', 'task', 'squad')),
    scope_id UUID NOT NULL,
    -- T1 = shortest headline gradient (injected by default into fresh
    -- sessions), T4 = richest near-verbatim digest (only fetched on
    -- explicit drill-down). T1 < T2 < T3 < T4 in detail.
    level TEXT NOT NULL CHECK (level IN ('T1', 'T2', 'T3', 'T4')),
    content TEXT NOT NULL,
    -- Inclusive time window of the source records this archive summarises,
    -- so a reader can re-fetch exactly the chat_message / comment rows that
    -- back this layer instead of guessing. Nullable for synthetic content.
    source_from TIMESTAMPTZ,
    source_to TIMESTAMPTZ,
    -- How many source records were folded in (audit / traceability only).
    source_count INTEGER NOT NULL DEFAULT 0,
    -- 'deterministic' fallback vs a model id (e.g. 'qwen-...'). Lets a later
    -- run tell apart fail-safe local summaries from model-quality ones and
    -- re-compact the former when the Qwen bridge is wired.
    generator TEXT NOT NULL DEFAULT 'deterministic',
    created_by_agent_id UUID REFERENCES agent(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_memory_archive_scope ON memory_archive(scope_type, scope_id, level, created_at DESC);
CREATE INDEX idx_memory_archive_workspace ON memory_archive(workspace_id, created_at DESC);

-- Compaction markers. When set, the scope has a usable archive and the
-- daemon claim path must treat the next task as a FRESH session: do not
-- inject PriorSessionID / PriorWorkDir, do not demand full history. These
-- are nullable and default unset, so existing rows keep current behaviour.
ALTER TABLE chat_session ADD COLUMN compacted_at TIMESTAMPTZ;
ALTER TABLE issue ADD COLUMN memory_compacted_at TIMESTAMPTZ;

-- Session-resume kill switch (issue requirement E). Lets an operator force
-- every claim under a workspace or a specific agent to start fresh,
-- independent of compaction state — the global "止血" lever for cc /
-- Claude开发 / cx / chat. Default true preserves today's resume behaviour.
ALTER TABLE workspace ADD COLUMN session_resume_enabled BOOLEAN NOT NULL DEFAULT true;
ALTER TABLE agent ADD COLUMN session_resume_enabled BOOLEAN NOT NULL DEFAULT true;
