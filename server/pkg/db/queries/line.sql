-- =====================
-- Line runner: lines + runs
-- =====================

-- name: CreateLine :one
INSERT INTO line (workspace_id, project_id, title, graph, status, created_by_type, created_by_id)
VALUES (
  sqlc.arg('workspace_id'),
  sqlc.narg('project_id'),
  sqlc.arg('title'),
  sqlc.arg('graph'),
  sqlc.arg('status'),
  sqlc.arg('created_by_type'),
  sqlc.narg('created_by_id')
) RETURNING *;

-- name: GetLine :one
SELECT * FROM line WHERE id = $1;

-- name: GetLineInWorkspace :one
SELECT * FROM line WHERE id = $1 AND workspace_id = $2;

-- name: ListLines :many
SELECT * FROM line WHERE workspace_id = $1 ORDER BY created_at DESC;

-- name: CreateLineRun :one
INSERT INTO line_run (line_id, workspace_id, status, graph, node_state)
VALUES (sqlc.arg('line_id'), sqlc.arg('workspace_id'), 'running', sqlc.arg('graph'), sqlc.arg('node_state'))
RETURNING *;

-- name: GetLineRun :one
SELECT * FROM line_run WHERE id = $1;

-- name: GetLineRunInWorkspace :one
SELECT * FROM line_run WHERE id = $1 AND workspace_id = $2;

-- name: ListActiveLineRuns :many
SELECT * FROM line_run WHERE status = 'running' ORDER BY started_at ASC;

-- name: ListLineRunsForLine :many
SELECT * FROM line_run WHERE line_id = $1 ORDER BY started_at DESC;

-- name: UpdateLineRunState :one
UPDATE line_run
SET node_state = sqlc.arg('node_state'),
    status = sqlc.arg('status'),
    error = sqlc.narg('error'),
    updated_at = now(),
    finished_at = CASE WHEN sqlc.arg('status') IN ('passed','failed','cancelled') THEN now() ELSE finished_at END
WHERE id = sqlc.arg('id')
RETURNING *;
