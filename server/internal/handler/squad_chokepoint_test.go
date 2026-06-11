package handler

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/jackc/pgx/v5/pgtype"

	"github.com/multica-ai/multica/server/internal/util"
	db "github.com/multica-ai/multica/server/pkg/db/generated"
)

// TestEnqueueTaskForSquadLeader_TerminalIssueIsNoOp proves the authoritative
// invariant at the single squad-leader enqueue chokepoint: a done/cancelled
// issue produces NO leader task and NO error (silent no-op), while a live
// issue still enqueues. Because every squad-leader trigger funnels through
// this method, this is what makes squad self-ignition on a terminal issue
// structurally impossible — not the per-caller gates.
func TestEnqueueTaskForSquadLeader_TerminalIssueIsNoOp(t *testing.T) {
	if testHandler == nil || testPool == nil {
		t.Skip("database not available")
	}
	fx := newSquadCommentTriggerFixture(t)
	ctx := context.Background()
	issueID := uuidToString(fx.Issue.ID)
	leaderUUID := util.MustParseUUID(fx.LeaderID)

	t.Cleanup(func() {
		testPool.Exec(context.Background(), `DELETE FROM agent_task_queue WHERE issue_id = $1`, issueID)
	})
	countTasks := func() int {
		var n int
		if err := testPool.QueryRow(ctx, `SELECT count(*) FROM agent_task_queue WHERE issue_id = $1`, issueID).Scan(&n); err != nil {
			t.Fatalf("count tasks: %v", err)
		}
		return n
	}
	setStatus := func(status string) db.Issue {
		if _, err := testPool.Exec(ctx, `UPDATE issue SET status = $1 WHERE id = $2`, status, issueID); err != nil {
			t.Fatalf("set status %s: %v", status, err)
		}
		issue, err := testHandler.Queries.GetIssue(ctx, util.MustParseUUID(issueID))
		if err != nil {
			t.Fatalf("reload issue: %v", err)
		}
		return issue
	}

	for _, status := range []string{"done", "cancelled"} {
		t.Run("terminal/"+status, func(t *testing.T) {
			testPool.Exec(ctx, `DELETE FROM agent_task_queue WHERE issue_id = $1`, issueID)
			issue := setStatus(status)
			task, err := testHandler.TaskService.EnqueueTaskForSquadLeader(ctx, issue, leaderUUID, pgtype.UUID{})
			if err != nil {
				t.Fatalf("%s: expected nil error (silent no-op), got %v", status, err)
			}
			if task.ID.Valid {
				t.Fatalf("%s: expected zero-value task, got id=%s", status, uuidToString(task.ID))
			}
			if n := countTasks(); n != 0 {
				t.Fatalf("%s: expected 0 task rows, got %d", status, n)
			}
		})
	}

	t.Run("live/in_progress enqueues", func(t *testing.T) {
		testPool.Exec(ctx, `DELETE FROM agent_task_queue WHERE issue_id = $1`, issueID)
		issue := setStatus("in_progress")
		task, err := testHandler.TaskService.EnqueueTaskForSquadLeader(ctx, issue, leaderUUID, pgtype.UUID{})
		if err != nil || !task.ID.Valid {
			t.Fatalf("in_progress: expected a real task, got id_valid=%v err=%v", task.ID.Valid, err)
		}
		if n := countTasks(); n != 1 {
			t.Fatalf("in_progress: expected 1 task row, got %d", n)
		}
	})
}

// TestCreateComment_SquadMentionOnTerminalIssueIsNoOp proves the escape hatch
// the per-caller gates left open is now closed end-to-end: an explicit @squad
// mention on a done/cancelled issue no longer wakes the squad leader, because
// the mention path routes through EnqueueTaskForSquadLeader. Reopen the issue
// to act on it.
func TestCreateComment_SquadMentionOnTerminalIssueIsNoOp(t *testing.T) {
	if testHandler == nil || testPool == nil {
		t.Skip("database not available")
	}
	ctx := context.Background()
	fx := newSquadCommentTriggerFixture(t)
	issueID := uuidToString(fx.Issue.ID)

	t.Cleanup(func() {
		testPool.Exec(context.Background(), `DELETE FROM agent_task_queue WHERE issue_id = $1`, issueID)
		testPool.Exec(context.Background(), `DELETE FROM comment WHERE issue_id = $1`, issueID)
	})
	if _, err := testPool.Exec(ctx, `UPDATE issue SET status = 'cancelled' WHERE id = $1`, issueID); err != nil {
		t.Fatalf("cancel issue: %v", err)
	}

	w := httptest.NewRecorder()
	r := newRequest("POST", "/api/issues/"+issueID+"/comments", map[string]any{
		"content": "[@Squad](mention://squad/" + fx.SquadID + ") please advance",
	})
	r = withURLParam(r, "id", issueID)
	testHandler.CreateComment(w, r)
	if w.Code != http.StatusCreated {
		t.Fatalf("CreateComment: expected 201, got %d: %s", w.Code, w.Body.String())
	}
	var resp CommentResponse
	_ = json.NewDecoder(w.Body).Decode(&resp)

	var leaderTasks int
	if err := testPool.QueryRow(ctx,
		`SELECT count(*) FROM agent_task_queue WHERE issue_id = $1 AND agent_id = $2`,
		issueID, fx.LeaderID,
	).Scan(&leaderTasks); err != nil {
		t.Fatalf("count leader tasks: %v", err)
	}
	if leaderTasks != 0 {
		t.Fatalf("@squad mention on cancelled issue: expected 0 leader tasks (chokepoint no-op), got %d", leaderTasks)
	}
}
