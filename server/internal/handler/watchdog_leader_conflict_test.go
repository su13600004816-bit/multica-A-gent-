package handler

import (
	"context"
	"testing"

	"github.com/multica-ai/multica/server/internal/util"
)

// countTasksForTriggerComment returns how many tasks (in ANY status) a single
// comment has produced for a given agent — the assertion lens for the
// trigger-comment single-execution lock, which keys on (trigger_comment_id,
// agent_id) regardless of status.
func countTasksForTriggerComment(t *testing.T, commentID, agentID string) int {
	t.Helper()
	var n int
	if err := testPool.QueryRow(context.Background(), `
		SELECT count(*) FROM agent_task_queue
		WHERE trigger_comment_id = $1 AND agent_id = $2
	`, util.MustParseUUID(commentID), util.MustParseUUID(agentID)).Scan(&n); err != nil {
		t.Fatalf("count tasks for trigger comment: %v", err)
	}
	return n
}

// seedIssueComment inserts a member-authored comment on the issue and returns
// its UUID, registering cleanup.
func seedIssueComment(t *testing.T, issueID, content string) string {
	t.Helper()
	var commentID string
	if err := testPool.QueryRow(context.Background(), `
		INSERT INTO comment (workspace_id, issue_id, author_type, author_id, content)
		VALUES ($1, $2, 'member', $3, $4)
		RETURNING id
	`, testWorkspaceID, issueID, testUserID, content).Scan(&commentID); err != nil {
		t.Fatalf("seed comment: %v", err)
	}
	t.Cleanup(func() {
		testPool.Exec(context.Background(), `DELETE FROM comment WHERE id = $1`, commentID)
	})
	return commentID
}

// TestTriggerComment_SingleExecutionLock is the regression test for SPEC §6
// conflict B (scenario ①): the leader→@agent→leader re-dispatch storm. Once a
// trigger comment has produced a task for the squad leader, that SAME comment
// re-entering the enqueue path (a watchdog re-route onto a PASS comment, or a
// comment edit) must NOT enqueue a second run — even after the first task has
// completed and the queued/dispatched dedup no longer applies. The
// (trigger_comment_id, agent_id) single-execution lock is the structural guard
// that stops one comment from pulling round after round.
func TestTriggerComment_SingleExecutionLock(t *testing.T) {
	if testHandler == nil || testPool == nil {
		t.Skip("database not available")
	}
	ctx := context.Background()
	fx := newSquadCommentTriggerFixture(t)

	commentID := seedIssueComment(t, uuidToString(fx.Issue.ID), "audit PASS, proceed")

	// First trigger: the comment enqueues exactly one leader task.
	testHandler.enqueueSquadLeaderTask(ctx, fx.Issue, util.MustParseUUID(commentID), "member", testUserID)
	if got := countTasksForTriggerComment(t, commentID, fx.LeaderID); got != 1 {
		t.Fatalf("first trigger: expected 1 leader task, got %d", got)
	}

	// Complete the task so the queued/dispatched dedup no longer blocks — from
	// here only the trigger-comment lock can prevent a second enqueue.
	if _, err := testPool.Exec(ctx, `
		UPDATE agent_task_queue SET status = 'completed', completed_at = now()
		WHERE trigger_comment_id = $1 AND agent_id = $2
	`, util.MustParseUUID(commentID), util.MustParseUUID(fx.LeaderID)); err != nil {
		t.Fatalf("complete first task: %v", err)
	}

	// Re-trigger from the SAME comment: must NOT create a second task.
	testHandler.enqueueSquadLeaderTask(ctx, fx.Issue, util.MustParseUUID(commentID), "member", testUserID)
	if got := countTasksForTriggerComment(t, commentID, fx.LeaderID); got != 1 {
		t.Fatalf("re-trigger from the same comment must not re-dispatch the leader; got %d total tasks", got)
	}
}

// TestShouldEnqueueSquadLeaderOnAssign_SuppressesLeaderSelfAssign is the
// regression test for SPEC §6 conflict D (scenario ③): the assign-path
// self-trigger guard. When the actor assigning work to a squad IS that squad's
// own leader agent, auto-firing the leader is a self-dispatch and must be
// suppressed. A human, or any agent other than the leader, must still trigger
// the leader. This mirrors the comment-path lastTaskWasLeader guard but keys on
// actor identity, and is kept independent of the comment path.
func TestShouldEnqueueSquadLeaderOnAssign_SuppressesLeaderSelfAssign(t *testing.T) {
	if testHandler == nil || testPool == nil {
		t.Skip("database not available")
	}
	ctx := context.Background()
	fx := newSquadCommentTriggerFixture(t)

	// The fixture issue defaults to 'backlog'; lift it out of the parking lot
	// so the backlog short-circuit doesn't mask the self-trigger guard.
	issue := fx.Issue
	issue.Status = "todo"

	if got := testHandler.shouldEnqueueSquadLeaderOnAssign(ctx, issue, "agent", fx.LeaderID); got {
		t.Fatalf("leader assigning work onto its own squad must NOT auto-fire itself (self-dispatch); got true")
	}

	// A human assigning to the squad still triggers the leader.
	if got := testHandler.shouldEnqueueSquadLeaderOnAssign(ctx, issue, "member", testUserID); !got {
		t.Fatalf("a human assigning to the squad must still trigger the leader; got false")
	}

	// A different agent (not this squad's leader) still triggers the leader.
	if got := testHandler.shouldEnqueueSquadLeaderOnAssign(ctx, issue, "agent", fx.OtherID); !got {
		t.Fatalf("an agent other than the squad leader must still trigger the leader; got false")
	}
}
