package service

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgtype"
	"github.com/multica-ai/multica/server/internal/events"
	"github.com/multica-ai/multica/server/internal/util"
	db "github.com/multica-ai/multica/server/pkg/db/generated"
	"github.com/multica-ai/multica/server/pkg/protocol"
)

// StageOrchestrator implements the PL-106 "stage advancement orchestrator /
// watchdog": it reacts to issue status changes and VERDICT comments to
// auto-advance the dev -> audit -> rework -> audit -> gate -> done pipeline,
// and a background watchdog reminds the line leader when a stage completes but
// the next action stalls.
//
// All orchestrator-authored comments are written with author_type='system'
// and never flow through the HTTP comment-trigger path, so they cannot wake an
// agent and form a loop. OnComment additionally ignores system-authored
// comments as a second line of defense (PL-91 self-trigger handling).
type StageOrchestrator struct {
	Queries *db.Queries
	DB      orchestratorDB
	Tasks   *TaskService
	Bus     *events.Bus

	// StallTimeout is how long a stage may sit without a follow-up event
	// before the watchdog reminds the line leader. Defaults to 5 minutes.
	StallTimeout time.Duration

	// Now is injected so tests can drive the watchdog deterministically.
	Now func() time.Time
}

// orchestratorDB is the subset of pgx the orchestrator needs for the two
// small tables it owns. *pgxpool.Pool satisfies it.
type orchestratorDB interface {
	Exec(ctx context.Context, sql string, args ...any) (pgconn.CommandTag, error)
	Query(ctx context.Context, sql string, args ...any) (pgx.Rows, error)
	QueryRow(ctx context.Context, sql string, args ...any) pgx.Row
}

// Stage constants for the lightweight state machine.
const (
	StageDev    = "dev"
	StageAudit  = "audit"
	StageRework = "rework"
	StageGate   = "gate"
	StageDone   = "done"
)

// Event-action keys used by the per-workspace / per-agent config switches.
const (
	ActionAudit    = "audit"
	ActionGate     = "gate"
	ActionDeepdig  = "deepdig"
	ActionRework   = "rework"
	ActionReminder = "reminder"
)

// DefaultStageStallTimeout is the grace period after a stage completes before
// the watchdog nudges the line leader.
const DefaultStageStallTimeout = 5 * time.Minute

func NewStageOrchestrator(q *db.Queries, dbx orchestratorDB, tasks *TaskService, bus *events.Bus) *StageOrchestrator {
	return &StageOrchestrator{
		Queries:      q,
		DB:           dbx,
		Tasks:        tasks,
		Bus:          bus,
		StallTimeout: DefaultStageStallTimeout,
		Now:          time.Now,
	}
}

func (o *StageOrchestrator) now() time.Time {
	if o.Now != nil {
		return o.Now()
	}
	return time.Now()
}

// ---------------------------------------------------------------------------
// VERDICT parsing
// ---------------------------------------------------------------------------

var (
	// Adjacent form: a verdict marker immediately followed (allowing
	// separators / brackets / whitespace) by a PASS/FAIL token. This is the
	// strongest signal and tolerates case, prefixes, suffixes and mixed
	// CN/EN scripts, e.g. "VERDICT: PASS", "【verdict=FAIL】", "审计结论 VERDICT：通过".
	// Fail-negation tokens (不通过/未通过) are listed before 通过 so they win the
	// alternation at the same position.
	verdictAdjacentRe = regexp.MustCompile(`(?i)(?:verdict|结论|裁决|判定|裁定)\s*[:：=＝\-—\s\[\]【】「」（）()]*\s*(不通过|未通过|不通過|未通過|通过|通過|pass|fail|失败|失敗)`)

	// Fallback signals: a marker is present somewhere and a single polarity
	// can be inferred from the whole body.
	verdictMarkerRe = regexp.MustCompile(`(?i)verdict|结论|裁决|判定|裁定`)
	verdictPassRe   = regexp.MustCompile(`(?i)\bpass(?:ed)?\b|通过|通過`)
	verdictFailRe   = regexp.MustCompile(`(?i)\bfail(?:ed|ure)?\b|失败|失敗|不通过|未通过|不通過|未通過`)
)

// ParseVerdict extracts a PASS/FAIL verdict from a comment body. It returns
// ("pass"|"fail", true) when a verdict is unambiguously present, or ("", false)
// otherwise. Robust to case, surrounding text, brackets and mixed CN/EN.
func ParseVerdict(content string) (string, bool) {
	if strings.TrimSpace(content) == "" {
		return "", false
	}
	if m := verdictAdjacentRe.FindStringSubmatch(content); m != nil {
		return classifyVerdictToken(m[1]), true
	}
	// Fallback requires an explicit marker so plain prose mentioning "pass"
	// doesn't get misread as a verdict.
	if !verdictMarkerRe.MatchString(content) {
		return "", false
	}
	failMatched := verdictFailRe.MatchString(content)
	// Neutralize fail-negations (不通过/未通过) before testing for a pass so
	// "不通过" is not double-counted as "通过".
	stripped := verdictFailRe.ReplaceAllString(content, " ")
	passMatched := verdictPassRe.MatchString(stripped)
	switch {
	case failMatched && !passMatched:
		return "fail", true
	case passMatched && !failMatched:
		return "pass", true
	default:
		// Both or neither — ambiguous, don't guess.
		return "", false
	}
}

func classifyVerdictToken(tok string) string {
	t := strings.ToLower(tok)
	if strings.Contains(t, "fail") ||
		strings.Contains(tok, "失败") || strings.Contains(tok, "失敗") ||
		strings.Contains(tok, "不通过") || strings.Contains(tok, "未通过") ||
		strings.Contains(tok, "不通過") || strings.Contains(tok, "未通過") {
		return "fail"
	}
	return "pass"
}

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

type orchConfig struct {
	enabled bool
	actions map[string]bool
}

// actionEnabled reports whether a specific event action should run. A missing
// key defaults to enabled, so an empty actions object means "all on".
func (c orchConfig) actionEnabled(name string) bool {
	if !c.enabled {
		return false
	}
	if c.actions == nil {
		return true
	}
	v, ok := c.actions[name]
	if !ok {
		return true
	}
	return v
}

// loadConfig resolves the effective config for an issue. The workspace-default
// row (agent_id IS NULL) is the base; a row for the controlling agent (the
// developer) overrides enabled + merges per-action toggles. No workspace row
// at all means the orchestrator is OFF (opt-in) so existing workspaces are
// untouched by the rollout.
func (o *StageOrchestrator) loadConfig(ctx context.Context, workspaceID, agentID pgtype.UUID) orchConfig {
	rows, err := o.DB.Query(ctx, `
		SELECT agent_id, enabled, actions
		FROM stage_orchestrator_config
		WHERE workspace_id = $1 AND (agent_id IS NULL OR agent_id = $2)`,
		workspaceID, agentID)
	if err != nil {
		slog.Warn("orchestrator: load config failed", "workspace_id", util.UUIDToString(workspaceID), "error", err)
		return orchConfig{enabled: false}
	}
	defer rows.Close()

	var (
		haveWS    bool
		wsEnabled bool
		wsActions map[string]bool
		haveAgent bool
		agEnabled bool
		agActions map[string]bool
	)
	for rows.Next() {
		var (
			rowAgent   pgtype.UUID
			enabled    bool
			actionsRaw []byte
		)
		if err := rows.Scan(&rowAgent, &enabled, &actionsRaw); err != nil {
			slog.Warn("orchestrator: scan config failed", "error", err)
			continue
		}
		parsed := map[string]bool{}
		if len(actionsRaw) > 0 {
			_ = json.Unmarshal(actionsRaw, &parsed)
		}
		if rowAgent.Valid {
			haveAgent, agEnabled, agActions = true, enabled, parsed
		} else {
			haveWS, wsEnabled, wsActions = true, enabled, parsed
		}
	}
	if err := rows.Err(); err != nil {
		slog.Warn("orchestrator: iterate config failed", "error", err)
	}

	if !haveWS {
		return orchConfig{enabled: false}
	}
	cfg := orchConfig{enabled: wsEnabled, actions: map[string]bool{}}
	for k, v := range wsActions {
		cfg.actions[k] = v
	}
	if haveAgent {
		// Agent override: AND the enabled flag, overlay action toggles.
		cfg.enabled = cfg.enabled && agEnabled
		for k, v := range agActions {
			cfg.actions[k] = v
		}
	}
	return cfg
}

// ---------------------------------------------------------------------------
// State row access
// ---------------------------------------------------------------------------

type stageState struct {
	IssueID      pgtype.UUID
	WorkspaceID  pgtype.UUID
	Stage        string
	AuditAgentID pgtype.UUID
	LastEventAt  time.Time
	RemindedKey  string
}

func (o *StageOrchestrator) getState(ctx context.Context, issueID pgtype.UUID) (stageState, bool) {
	var s stageState
	err := o.DB.QueryRow(ctx, `
		SELECT issue_id, workspace_id, stage, audit_agent_id, last_event_at, reminded_key
		FROM stage_orchestration WHERE issue_id = $1`, issueID).
		Scan(&s.IssueID, &s.WorkspaceID, &s.Stage, &s.AuditAgentID, &s.LastEventAt, &s.RemindedKey)
	if errors.Is(err, pgx.ErrNoRows) {
		return stageState{}, false
	}
	if err != nil {
		slog.Warn("orchestrator: get state failed", "issue_id", util.UUIDToString(issueID), "error", err)
		return stageState{}, false
	}
	return s, true
}

// advanceStage writes the new stage, stamps last_event_at=now and clears the
// reminder bookkeeping so the next stall (if any) can be reminded fresh. The
// row is created if it does not yet exist.
func (o *StageOrchestrator) advanceStage(ctx context.Context, issue db.Issue, stage string, auditAgentID pgtype.UUID) {
	_, err := o.DB.Exec(ctx, `
		INSERT INTO stage_orchestration (issue_id, workspace_id, stage, audit_agent_id, last_event_at, reminded_key, reminded_at, updated_at)
		VALUES ($1, $2, $3, $4, $5, '', NULL, now())
		ON CONFLICT (issue_id) DO UPDATE SET
			stage = EXCLUDED.stage,
			audit_agent_id = COALESCE(EXCLUDED.audit_agent_id, stage_orchestration.audit_agent_id),
			last_event_at = EXCLUDED.last_event_at,
			reminded_key = '',
			reminded_at = NULL,
			updated_at = now()`,
		issue.ID, issue.WorkspaceID, stage, auditAgentID, o.now())
	if err != nil {
		slog.Warn("orchestrator: advance stage failed", "issue_id", util.UUIDToString(issue.ID), "stage", stage, "error", err)
	}
}

// ---------------------------------------------------------------------------
// Event entry points
// ---------------------------------------------------------------------------

// OnIssueStatusChanged is the hook for issue status transitions. The only
// status that drives the pipeline is in_review: a dev issue entering in_review
// dispatches the first audit; a reworked issue re-entering in_review dispatches
// a re-review by the same auditor. Idempotent: a second in_review while already
// in the audit stage is a no-op.
func (o *StageOrchestrator) OnIssueStatusChanged(ctx context.Context, prev, issue db.Issue) {
	if o == nil || o.DB == nil {
		return
	}
	if issue.Status != "in_review" {
		return
	}
	cfgAgent := configAgentID(issue)
	cfg := o.loadConfig(ctx, issue.WorkspaceID, cfgAgent)
	if !cfg.enabled {
		return
	}

	state, exists := o.getState(ctx, issue.ID)
	stage := StageDev
	if exists {
		stage = state.Stage
	}

	switch stage {
	case StageDev:
		// First review.
		if !cfg.actionEnabled(ActionAudit) {
			return
		}
		auditor, ok := o.resolveRoleAgent(ctx, issue, roleAudit)
		if !ok {
			slog.Info("orchestrator: no auditor resolved for first review", "issue_id", util.UUIDToString(issue.ID))
			// Still record audit stage so the watchdog can nudge the leader.
			o.advanceStage(ctx, issue, StageAudit, pgtype.UUID{})
			return
		}
		o.dispatchAgent(ctx, issue, auditor, false, "🤖 阶段编排器：开发已转 in_review，自动派审计首审。")
		o.advanceStage(ctx, issue, StageAudit, auditor)

	case StageRework:
		// Rework finished -> re-review by the same auditor.
		if !cfg.actionEnabled(ActionAudit) {
			return
		}
		auditor := state.AuditAgentID
		if !auditor.Valid {
			if resolved, ok := o.resolveRoleAgent(ctx, issue, roleAudit); ok {
				auditor = resolved
			}
		}
		if auditor.Valid {
			o.dispatchAgent(ctx, issue, auditor, false, "🤖 阶段编排器：返工已转 in_review，自动派同一审计复审。")
		}
		o.advanceStage(ctx, issue, StageAudit, auditor)

	case StageAudit, StageGate, StageDone:
		// Already past the dev->audit dispatch; in_review here is idempotent
		// (the audit was already dispatched for this review cycle).
		return
	}
}

// OnComment is the hook for new comments. It parses a VERDICT and, based on the
// issue's current stage, advances the pipeline. System-authored comments are
// ignored to prevent the orchestrator's own messages from re-triggering it.
func (o *StageOrchestrator) OnComment(ctx context.Context, issue db.Issue, comment db.Comment, authorType, authorID string) {
	if o == nil || o.DB == nil {
		return
	}
	// Anti-loop: never react to system-authored comments (these include the
	// orchestrator's own messages).
	if authorType == "system" || comment.AuthorType == "system" {
		return
	}
	verdict, ok := ParseVerdict(comment.Content)
	if !ok {
		return
	}

	state, exists := o.getState(ctx, issue.ID)
	if !exists {
		return
	}
	cfg := o.loadConfig(ctx, issue.WorkspaceID, configAgentID(issue))
	if !cfg.enabled {
		return
	}

	switch state.Stage {
	case StageAudit:
		if verdict == "pass" {
			if cfg.actionEnabled(ActionGate) {
				if gate, ok := o.resolveRoleAgent(ctx, issue, roleGate); ok {
					o.dispatchAgent(ctx, issue, gate, false, "🤖 阶段编排器：审计 PASS，自动触发门禁(gate)。")
				} else {
					o.postSystemComment(ctx, issue, "🤖 阶段编排器：审计 PASS，但未找到门禁(gate)角色，已挂起等待线主脑处理。")
				}
			}
			o.advanceStage(ctx, issue, StageGate, state.AuditAgentID)
			return
		}
		// FAIL -> deepdig + rework.
		if cfg.actionEnabled(ActionDeepdig) {
			if dig, ok := o.resolveRoleAgent(ctx, issue, roleDeepdig); ok {
				o.dispatchAgent(ctx, issue, dig, false, "🤖 阶段编排器：审计 FAIL，自动触发深挖(deepdig)。")
			}
		}
		if cfg.actionEnabled(ActionRework) {
			o.dispatchRework(ctx, issue, "🤖 阶段编排器：审计 FAIL，自动派返工。")
		}
		o.advanceStage(ctx, issue, StageRework, state.AuditAgentID)

	case StageGate:
		if verdict == "pass" {
			o.markIssueDone(ctx, issue)
			o.advanceStage(ctx, issue, StageDone, state.AuditAgentID)
			return
		}
		// gate FAIL -> rework.
		if cfg.actionEnabled(ActionRework) {
			o.dispatchRework(ctx, issue, "🤖 阶段编排器：门禁 FAIL，自动派返工。")
		}
		o.advanceStage(ctx, issue, StageRework, state.AuditAgentID)

	default:
		// dev / rework / done: a verdict here is not part of the contract.
		return
	}
}

// ---------------------------------------------------------------------------
// Watchdog
// ---------------------------------------------------------------------------

// RunWatchdogOnce scans for stages that completed but stalled (no follow-up
// event within StallTimeout) and reminds the line leader exactly once per
// stuck point. Returns the number of reminders sent (useful for tests / logs).
func (o *StageOrchestrator) RunWatchdogOnce(ctx context.Context) int {
	if o == nil || o.DB == nil {
		return 0
	}
	cutoff := o.now().Add(-o.StallTimeout)
	rows, err := o.DB.Query(ctx, `
		SELECT issue_id, workspace_id, stage, audit_agent_id, last_event_at, reminded_key
		FROM stage_orchestration
		WHERE stage IN ('audit', 'rework', 'gate')
		  AND last_event_at < $1`, cutoff)
	if err != nil {
		slog.Warn("orchestrator: watchdog scan failed", "error", err)
		return 0
	}
	var stalled []stageState
	for rows.Next() {
		var s stageState
		if err := rows.Scan(&s.IssueID, &s.WorkspaceID, &s.Stage, &s.AuditAgentID, &s.LastEventAt, &s.RemindedKey); err != nil {
			slog.Warn("orchestrator: watchdog scan row failed", "error", err)
			continue
		}
		stalled = append(stalled, s)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		slog.Warn("orchestrator: watchdog iterate failed", "error", err)
	}

	sent := 0
	for _, s := range stalled {
		key := stallKey(s.Stage, s.LastEventAt)
		if s.RemindedKey == key {
			continue // already reminded this exact stuck point
		}
		if o.remind(ctx, s, key) {
			sent++
		}
	}
	return sent
}

// remind posts a single reminder for a stalled stage and wakes the line
// leader. Returns true when a reminder was actually sent.
func (o *StageOrchestrator) remind(ctx context.Context, s stageState, key string) bool {
	issue, err := o.Queries.GetIssue(ctx, s.IssueID)
	if err != nil {
		return false
	}
	cfg := o.loadConfig(ctx, issue.WorkspaceID, configAgentID(issue))
	if !cfg.enabled || !cfg.actionEnabled(ActionReminder) {
		return false
	}

	leader, hasLeader := o.resolveRoleAgent(ctx, issue, roleLeader)
	overseer, hasOverseer := o.resolveRoleAgent(ctx, issue, roleOverseer)

	var b strings.Builder
	b.WriteString(fmt.Sprintf("⏰ 阶段编排器看门狗：本卡片在 `%s` 阶段已超过 %s 没有下一步动作。\n\n",
		s.Stage, o.StallTimeout.String()))
	if hasLeader {
		b.WriteString(fmt.Sprintf("请线主脑(%s)介入推进。", agentName(ctx, o.Queries, leader)))
	} else {
		b.WriteString("请线主脑介入推进。")
	}
	if hasOverseer {
		b.WriteString(fmt.Sprintf(" cc 总管(%s)。", agentName(ctx, o.Queries, overseer)))
	}

	o.postSystemComment(ctx, issue, b.String())

	// Wake the leader once (deduped) so the reminder is actionable, not just
	// informational. The overseer is cc-only (named in the comment) to avoid
	// double-dispatch.
	if hasLeader {
		o.dispatchAgent(ctx, issue, leader, true, "")
	}

	// Mark this stuck point reminded WITHOUT touching last_event_at, so the
	// key stays stable and we never remind the same stall twice. A genuine
	// stage advance (advanceStage) resets reminded_key and bumps
	// last_event_at, allowing a fresh stall to be reminded later.
	if _, err := o.DB.Exec(ctx, `
		UPDATE stage_orchestration SET reminded_key = $2, reminded_at = $3, updated_at = now()
		WHERE issue_id = $1`, s.IssueID, key, o.now()); err != nil {
		slog.Warn("orchestrator: mark reminded failed", "issue_id", util.UUIDToString(s.IssueID), "error", err)
	}
	return true
}

func stallKey(stage string, lastEventAt time.Time) string {
	return stage + "|" + strconv.FormatInt(lastEventAt.UnixNano(), 10)
}

// ---------------------------------------------------------------------------
// Dispatch helpers
// ---------------------------------------------------------------------------

// dispatchAgent enqueues a task for agentID on the issue (deduped against any
// pending task it already has) and, when note is non-empty, posts a
// system comment recording the action. isLeader routes through the squad-leader
// enqueue path so leader self-trigger guards apply.
func (o *StageOrchestrator) dispatchAgent(ctx context.Context, issue db.Issue, agentID pgtype.UUID, isLeader bool, note string) {
	if !agentID.Valid {
		return
	}
	hasPending, err := o.Queries.HasPendingTaskForIssueAndAgent(ctx, db.HasPendingTaskForIssueAndAgentParams{
		IssueID: issue.ID,
		AgentID: agentID,
	})
	if err == nil && hasPending {
		// Idempotent: the agent is already queued/dispatched for this issue.
		return
	}
	if note != "" {
		o.postSystemComment(ctx, issue, note)
	}
	if isLeader {
		if _, err := o.Tasks.EnqueueTaskForSquadLeader(ctx, issue, agentID, pgtype.UUID{}); err != nil {
			slog.Warn("orchestrator: dispatch leader failed", "issue_id", util.UUIDToString(issue.ID), "agent_id", util.UUIDToString(agentID), "error", err)
		}
		return
	}
	if _, err := o.Tasks.EnqueueTaskForMention(ctx, issue, agentID, pgtype.UUID{}); err != nil {
		slog.Warn("orchestrator: dispatch agent failed", "issue_id", util.UUIDToString(issue.ID), "agent_id", util.UUIDToString(agentID), "error", err)
	}
}

// dispatchRework re-dispatches the developer to fix the issue. The developer is
// the issue's agent assignee; for squad-assigned issues it is the member with a
// developer role, falling back to the squad leader.
func (o *StageOrchestrator) dispatchRework(ctx context.Context, issue db.Issue, note string) {
	if issue.AssigneeType.Valid && issue.AssigneeType.String == "agent" && issue.AssigneeID.Valid {
		o.dispatchAgent(ctx, issue, issue.AssigneeID, false, note)
		return
	}
	if dev, ok := o.resolveRoleAgent(ctx, issue, roleDev); ok {
		o.dispatchAgent(ctx, issue, dev, false, note)
		return
	}
	if leader, ok := o.resolveRoleAgent(ctx, issue, roleLeader); ok {
		o.dispatchAgent(ctx, issue, leader, true, note)
	}
}

// markIssueDone advances the issue to done. Done directly via SQL (the agent /
// gate already produced the verdict); a system comment + EventIssueUpdated keep
// clients in sync.
func (o *StageOrchestrator) markIssueDone(ctx context.Context, issue db.Issue) {
	tag, err := o.DB.Exec(ctx, `
		UPDATE issue SET status = 'done', updated_at = now()
		WHERE id = $1 AND status <> 'done'`, issue.ID)
	if err != nil {
		slog.Warn("orchestrator: mark issue done failed", "issue_id", util.UUIDToString(issue.ID), "error", err)
		return
	}
	o.postSystemComment(ctx, issue, "🤖 阶段编排器：门禁 PASS，已标记 done 并推进下一阶段。")
	if tag.RowsAffected() > 0 && o.Bus != nil {
		o.Bus.Publish(events.Event{
			Type:        protocol.EventIssueUpdated,
			WorkspaceID: util.UUIDToString(issue.WorkspaceID),
			ActorType:   "system",
			Payload: map[string]any{
				"issue_id":       util.UUIDToString(issue.ID),
				"status":         "done",
				"prev_status":    issue.Status,
				"status_changed": true,
			},
		})
	}
}

// postSystemComment writes an author_type='system' comment. These never flow
// through the comment-trigger path (they are not created via the HTTP handler),
// so they cannot wake an agent — the foundation of the anti-loop guarantee.
func (o *StageOrchestrator) postSystemComment(ctx context.Context, issue db.Issue, content string) {
	if content == "" {
		return
	}
	comment, err := o.Queries.CreateComment(ctx, db.CreateCommentParams{
		IssueID:     issue.ID,
		WorkspaceID: issue.WorkspaceID,
		AuthorType:  "system",
		AuthorID:    pgtype.UUID{Valid: true}, // zero UUID; clients branch on author_type
		Content:     content,
		Type:        "system",
		ParentID:    pgtype.UUID{Valid: false},
	})
	if err != nil {
		slog.Warn("orchestrator: post system comment failed", "issue_id", util.UUIDToString(issue.ID), "error", err)
		return
	}
	if o.Bus != nil {
		o.Bus.Publish(events.Event{
			Type:        protocol.EventCommentCreated,
			WorkspaceID: util.UUIDToString(issue.WorkspaceID),
			ActorType:   "system",
			Payload: map[string]any{
				"comment": map[string]any{
					"id":          util.UUIDToString(comment.ID),
					"issue_id":    util.UUIDToString(comment.IssueID),
					"author_type": comment.AuthorType,
					"author_id":   util.UUIDToString(comment.AuthorID),
					"content":     comment.Content,
					"type":        comment.Type,
					"created_at":  comment.CreatedAt.Time.Format("2006-01-02T15:04:05Z"),
				},
				"issue_title":  issue.Title,
				"issue_status": issue.Status,
			},
		})
	}
}

// ---------------------------------------------------------------------------
// Role resolution
// ---------------------------------------------------------------------------

type squadRole int

const (
	roleAudit squadRole = iota
	roleGate
	roleDeepdig
	roleOverseer
	roleDev
	roleLeader
)

var roleKeywords = map[squadRole][]string{
	roleAudit:    {"audit", "审计", "审查", "复审", "codex", "review"},
	roleGate:     {"gate", "门禁", "质量门", "gatekeeper", "qwen"},
	roleDeepdig:  {"deepdig", "深挖", "deepseek", "dig", "根因", "root cause"},
	roleOverseer: {"overseer", "总管", "manager", "主管", "总控", "admin"},
	roleDev:      {"dev", "开发", "developer", "coder", "工程"},
}

// resolveRoleAgent finds the agent that fills a role for the issue's governing
// squad. roleLeader returns the squad's leader directly; other roles match
// squad_member.role against keyword lists. Returns (id, true) only when a
// usable agent (with a runtime, not archived) is found.
func (o *StageOrchestrator) resolveRoleAgent(ctx context.Context, issue db.Issue, role squadRole) (pgtype.UUID, bool) {
	squad, ok := o.resolveSquad(ctx, issue)
	if !ok {
		return pgtype.UUID{}, false
	}
	if role == roleLeader {
		if o.agentUsable(ctx, squad.LeaderID) {
			return squad.LeaderID, true
		}
		return pgtype.UUID{}, false
	}
	members, err := o.Queries.ListSquadMembers(ctx, squad.ID)
	if err != nil {
		return pgtype.UUID{}, false
	}
	keywords := roleKeywords[role]
	for _, m := range members {
		if m.MemberType != "agent" {
			continue
		}
		if !roleMatches(m.Role, keywords) {
			continue
		}
		if o.agentUsable(ctx, m.MemberID) {
			return m.MemberID, true
		}
	}
	return pgtype.UUID{}, false
}

func roleMatches(role string, keywords []string) bool {
	r := strings.ToLower(strings.TrimSpace(role))
	if r == "" {
		return false
	}
	for _, kw := range keywords {
		if strings.Contains(r, strings.ToLower(kw)) {
			return true
		}
	}
	return false
}

func (o *StageOrchestrator) agentUsable(ctx context.Context, agentID pgtype.UUID) bool {
	if !agentID.Valid {
		return false
	}
	agent, err := o.Queries.GetAgent(ctx, agentID)
	if err != nil || agent.ArchivedAt.Valid || !agent.RuntimeID.Valid {
		return false
	}
	return true
}

// resolveSquad finds the squad governing an issue: the issue's own squad
// assignee, then any ancestor issue assigned to a squad, then a squad the
// assignee agent belongs to.
func (o *StageOrchestrator) resolveSquad(ctx context.Context, issue db.Issue) (db.Squad, bool) {
	if issue.AssigneeType.Valid && issue.AssigneeType.String == "squad" && issue.AssigneeID.Valid {
		if sq, err := o.Queries.GetSquadByAssignee(ctx, db.GetSquadByAssigneeParams{
			ID:          issue.AssigneeID,
			WorkspaceID: issue.WorkspaceID,
		}); err == nil {
			return sq, true
		}
	}

	// Walk up the parent chain (bounded) looking for a squad-assigned ancestor.
	parent := issue.ParentIssueID
	for hops := 0; hops < 8 && parent.Valid; hops++ {
		p, err := o.Queries.GetIssue(ctx, parent)
		if err != nil {
			break
		}
		if p.AssigneeType.Valid && p.AssigneeType.String == "squad" && p.AssigneeID.Valid {
			if sq, err := o.Queries.GetSquadByAssignee(ctx, db.GetSquadByAssigneeParams{
				ID:          p.AssigneeID,
				WorkspaceID: p.WorkspaceID,
			}); err == nil {
				return sq, true
			}
		}
		parent = p.ParentIssueID
	}

	// Fall back to a squad the assignee agent is a member of.
	if issue.AssigneeType.Valid && issue.AssigneeType.String == "agent" && issue.AssigneeID.Valid {
		if squads, err := o.Queries.ListSquadsByMember(ctx, db.ListSquadsByMemberParams{
			WorkspaceID: issue.WorkspaceID,
			MemberType:  "agent",
			MemberID:    issue.AssigneeID,
		}); err == nil && len(squads) > 0 {
			return squads[0], true
		}
	}
	return db.Squad{}, false
}

// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------

// configAgentID returns the agent whose per-agent config override applies to an
// issue (the developer / agent assignee), or an invalid UUID for squad issues.
func configAgentID(issue db.Issue) pgtype.UUID {
	if issue.AssigneeType.Valid && issue.AssigneeType.String == "agent" {
		return issue.AssigneeID
	}
	return pgtype.UUID{}
}

func agentName(ctx context.Context, q *db.Queries, id pgtype.UUID) string {
	if !id.Valid {
		return ""
	}
	if agent, err := q.GetAgent(ctx, id); err == nil {
		return agent.Name
	}
	return util.UUIDToString(id)
}
