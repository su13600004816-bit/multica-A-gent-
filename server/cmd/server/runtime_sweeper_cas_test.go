package main

import (
	"context"
	"testing"

	db "github.com/multica-ai/multica/server/pkg/db/generated"
)

// seedIssueWithStatus inserts an issue in the given status owned by a member of
// the test workspace and returns its UUID, registering cleanup.
func seedIssueWithStatus(t *testing.T, status string) string {
	t.Helper()
	ctx := context.Background()
	var issueID string
	if err := testPool.QueryRow(ctx, `
		INSERT INTO issue (workspace_id, title, status, priority, creator_type, creator_id)
		SELECT $1, 'sweeper CAS issue', $2, 'none', 'member', m.user_id
		FROM member m WHERE m.workspace_id = $1 LIMIT 1
		RETURNING id
	`, testWorkspaceID, status).Scan(&issueID); err != nil {
		t.Fatalf("seed issue: %v", err)
	}
	t.Cleanup(func() {
		testPool.Exec(context.Background(), `DELETE FROM issue WHERE id = $1`, issueID)
	})
	return issueID
}

// TestSweeperRollbackCAS_PreservesConcurrentTerminalStatus is the regression
// test for SPEC §6 conflict A (scenario ②): the watchdog/runtime sweeper rolls
// a stuck task's issue back to 'todo', but between its read and its write the
// squad leader may finish and write a terminal status. The compare-and-swap
// UpdateIssueStatusIfCurrent flips to 'todo' ONLY while the issue is still
// 'in_progress', so a stale timeout-driven reset can never clobber the fresh
// event-driven 'done'/'cancelled' the leader just committed.
func TestSweeperRollbackCAS_PreservesConcurrentTerminalStatus(t *testing.T) {
	if testPool == nil {
		t.Skip("no database connection")
	}
	ctx := context.Background()
	queries := db.New(testPool)

	for _, terminal := range []string{"done", "cancelled"} {
		t.Run(terminal, func(t *testing.T) {
			// The leader has already moved the issue to a terminal status —
			// this is what the sweeper read missed.
			issueID := seedIssueWithStatus(t, terminal)

			rows, err := queries.UpdateIssueStatusIfCurrent(ctx, db.UpdateIssueStatusIfCurrentParams{
				NewStatus:      "todo",
				ID:             parseUUID(issueID),
				WorkspaceID:    parseUUID(testWorkspaceID),
				ExpectedStatus: "in_progress",
			})
			if err != nil {
				t.Fatalf("UpdateIssueStatusIfCurrent: %v", err)
			}
			if rows != 0 {
				t.Fatalf("CAS must not roll back an issue that already left in_progress; updated %d rows", rows)
			}

			var status string
			if err := testPool.QueryRow(ctx, `SELECT status FROM issue WHERE id = $1`, issueID).Scan(&status); err != nil {
				t.Fatalf("read back issue: %v", err)
			}
			if status != terminal {
				t.Fatalf("leader's terminal status was clobbered by a stale sweeper reset: want %q, got %q", terminal, status)
			}
		})
	}
}

// TestSweeperRollbackCAS_StillRollsBackStuckInProgress confirms the happy path:
// when the issue really is still 'in_progress' (no concurrent leader write),
// the CAS reset to 'todo' fires exactly as before so a genuinely stuck issue is
// re-queued for the daemon.
func TestSweeperRollbackCAS_StillRollsBackStuckInProgress(t *testing.T) {
	if testPool == nil {
		t.Skip("no database connection")
	}
	ctx := context.Background()
	queries := db.New(testPool)

	issueID := seedIssueWithStatus(t, "in_progress")

	rows, err := queries.UpdateIssueStatusIfCurrent(ctx, db.UpdateIssueStatusIfCurrentParams{
		NewStatus:      "todo",
		ID:             parseUUID(issueID),
		WorkspaceID:    parseUUID(testWorkspaceID),
		ExpectedStatus: "in_progress",
	})
	if err != nil {
		t.Fatalf("UpdateIssueStatusIfCurrent: %v", err)
	}
	if rows != 1 {
		t.Fatalf("CAS must roll back a still-in_progress issue; updated %d rows", rows)
	}

	var status string
	if err := testPool.QueryRow(ctx, `SELECT status FROM issue WHERE id = $1`, issueID).Scan(&status); err != nil {
		t.Fatalf("read back issue: %v", err)
	}
	if status != "todo" {
		t.Fatalf("stuck in_progress issue not rolled back: want todo, got %q", status)
	}
}
