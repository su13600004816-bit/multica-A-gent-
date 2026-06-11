package handler

import (
	"context"
	"testing"

	"github.com/jackc/pgx/v5/pgtype"

	"github.com/multica-ai/multica/server/internal/util"
	db "github.com/multica-ai/multica/server/pkg/db/generated"
)

// squadIssue builds a db.Issue assigned to the given squad UUID, in the given
// status. Pure constructor — no DB — so the routing-invariant unit test below
// runs without a database connection.
func squadIssue(squadID pgtype.UUID, status string) db.Issue {
	return db.Issue{
		ID:           util.MustParseUUID("00000000-0000-0000-0000-0000000000aa"),
		WorkspaceID:  util.MustParseUUID("00000000-0000-0000-0000-0000000000bb"),
		Status:       status,
		AssigneeType: pgtype.Text{String: "squad", Valid: true},
		AssigneeID:   squadID,
	}
}

// TestIssueOwningSquadID_RoutesBackToOwningSquadNeverCrossTeam is the pure-unit
// regression assertion for PL-145 §7/§8: a watchdog re-route ("回本队") must
// resolve to the squad that OWNS the issue and NEVER to a foreign mechanism
// line (T01/T02/T03). issueOwningSquadID is the single source of truth for the
// re-route target, so pinning it here proves the invariant without needing a
// database. A C-series anomaly issue resolves to its own C squad; the T-series
// squad UUIDs are never reachable because the only squad ever consulted is the
// issue's own assignee.
func TestIssueOwningSquadID_RoutesBackToOwningSquadNeverCrossTeam(t *testing.T) {
	cSquad := util.MustParseUUID("c0000000-0000-0000-0000-0000000000c2") // owning C02 squad
	t01 := util.MustParseUUID("70000000-0000-0000-0000-000000000001")
	t02 := util.MustParseUUID("70000000-0000-0000-0000-000000000002")
	t03 := util.MustParseUUID("70000000-0000-0000-0000-000000000003")

	got, ok := issueOwningSquadID(squadIssue(cSquad, "in_review"))
	if !ok {
		t.Fatalf("C-series anomaly issue must resolve a re-route target; got ok=false")
	}
	// Re-route target is exactly the owning C squad.
	if uuidToString(got) != uuidToString(cSquad) {
		t.Fatalf("re-route target = %s, want owning C squad %s", uuidToString(got), uuidToString(cSquad))
	}
	// And is none of the T-series mechanism lines (no cross-team route).
	for name, tSquad := range map[string]pgtype.UUID{"T01": t01, "T02": t02, "T03": t03} {
		if uuidToString(got) == uuidToString(tSquad) {
			t.Fatalf("C-series anomaly must not route to %s; got %s", name, uuidToString(got))
		}
	}

	// A non-squad assignee has no owning squad → no re-route target at all
	// (the watchdog cannot invent a T-series destination out of nothing).
	for _, bad := range []db.Issue{
		{AssigneeType: pgtype.Text{String: "agent", Valid: true}, AssigneeID: t01},
		{AssigneeType: pgtype.Text{Valid: false}},
		{AssigneeType: pgtype.Text{String: "squad", Valid: true}, AssigneeID: pgtype.UUID{Valid: false}},
	} {
		if _, ok := issueOwningSquadID(bad); ok {
			t.Fatalf("non-squad-owned issue must not resolve a re-route target: %+v", bad.AssigneeType)
		}
	}
}

// TestWatchdogReroute_CSeriesAnomalyReturnsToOwningSquad is the integration
// regression assertion for PL-145 §8: construct a C-series anomaly event (a
// watchdog re-route comment on an issue OWNED by a C squad, while a separate
// "T03" mechanism squad also exists in the workspace) and assert the enqueue
// lands on the owning C squad's leader and produces ZERO cross-team tasks for
// the T squad's leader.
func TestWatchdogReroute_CSeriesAnomalyReturnsToOwningSquad(t *testing.T) {
	if testHandler == nil || testPool == nil {
		t.Skip("database not available")
	}
	ctx := context.Background()

	// fx.Issue is owned by fx.SquadID (the "C02" owning squad), led by
	// fx.LeaderID.
	fx := newSquadCommentTriggerFixture(t)

	// Stand up a separate "T03" mechanism-line squad in the same workspace,
	// led by a different agent. This is the squad the broken watchdog used to
	// dump C-series audits onto; it must receive nothing.
	tLeaderID := createHandlerTestAgent(t, "Mechanism Line T03 Leader", nil)
	var tSquadID string
	if err := testPool.QueryRow(ctx, `
		INSERT INTO squad (workspace_id, name, description, leader_id, creator_id)
		VALUES ($1, $2, '', $3, $4)
		RETURNING id
	`, testWorkspaceID, "Mechanism Line T03", tLeaderID, testUserID).Scan(&tSquadID); err != nil {
		t.Fatalf("create T03 squad: %v", err)
	}
	t.Cleanup(func() {
		testPool.Exec(context.Background(), `DELETE FROM squad WHERE id = $1`, tSquadID)
	})

	// The watchdog anomaly: a stage-stall re-route comment on the C-owned
	// issue, authored by the workspace member (the watchdog posts as a member).
	commentID := seedIssueComment(t, uuidToString(fx.Issue.ID),
		"🚨 看门狗自动分流: in_review 阶段推进超时, 重新交回本队")

	testHandler.enqueueSquadLeaderTask(ctx, fx.Issue, util.MustParseUUID(commentID), "member", testUserID)

	// The owning C squad's leader gets exactly one task (回本队).
	if got := countTasksForTriggerComment(t, commentID, fx.LeaderID); got != 1 {
		t.Fatalf("C-series anomaly must route back to the owning squad leader; got %d tasks", got)
	}
	// The T03 mechanism-line leader gets nothing — no cross-team route.
	if got := countTasksForTriggerComment(t, commentID, tLeaderID); got != 0 {
		t.Fatalf("C-series anomaly must NOT route to a T-series squad; T03 leader got %d tasks", got)
	}
}
