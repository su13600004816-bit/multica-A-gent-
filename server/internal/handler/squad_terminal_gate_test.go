package handler

import (
	"context"
	"testing"

	"github.com/multica-ai/multica/server/internal/util"
	db "github.com/multica-ai/multica/server/pkg/db/generated"
)

// TestShouldEnqueueSquadLeaderOnComment_SkipsTerminalIssue locks the
// terminal-status gate that stops the squad self-ignition loop. A done or
// cancelled squad issue must NOT wake its leader on a routine (non-@mention)
// comment -- otherwise an automated watchdog posting status notes to a
// terminal squad issue re-triggers the squad every cycle (observed in
// production: a cancelled "watchdog board" issue accrued 555 leader runs at a
// ~2-minute cadence). Live statuses (in_progress / in_review here, and the
// fixture default backlog covered by the sibling tests) must still wake the
// leader so legitimate nudges keep working.
func TestShouldEnqueueSquadLeaderOnComment_SkipsTerminalIssue(t *testing.T) {
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
		{status: "in_progress", want: true}, // control: live issue still wakes leader
		{status: "in_review", want: true},   // non-terminal review state still wakes
		{status: "done", want: false},       // terminal: must not wake (anti-loop)
		{status: "cancelled", want: false},  // terminal: must not wake (anti-loop)
	}
	for _, tc := range cases {
		t.Run(tc.status, func(t *testing.T) {
			issue := setStatus(tc.status)
			// Plain member comment, no @mention -- the exact shape an automated
			// watchdog status note takes (issue cross-refs only, no routing).
			got := testHandler.shouldEnqueueSquadLeaderOnComment(ctx, issue, "watchdog: please advance", "member", testUserID)
			if got != tc.want {
				t.Fatalf("status=%s: shouldEnqueueSquadLeaderOnComment = %v, want %v", tc.status, got, tc.want)
			}
		})
	}
}
