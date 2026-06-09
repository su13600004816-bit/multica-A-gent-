package handler

import (
	"context"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgtype"
	"github.com/multica-ai/multica/server/internal/service"
	db "github.com/multica-ai/multica/server/pkg/db/generated"
)

// stageOrchFixture wires up a governing squad with audit/gate/deepdig/overseer
// roles plus a dev agent, a dev issue assigned to the dev agent, and an enabled
// workspace config. The dev issue's squad is resolved via the dev agent's squad
// membership.
type stageOrchFixture struct {
	Orch     *service.StageOrchestrator
	Issue    db.Issue
	SquadID  pgtype.UUID
	DevID    pgtype.UUID
	AuditID  pgtype.UUID
	GateID   pgtype.UUID
	DigID    pgtype.UUID
	LeaderID pgtype.UUID
	OverID   pgtype.UUID
}

func newStageOrchFixture(t *testing.T) stageOrchFixture {
	t.Helper()
	if testHandler == nil || testPool == nil || testHandler.Orchestrator == nil {
		t.Skip("database / orchestrator not available")
	}
	ctx := context.Background()

	dev := parseUUID(createHandlerTestAgent(t, "orch-dev-"+randSuffix(t), nil))
	auditor := parseUUID(createHandlerTestAgent(t, "orch-audit-"+randSuffix(t), nil))
	gate := parseUUID(createHandlerTestAgent(t, "orch-gate-"+randSuffix(t), nil))
	dig := parseUUID(createHandlerTestAgent(t, "orch-dig-"+randSuffix(t), nil))
	leader := parseUUID(createHandlerTestAgent(t, "orch-leader-"+randSuffix(t), nil))
	overseer := parseUUID(createHandlerTestAgent(t, "orch-over-"+randSuffix(t), nil))

	var squadID string
	if err := testPool.QueryRow(ctx, `
		INSERT INTO squad (workspace_id, name, leader_id, creator_id)
		VALUES ($1, $2, $3, $4) RETURNING id
	`, testWorkspaceID, "orch-squad-"+randSuffix(t), uuidToString(leader), testUserID).Scan(&squadID); err != nil {
		t.Fatalf("create squad: %v", err)
	}
	t.Cleanup(func() { testPool.Exec(context.Background(), `DELETE FROM squad WHERE id = $1`, squadID) })

	addMember := func(agentID pgtype.UUID, role string) {
		if _, err := testPool.Exec(ctx, `
			INSERT INTO squad_member (squad_id, member_type, member_id, role)
			VALUES ($1, 'agent', $2, $3)
		`, squadID, uuidToString(agentID), role); err != nil {
			t.Fatalf("add member %s: %v", role, err)
		}
	}
	addMember(dev, "开发")
	addMember(auditor, "审计 audit")
	addMember(gate, "门禁 gate")
	addMember(dig, "深挖 deepdig")
	addMember(overseer, "总管 overseer")

	// Dev issue assigned to the dev agent.
	var issueID string
	if err := testPool.QueryRow(ctx, `
		INSERT INTO issue (workspace_id, title, status, creator_type, creator_id, assignee_type, assignee_id)
		VALUES ($1, $2, 'in_progress', 'member', $3, 'agent', $4) RETURNING id
	`, testWorkspaceID, "orch dev issue", testUserID, uuidToString(dev)).Scan(&issueID); err != nil {
		t.Fatalf("create issue: %v", err)
	}
	t.Cleanup(func() { testPool.Exec(context.Background(), `DELETE FROM issue WHERE id = $1`, issueID) })

	// Enable the orchestrator for the workspace (opt-in).
	if _, err := testPool.Exec(ctx, `
		INSERT INTO stage_orchestrator_config (workspace_id, agent_id, enabled, actions)
		VALUES ($1, NULL, TRUE, '{}'::jsonb)
		ON CONFLICT (workspace_id, COALESCE(agent_id, '00000000-0000-0000-0000-000000000000'::uuid))
		DO UPDATE SET enabled = TRUE, actions = '{}'::jsonb
	`, testWorkspaceID); err != nil {
		t.Fatalf("enable config: %v", err)
	}
	t.Cleanup(func() {
		testPool.Exec(context.Background(), `DELETE FROM stage_orchestrator_config WHERE workspace_id = $1`, testWorkspaceID)
		testPool.Exec(context.Background(), `DELETE FROM stage_orchestration WHERE issue_id = $1`, issueID)
	})

	issue, err := testHandler.Queries.GetIssue(ctx, parseUUID(issueID))
	if err != nil {
		t.Fatalf("get issue: %v", err)
	}

	return stageOrchFixture{
		Orch:     testHandler.Orchestrator,
		Issue:    issue,
		SquadID:  parseUUID(squadID),
		DevID:    dev,
		AuditID:  auditor,
		GateID:   gate,
		DigID:    dig,
		LeaderID: leader,
		OverID:   overseer,
	}
}

func randSuffix(t *testing.T) string {
	t.Helper()
	return randomID()[:8]
}

// pendingCount returns the number of queued/dispatched tasks an agent has on an issue.
func pendingCount(t *testing.T, issueID, agentID pgtype.UUID) int {
	t.Helper()
	var n int
	if err := testPool.QueryRow(context.Background(), `
		SELECT count(*) FROM agent_task_queue
		WHERE issue_id = $1 AND agent_id = $2 AND status IN ('queued', 'dispatched')
	`, issueID, agentID).Scan(&n); err != nil {
		t.Fatalf("count tasks: %v", err)
	}
	return n
}

func currentStage(t *testing.T, issueID pgtype.UUID) string {
	t.Helper()
	var stage string
	if err := testPool.QueryRow(context.Background(),
		`SELECT stage FROM stage_orchestration WHERE issue_id = $1`, issueID).Scan(&stage); err != nil {
		t.Fatalf("get stage: %v", err)
	}
	return stage
}

func systemCommentCount(t *testing.T, issueID pgtype.UUID) int {
	t.Helper()
	var n int
	if err := testPool.QueryRow(context.Background(),
		`SELECT count(*) FROM comment WHERE issue_id = $1 AND author_type = 'system'`, issueID).Scan(&n); err != nil {
		t.Fatalf("count system comments: %v", err)
	}
	return n
}

// inReview drives a status transition into in_review through the orchestrator.
func (fx stageOrchFixture) inReview(t *testing.T) {
	t.Helper()
	ctx := context.Background()
	prev := fx.Issue
	if _, err := testPool.Exec(ctx, `UPDATE issue SET status = 'in_review' WHERE id = $1`, fx.Issue.ID); err != nil {
		t.Fatalf("set in_review: %v", err)
	}
	issue, err := testHandler.Queries.GetIssue(ctx, fx.Issue.ID)
	if err != nil {
		t.Fatalf("reload issue: %v", err)
	}
	fx.Orch.OnIssueStatusChanged(ctx, prev, issue)
}

// verdict posts an agent VERDICT comment and runs it through the orchestrator.
// Posting a verdict means the author's run is finishing, so its task on the
// issue is completed first — this mirrors production and keeps the
// HasPendingTaskForIssueAndAgent dedup honest (e.g. so a re-review can be
// re-dispatched to the same auditor whose first audit task has finished).
func (fx stageOrchFixture) verdict(t *testing.T, author pgtype.UUID, body string) {
	t.Helper()
	ctx := context.Background()
	testPool.Exec(ctx,
		`UPDATE agent_task_queue SET status = 'completed' WHERE issue_id = $1 AND agent_id = $2 AND status IN ('queued','dispatched')`,
		fx.Issue.ID, author)
	comment, err := testHandler.Queries.CreateComment(ctx, db.CreateCommentParams{
		IssueID:     fx.Issue.ID,
		WorkspaceID: fx.Issue.WorkspaceID,
		AuthorType:  "agent",
		AuthorID:    author,
		Content:     body,
		Type:        "comment",
	})
	if err != nil {
		t.Fatalf("create verdict comment: %v", err)
	}
	issue, _ := testHandler.Queries.GetIssue(ctx, fx.Issue.ID)
	fx.Orch.OnComment(ctx, issue, comment, "agent", uuidToString(author))
}

// Scenario 1: dev -> in_review auto-dispatches the auditor, idempotently.
func TestOrchestrator_DevToInReviewDispatchesAuditOnce(t *testing.T) {
	fx := newStageOrchFixture(t)

	fx.inReview(t)
	if got := pendingCount(t, fx.Issue.ID, fx.AuditID); got != 1 {
		t.Fatalf("after first in_review: auditor pending = %d, want 1", got)
	}
	if s := currentStage(t, fx.Issue.ID); s != service.StageAudit {
		t.Fatalf("stage = %q, want audit", s)
	}

	// Second in_review must NOT dispatch a second audit (idempotent).
	fx.inReview(t)
	if got := pendingCount(t, fx.Issue.ID, fx.AuditID); got != 1 {
		t.Fatalf("after second in_review: auditor pending = %d, want 1 (idempotent)", got)
	}
}

// Scenario 2: audit PASS -> gate; audit FAIL -> deepdig + rework.
func TestOrchestrator_AuditPassTriggersGate(t *testing.T) {
	fx := newStageOrchFixture(t)
	fx.inReview(t)

	fx.verdict(t, fx.AuditID, "审计完成 VERDICT: PASS")
	if got := pendingCount(t, fx.Issue.ID, fx.GateID); got != 1 {
		t.Fatalf("gate pending = %d, want 1", got)
	}
	if s := currentStage(t, fx.Issue.ID); s != service.StageGate {
		t.Fatalf("stage = %q, want gate", s)
	}
}

func TestOrchestrator_AuditFailTriggersDeepdigAndRework(t *testing.T) {
	fx := newStageOrchFixture(t)
	fx.inReview(t)

	fx.verdict(t, fx.AuditID, "【VERDICT: FAIL】存在缺陷")
	if got := pendingCount(t, fx.Issue.ID, fx.DigID); got != 1 {
		t.Fatalf("deepdig pending = %d, want 1", got)
	}
	if got := pendingCount(t, fx.Issue.ID, fx.DevID); got != 1 {
		t.Fatalf("rework (dev) pending = %d, want 1", got)
	}
	if s := currentStage(t, fx.Issue.ID); s != service.StageRework {
		t.Fatalf("stage = %q, want rework", s)
	}
}

// Scenario 3: rework -> in_review auto-dispatches a re-review by the same auditor.
func TestOrchestrator_ReworkInReviewDispatchesReReview(t *testing.T) {
	fx := newStageOrchFixture(t)
	fx.inReview(t)
	fx.verdict(t, fx.AuditID, "VERDICT: FAIL")
	if s := currentStage(t, fx.Issue.ID); s != service.StageRework {
		t.Fatalf("precondition stage = %q, want rework", s)
	}
	// Clear the dev's rework task to simulate rework completion.
	testPool.Exec(context.Background(),
		`UPDATE agent_task_queue SET status = 'completed' WHERE issue_id = $1 AND agent_id = $2`,
		fx.Issue.ID, fx.DevID)

	fx.inReview(t)
	if got := pendingCount(t, fx.Issue.ID, fx.AuditID); got != 1 {
		t.Fatalf("re-review by same auditor pending = %d, want 1", got)
	}
	if s := currentStage(t, fx.Issue.ID); s != service.StageAudit {
		t.Fatalf("stage = %q, want audit", s)
	}
}

// Scenario 4: gate PASS -> done; gate FAIL -> rework.
func TestOrchestrator_GatePassMarksDone(t *testing.T) {
	fx := newStageOrchFixture(t)
	fx.inReview(t)
	fx.verdict(t, fx.AuditID, "VERDICT: PASS")
	fx.verdict(t, fx.GateID, "门禁 VERDICT: PASS")

	if s := currentStage(t, fx.Issue.ID); s != service.StageDone {
		t.Fatalf("stage = %q, want done", s)
	}
	issue, _ := testHandler.Queries.GetIssue(context.Background(), fx.Issue.ID)
	if issue.Status != "done" {
		t.Fatalf("issue status = %q, want done", issue.Status)
	}
}

func TestOrchestrator_GateFailTriggersRework(t *testing.T) {
	fx := newStageOrchFixture(t)
	fx.inReview(t)
	fx.verdict(t, fx.AuditID, "VERDICT: PASS")
	fx.verdict(t, fx.GateID, "门禁 VERDICT: FAIL")

	if got := pendingCount(t, fx.Issue.ID, fx.DevID); got != 1 {
		t.Fatalf("rework (dev) pending = %d, want 1", got)
	}
	if s := currentStage(t, fx.Issue.ID); s != service.StageRework {
		t.Fatalf("stage = %q, want rework", s)
	}
}

// Scenario 5: a completed stage with no follow-up produces exactly one reminder
// after the stall timeout.
func TestOrchestrator_WatchdogRemindsOncePerStall(t *testing.T) {
	fx := newStageOrchFixture(t)
	fx.inReview(t) // -> audit stage, auditor dispatched; now no verdict ever comes.

	// Use a tiny stall timeout instead of waiting 5 real minutes.
	fx.Orch.StallTimeout = 50 * time.Millisecond
	time.Sleep(80 * time.Millisecond)

	before := systemCommentCount(t, fx.Issue.ID)
	if sent := fx.Orch.RunWatchdogOnce(context.Background()); sent != 1 {
		t.Fatalf("first watchdog pass sent = %d, want 1", sent)
	}
	if got := pendingCount(t, fx.Issue.ID, fx.LeaderID); got != 1 {
		t.Fatalf("leader woken pending = %d, want 1", got)
	}
	after := systemCommentCount(t, fx.Issue.ID)
	if after-before != 1 {
		t.Fatalf("reminder system comments added = %d, want 1", after-before)
	}

	// Second pass for the same stuck point must NOT remind again.
	if sent := fx.Orch.RunWatchdogOnce(context.Background()); sent != 0 {
		t.Fatalf("second watchdog pass sent = %d, want 0 (no spam)", sent)
	}
	if got := systemCommentCount(t, fx.Issue.ID); got != after {
		t.Fatalf("system comments after second pass = %d, want %d", got, after)
	}
}

// Scenario 6: the orchestrator's own system comments never re-trigger it.
func TestOrchestrator_IgnoresSystemComments(t *testing.T) {
	fx := newStageOrchFixture(t)
	fx.inReview(t)
	stageBefore := currentStage(t, fx.Issue.ID)

	ctx := context.Background()
	// A system-authored comment that *contains* a verdict must be ignored.
	sysComment, err := testHandler.Queries.CreateComment(ctx, db.CreateCommentParams{
		IssueID:     fx.Issue.ID,
		WorkspaceID: fx.Issue.WorkspaceID,
		AuthorType:  "system",
		AuthorID:    pgtype.UUID{Valid: true},
		Content:     "🤖 阶段编排器：VERDICT: PASS",
		Type:        "system",
	})
	if err != nil {
		t.Fatalf("create system comment: %v", err)
	}
	issue, _ := testHandler.Queries.GetIssue(ctx, fx.Issue.ID)
	fx.Orch.OnComment(ctx, issue, sysComment, "system", "")

	if got := pendingCount(t, fx.Issue.ID, fx.GateID); got != 0 {
		t.Fatalf("system verdict wrongly dispatched gate: pending = %d, want 0", got)
	}
	if s := currentStage(t, fx.Issue.ID); s != stageBefore {
		t.Fatalf("stage changed from system comment: %q -> %q", stageBefore, s)
	}
}

// Config gate: when the workspace has no config row, the orchestrator is off.
func TestOrchestrator_DisabledWhenNoConfig(t *testing.T) {
	fx := newStageOrchFixture(t)
	// Remove the enabling config row.
	if _, err := testPool.Exec(context.Background(),
		`DELETE FROM stage_orchestrator_config WHERE workspace_id = $1`, testWorkspaceID); err != nil {
		t.Fatalf("delete config: %v", err)
	}

	fx.inReview(t)
	if got := pendingCount(t, fx.Issue.ID, fx.AuditID); got != 0 {
		t.Fatalf("auditor dispatched while disabled: pending = %d, want 0", got)
	}
}
