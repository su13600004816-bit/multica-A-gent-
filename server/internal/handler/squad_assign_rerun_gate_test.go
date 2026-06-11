package handler

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/multica-ai/multica/server/internal/util"
	db "github.com/multica-ai/multica/server/pkg/db/generated"
)

// TestShouldEnqueueSquadLeaderOnAssign_SkipsTerminalIssue locks the second
// self-ignition door: a (re)assignment of a done/cancelled squad issue must
// NOT auto-wake the leader. Live statuses still wake it.
func TestShouldEnqueueSquadLeaderOnAssign_SkipsTerminalIssue(t *testing.T) {
	if testHandler == nil || testPool == nil {
		t.Skip("database not available")
	}
	fx := newSquadCommentTriggerFixture(t)
	ctx := context.Background()
	issueID := uuidToString(fx.Issue.ID)

	setStatus := func(status string) db.Issue {
		t.Helper()
		if _, err := testPool.Exec(ctx, `UPDATE issue SET status = $1 WHERE id = $2`, status, issueID); err != nil {
			t.Fatalf("set status %s: %v", status, err)
		}
		issue, err := testHandler.Queries.GetIssue(ctx, util.MustParseUUID(issueID))
		if err != nil {
			t.Fatalf("reload issue: %v", err)
		}
		return issue
	}

	cases := []struct {
		status string
		want   bool
	}{
		{status: "in_progress", want: true}, // control: live issue wakes leader on assign
		{status: "todo", want: true},        // control
		{status: "done", want: false},       // terminal: must not wake
		{status: "cancelled", want: false},  // terminal: must not wake
	}
	for _, tc := range cases {
		t.Run(tc.status, func(t *testing.T) {
			issue := setStatus(tc.status)
			// Member actor that is not the squad leader -> only the status gate
			// and leader-readiness decide the outcome.
			got := testHandler.shouldEnqueueSquadLeaderOnAssign(ctx, issue, "member", testUserID)
			if got != tc.want {
				t.Fatalf("status=%s: shouldEnqueueSquadLeaderOnAssign = %v, want %v", tc.status, got, tc.want)
			}
		})
	}
}

// TestRerunIssue_RefusesCancelledIssue locks the third self-ignition door:
// POST /api/issues/{id}/rerun on a cancelled issue must be refused with 400,
// so the watchdog's route_to_squad rerun cannot reignite a cancelled squad
// board after the on-comment gate blocks it. A live issue still reruns.
func TestRerunIssue_RefusesCancelledIssue(t *testing.T) {
	if testHandler == nil || testPool == nil {
		t.Skip("database not available")
	}
	fx := newSquadCommentTriggerFixture(t)
	ctx := context.Background()
	issueID := uuidToString(fx.Issue.ID)

	t.Cleanup(func() {
		testPool.Exec(context.Background(), `DELETE FROM agent_task_queue WHERE issue_id = $1`, issueID)
	})

	setStatus := func(status string) {
		if _, err := testPool.Exec(ctx, `UPDATE issue SET status = $1 WHERE id = $2`, status, issueID); err != nil {
			t.Fatalf("set status %s: %v", status, err)
		}
	}
	rerun := func() int {
		w := httptest.NewRecorder()
		r := newRequest("POST", "/api/issues/"+issueID+"/rerun", nil)
		r = withURLParam(r, "id", issueID)
		testHandler.RerunIssue(w, r)
		return w.Code
	}

	setStatus("cancelled")
	if code := rerun(); code != http.StatusBadRequest {
		t.Fatalf("rerun cancelled issue: expected 400, got %d", code)
	}

	// Control: a live issue still accepts rerun (202 Accepted).
	setStatus("in_progress")
	if code := rerun(); code != http.StatusAccepted {
		t.Fatalf("rerun in_progress issue: expected 202, got %d", code)
	}
}
