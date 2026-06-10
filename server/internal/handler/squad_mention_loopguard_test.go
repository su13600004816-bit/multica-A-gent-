package handler

import (
	"context"
	"testing"

	"github.com/multica-ai/multica/server/internal/util"
	db "github.com/multica-ai/multica/server/pkg/db/generated"
)

// squadMentionLoopFixture wires a squad (with the seeded agent as a registered
// leader-role member), an issue assigned to that squad, and a separate
// agent-assigned issue. It exercises inSquadAgentMentionSuppressed via the
// real enqueueMentionedAgentTasks path: an in-squad agent's direct @agent
// mention on the squad's own issue must NOT enqueue a personal run (the
// leader→@agent→run→leader anti-loop), while the same mention on a non-squad
// issue, and a non-member agent's mention on the squad issue, still enqueue.
type squadMentionLoopFixture struct {
	SquadID    string
	LeaderID   string // in-squad agent (registered squad_member, role=leader)
	OutsiderID string // agent in the workspace but NOT a member of the squad
	TargetID   string // a third agent used purely as a mention target (has runtime)
	SquadIssue db.Issue
	AgentIssue db.Issue // assigned to LeaderID directly (non-squad control)
}

func newSquadMentionLoopFixture(t *testing.T) squadMentionLoopFixture {
	t.Helper()
	ctx := context.Background()

	// Reuse the seeded "Handler Test Agent" as the squad leader — it has a runtime.
	var leaderID string
	if err := testPool.QueryRow(ctx, `
		SELECT id FROM agent WHERE workspace_id = $1 ORDER BY created_at ASC LIMIT 1
	`, testWorkspaceID).Scan(&leaderID); err != nil {
		t.Fatalf("load leader agent: %v", err)
	}

	outsiderID := createHandlerTestAgent(t, "Loopguard Outsider", nil)
	targetID := createHandlerTestAgent(t, "Loopguard Target", nil)

	var squadID string
	if err := testPool.QueryRow(ctx, `
		INSERT INTO squad (workspace_id, name, description, leader_id, creator_id)
		VALUES ($1, $2, '', $3, $4)
		RETURNING id
	`, testWorkspaceID, "Loopguard Squad", leaderID, testUserID).Scan(&squadID); err != nil {
		t.Fatalf("create squad: %v", err)
	}
	t.Cleanup(func() {
		testPool.Exec(context.Background(), `DELETE FROM squad WHERE id = $1`, squadID)
	})

	// Mirror CreateSquad: the leader is a registered squad member (role=leader).
	// inSquadAgentMentionSuppressed keys off squad_member, so without this the
	// leader would not be recognized as in-squad.
	if _, err := testPool.Exec(ctx, `
		INSERT INTO squad_member (squad_id, member_type, member_id, role)
		VALUES ($1, 'agent', $2, 'leader')
	`, squadID, leaderID); err != nil {
		t.Fatalf("register leader as squad member: %v", err)
	}

	insertIssue := func(title, assigneeType, assigneeID string) db.Issue {
		t.Helper()
		var number int
		if err := testPool.QueryRow(ctx, `
			UPDATE workspace
			SET issue_counter = GREATEST(issue_counter, (SELECT COALESCE(MAX(number), 0) FROM issue WHERE workspace_id = $1)) + 1
			WHERE id = $1 RETURNING issue_counter
		`, testWorkspaceID).Scan(&number); err != nil {
			t.Fatalf("next issue number: %v", err)
		}
		var id string
		if err := testPool.QueryRow(ctx, `
			INSERT INTO issue (workspace_id, creator_type, creator_id, title, assignee_type, assignee_id, number)
			VALUES ($1, 'member', $2, $3, $4, $5, $6)
			RETURNING id
		`, testWorkspaceID, testUserID, title, assigneeType, assigneeID, number).Scan(&id); err != nil {
			t.Fatalf("create issue %q: %v", title, err)
		}
		t.Cleanup(func() {
			testPool.Exec(context.Background(), `DELETE FROM agent_task_queue WHERE issue_id = $1`, id)
			testPool.Exec(context.Background(), `DELETE FROM comment WHERE issue_id = $1`, id)
			testPool.Exec(context.Background(), `DELETE FROM issue WHERE id = $1`, id)
		})
		issue, err := testHandler.Queries.GetIssue(ctx, util.MustParseUUID(id))
		if err != nil {
			t.Fatalf("load issue %q: %v", title, err)
		}
		return issue
	}

	squadIssue := insertIssue("loopguard squad issue", "squad", squadID)
	agentIssue := insertIssue("loopguard agent issue", "agent", leaderID)

	return squadMentionLoopFixture{
		SquadID:    squadID,
		LeaderID:   leaderID,
		OutsiderID: outsiderID,
		TargetID:   targetID,
		SquadIssue: squadIssue,
		AgentIssue: agentIssue,
	}
}

// makeAgentComment seeds an agent-authored comment with the given content on
// the given issue and returns the loaded db.Comment.
func makeAgentComment(t *testing.T, issueID, authorID, content string) db.Comment {
	t.Helper()
	ctx := context.Background()
	var id string
	if err := testPool.QueryRow(ctx, `
		INSERT INTO comment (workspace_id, issue_id, author_type, author_id, content)
		VALUES ($1, $2, 'agent', $3, $4)
		RETURNING id
	`, testWorkspaceID, issueID, authorID, content).Scan(&id); err != nil {
		t.Fatalf("create comment: %v", err)
	}
	c, err := testHandler.Queries.GetComment(ctx, util.MustParseUUID(id))
	if err != nil {
		t.Fatalf("load comment: %v", err)
	}
	return c
}

func agentMention(id string) string {
	return "[@A](mention://agent/" + id + ") please take this"
}

// TestInSquadAgentMention_SuppressedOnSquadIssue is the core anti-loop
// regression: when an agent that belongs to the squad owning the issue posts a
// comment that directly @mentions another agent, NO personal run is enqueued.
// Squad-internal dispatch must flow through squad assignment / the leader, not
// personal mentions — otherwise a leader@worker mention spawns a run that wakes
// the leader again and the cycle (which also overwrites a recorded PASS) never
// settles.
func TestInSquadAgentMention_SuppressedOnSquadIssue(t *testing.T) {
	if testHandler == nil || testPool == nil {
		t.Skip("database not available")
	}
	ctx := context.Background()
	fx := newSquadMentionLoopFixture(t)

	comment := makeAgentComment(t, uuidToString(fx.SquadIssue.ID), fx.LeaderID, agentMention(fx.TargetID))

	if got := countQueuedOrDispatched(t, fx.TargetID, uuidToString(fx.SquadIssue.ID)); got != 0 {
		t.Fatalf("before: expected 0 pending tasks for target, got %d", got)
	}

	testHandler.enqueueMentionedAgentTasks(ctx, fx.SquadIssue, comment, nil, "agent", fx.LeaderID)

	if got := countQueuedOrDispatched(t, fx.TargetID, uuidToString(fx.SquadIssue.ID)); got != 0 {
		t.Fatalf("in-squad agent @agent mention on squad issue must NOT enqueue; got %d", got)
	}
}

// TestInSquadAgentMention_NotSuppressedOnNonSquadIssue proves the guard is
// scoped to squad-owned issues. The same in-squad agent author @mentioning the
// same target on an agent-assigned (non-squad) issue still enqueues — personal
// issues keep the normal agent→agent delegation behavior.
func TestInSquadAgentMention_NotSuppressedOnNonSquadIssue(t *testing.T) {
	if testHandler == nil || testPool == nil {
		t.Skip("database not available")
	}
	ctx := context.Background()
	fx := newSquadMentionLoopFixture(t)

	comment := makeAgentComment(t, uuidToString(fx.AgentIssue.ID), fx.LeaderID, agentMention(fx.TargetID))

	testHandler.enqueueMentionedAgentTasks(ctx, fx.AgentIssue, comment, nil, "agent", fx.LeaderID)

	if got := countQueuedOrDispatched(t, fx.TargetID, uuidToString(fx.AgentIssue.ID)); got != 1 {
		t.Fatalf("agent @agent mention on a non-squad issue must enqueue; got %d", got)
	}
}

// TestInSquadAgentMention_NotSuppressedForOutsideAgent proves the guard only
// silences authors that belong to the owning squad. An agent that is NOT a
// member of the squad still enqueues a personal mention on the squad's issue —
// the suppression is about squad-internal dispatch discipline, not a blanket
// ban on mentions touching squad issues.
func TestInSquadAgentMention_NotSuppressedForOutsideAgent(t *testing.T) {
	if testHandler == nil || testPool == nil {
		t.Skip("database not available")
	}
	ctx := context.Background()
	fx := newSquadMentionLoopFixture(t)

	comment := makeAgentComment(t, uuidToString(fx.SquadIssue.ID), fx.OutsiderID, agentMention(fx.TargetID))

	testHandler.enqueueMentionedAgentTasks(ctx, fx.SquadIssue, comment, nil, "agent", fx.OutsiderID)

	if got := countQueuedOrDispatched(t, fx.TargetID, uuidToString(fx.SquadIssue.ID)); got != 1 {
		t.Fatalf("non-member agent @agent mention on squad issue must enqueue; got %d", got)
	}
}
