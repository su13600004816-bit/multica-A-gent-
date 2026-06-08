-- Memory archiving + session-resume gating (PL-91).
-- Originals (chat_message / comment) are never mutated here; these queries
-- only write the lower-gradient summary and flip nullable marker columns.

-- name: CreateMemoryArchive :one
INSERT INTO memory_archive (
    workspace_id, scope_type, scope_id, level, content,
    source_from, source_to, source_count, generator, created_by_agent_id
)
VALUES (
    $1, $2, $3, $4, $5,
    sqlc.narg('source_from'), sqlc.narg('source_to'), $6, $7,
    sqlc.narg('created_by_agent_id')
)
RETURNING *;

-- name: ListMemoryArchivesByScope :many
-- All archive generations for a scope, newest first. The drill-down read:
-- T1/T2 are injected by default, T3/T4 only fetched on demand.
SELECT * FROM memory_archive
WHERE scope_type = $1 AND scope_id = $2
ORDER BY created_at DESC, level ASC;

-- name: GetLatestMemoryArchiveLevel :one
-- Most recent archive of one level for a scope. Used to inject the T1/T2
-- headline into a fresh session without pulling the whole transcript.
SELECT * FROM memory_archive
WHERE scope_type = $1 AND scope_id = $2 AND level = $3
ORDER BY created_at DESC
LIMIT 1;

-- name: MarkChatSessionCompacted :exec
-- Stamp the scope as compacted AND cut the provider session loose. Both the
-- chat_session pointer and (implicitly, via compacted_at) the per-task
-- fallback are now ignored by the daemon claim path, so the next message
-- starts a fresh agent session that only carries the injected summary.
UPDATE chat_session
SET compacted_at = now(), session_id = NULL, work_dir = NULL, updated_at = now()
WHERE id = $1;

-- name: MarkIssueMemoryCompacted :exec
UPDATE issue SET memory_compacted_at = now()
WHERE id = $1;

-- name: GetIssueMemoryCompactedAt :one
SELECT memory_compacted_at FROM issue WHERE id = $1;

-- name: GetAgentSessionResumeEnabled :one
-- Combined workspace + agent kill switch. false => the daemon must never
-- resume a prior session for this agent's tasks, regardless of compaction.
SELECT (a.session_resume_enabled AND w.session_resume_enabled)::bool AS resume_enabled
FROM agent a
JOIN workspace w ON w.id = a.workspace_id
WHERE a.id = $1;
