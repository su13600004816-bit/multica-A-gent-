package service

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"time"

	"github.com/jackc/pgx/v5/pgtype"

	"github.com/multica-ai/multica/server/internal/service/memorycompact"
	"github.com/multica-ai/multica/server/internal/util"
	db "github.com/multica-ai/multica/server/pkg/db/generated"
	"github.com/multica-ai/multica/server/pkg/protocol"
)

// archiveCommentFetchLimit bounds how many of an issue's most-relevant
// comments are folded into an archive generation. The point of the feature
// is token 止血, so we never pull the full 2000-comment ceiling here; the
// compactor caps each record further. Chronological order with this LIMIT
// keeps the newest activity (most useful for the next session) in scope.
const archiveCommentFetchLimit = 400

// MemoryArchiveService produces the T1..T4 memory archive for a scope and
// flips its compaction marker (PL-91). It is the production wiring of
// internal/service/memorycompact: it reads source rows, runs the configured
// Compactor (Qwen when DASHSCOPE_API_KEY is set, else the deterministic
// fail-safe), persists every level, and — only on full success — marks the
// scope compacted so the daemon claim path starts the next task fresh.
//
// Every entry point is best-effort and fail-safe: callers log and ignore
// errors, and the original comment / chat_message rows are never mutated.
type MemoryArchiveService struct {
	Queries   *db.Queries
	TxStarter TxStarter
	Compactor memorycompact.Compactor
}

// NewMemoryArchiveService wires the service. A nil compactor defaults to the
// deterministic fail-safe so the service is always safe to call.
func NewMemoryArchiveService(q *db.Queries, tx TxStarter, c memorycompact.Compactor) *MemoryArchiveService {
	if c == nil {
		c = memorycompact.DeterministicCompactor{}
	}
	return &MemoryArchiveService{Queries: q, TxStarter: tx, Compactor: c}
}

// NewDefaultMemoryArchiveService builds the service with the production
// compactor: Qwen when DASHSCOPE_API_KEY is configured, else the
// deterministic fail-safe. Callers that don't want to depend on the
// memorycompact package directly use this.
func NewDefaultMemoryArchiveService(q *db.Queries, tx TxStarter) *MemoryArchiveService {
	return NewMemoryArchiveService(q, tx, memorycompact.DefaultCompactor())
}

// ArchiveIssue compacts an issue's recent comments and marks the issue
// memory-compacted. After this, daemon claims for the issue skip session
// resume and the assignment prompt carries only the injected T1/T2 summary.
func (m *MemoryArchiveService) ArchiveIssue(ctx context.Context, issueID, agentID pgtype.UUID) error {
	if m == nil || !issueID.Valid {
		return nil
	}
	issue, err := m.Queries.GetIssue(ctx, issueID)
	if err != nil {
		return fmt.Errorf("get issue: %w", err)
	}
	comments, err := m.issueArchiveComments(ctx, issueID, issue.WorkspaceID, issue.MemoryCompactedAt)
	if err != nil {
		return fmt.Errorf("list comments: %w", err)
	}
	if len(comments) == 0 {
		// Nothing to summarise; do not mark compacted (a fresh session with
		// no history to inject would just lose context for no token saving).
		return nil
	}
	msgs := commentsToMessages(comments)
	msgs = m.withPriorSummary(ctx, "issue", issueID, issue.MemoryCompactedAt, msgs)
	in := memorycompact.Input{
		ScopeType: "issue",
		ScopeID:   util.UUIDToString(issueID),
		Messages:  msgs,
	}
	return m.runArchive(ctx, in, util.UUIDToString(issue.WorkspaceID), util.UUIDToString(agentID))
}

// Chat compaction thresholds (PL-91). A chat session's ACTIVE window
// (messages since its last compaction) is compacted once it grows past
// either bound. Tuned to cut long-running chat windows — the global 止血
// target for cc / Claude开发 / cx / chat — before they balloon the resumed
// provider context. Either trigger fires.
const (
	chatCompactMessageThreshold = 40
	chatCompactByteThreshold    = 200_000
)

// ChatSessionNeedsCompaction reports whether an active-window size crosses a
// compaction threshold. Pure so it is unit-testable without a DB.
func ChatSessionNeedsCompaction(messageCount, byteSize int64) bool {
	return messageCount >= chatCompactMessageThreshold || byteSize >= chatCompactByteThreshold
}

// MaybeCompactChatSession compacts a chat session when its active window has
// grown past the threshold: it archives the window into T1..T4 and marks the
// session compacted, which clears session_id/work_dir so the daemon's next
// claim starts a fresh provider session. Returns true when it compacted.
// Best-effort and fail-safe: the caller logs and continues, originals are
// never deleted, and on any failure the session keeps its old resume pointer.
func (m *MemoryArchiveService) MaybeCompactChatSession(ctx context.Context, session db.ChatSession) (bool, error) {
	if m == nil {
		return false, nil
	}
	size, err := m.Queries.GetChatSessionWindowSize(ctx, db.GetChatSessionWindowSizeParams{
		ChatSessionID: session.ID,
		Since:         session.CompactedAt,
	})
	if err != nil {
		return false, fmt.Errorf("chat window size: %w", err)
	}
	if !ChatSessionNeedsCompaction(size.MessageCount, size.ByteSize) {
		return false, nil
	}
	if err := m.ArchiveChatSession(ctx, session); err != nil {
		return false, err
	}
	return true, nil
}

// ArchiveChatSession compacts a chat session's active window (messages newer
// than its last compaction point) and marks it compacted.
func (m *MemoryArchiveService) ArchiveChatSession(ctx context.Context, session db.ChatSession) error {
	if m == nil || !session.ID.Valid {
		return nil
	}
	rows, err := m.Queries.ListChatMessages(ctx, session.ID)
	if err != nil {
		return fmt.Errorf("list chat messages: %w", err)
	}
	window := filterChatSince(rows, session.CompactedAt)
	if len(window) == 0 {
		return nil
	}
	msgs := chatMessagesToMessages(window)
	msgs = m.withPriorSummary(ctx, "chat_session", session.ID, session.CompactedAt, msgs)
	in := memorycompact.Input{
		ScopeType: "chat_session",
		ScopeID:   util.UUIDToString(session.ID),
		Messages:  msgs,
	}
	return m.runArchive(ctx, in, util.UUIDToString(session.WorkspaceID), util.UUIDToString(session.AgentID))
}

// ArchiveTask writes a scope_type='task' archive for a completed task so the
// run itself is traceable through the archive index (audit requirement). The
// source is the task's trigger context + final output; originals are never
// touched. Best-effort and fail-safe. MarkCompacted for the 'task' scope is a
// no-op (there is no per-task marker column) — the archive rows are the record.
func (m *MemoryArchiveService) ArchiveTask(ctx context.Context, task db.AgentTaskQueue, result []byte) error {
	if m == nil || !task.ID.Valid {
		return nil
	}
	var workspaceID pgtype.UUID
	switch {
	case task.IssueID.Valid:
		issue, err := m.Queries.GetIssue(ctx, task.IssueID)
		if err != nil {
			return fmt.Errorf("get issue: %w", err)
		}
		workspaceID = issue.WorkspaceID
	case task.ChatSessionID.Valid:
		cs, err := m.Queries.GetChatSession(ctx, task.ChatSessionID)
		if err != nil {
			return fmt.Errorf("get chat session: %w", err)
		}
		workspaceID = cs.WorkspaceID
	default:
		return nil
	}
	msgs := taskResultToMessages(task, result)
	if len(msgs) == 0 {
		return nil
	}
	in := memorycompact.Input{
		ScopeType: "task",
		ScopeID:   util.UUIDToString(task.ID),
		Messages:  msgs,
	}
	return m.runArchive(ctx, in, util.UUIDToString(workspaceID), util.UUIDToString(task.AgentID))
}

// IssueMemorySummary returns the low-gradient (T1 + T2) summary to inject
// into a fresh issue session's prompt, or "" when there is no archive.
func (m *MemoryArchiveService) IssueMemorySummary(ctx context.Context, issueID pgtype.UUID) string {
	return m.scopeSummary(ctx, "issue", issueID)
}

// ChatSessionMemorySummary returns the T1+T2 summary to inject into a fresh
// chat session's prompt after compaction, or "" when there is no archive.
func (m *MemoryArchiveService) ChatSessionMemorySummary(ctx context.Context, chatSessionID pgtype.UUID) string {
	return m.scopeSummary(ctx, "chat_session", chatSessionID)
}

// scopeSummary builds the low-gradient (T1 + T2) summary for any scope.
// Always best-effort: any error yields "".
func (m *MemoryArchiveService) scopeSummary(ctx context.Context, scopeType string, id pgtype.UUID) string {
	if m == nil || !id.Valid {
		return ""
	}
	scope := util.UUIDToString(id)
	t1 := m.latestLevel(ctx, scopeType, scope, string(memorycompact.LevelT1))
	t2 := m.latestLevel(ctx, scopeType, scope, string(memorycompact.LevelT2))
	switch {
	case t1 != "" && t2 != "":
		return t1 + "\n\n" + t2
	case t1 != "":
		return t1
	default:
		return t2
	}
}

func (m *MemoryArchiveService) latestLevel(ctx context.Context, scopeType, scopeID, level string) string {
	id, err := util.ParseUUID(scopeID)
	if err != nil {
		return ""
	}
	row, err := m.Queries.GetLatestMemoryArchiveLevel(ctx, db.GetLatestMemoryArchiveLevelParams{
		ScopeType: scopeType,
		ScopeID:   id,
		Level:     level,
	})
	if err != nil {
		return ""
	}
	return row.Content
}

func (m *MemoryArchiveService) issueArchiveComments(ctx context.Context, issueID, workspaceID pgtype.UUID, compactedAt pgtype.Timestamptz) ([]db.Comment, error) {
	if compactedAt.Valid {
		return m.Queries.ListCommentsSinceForIssue(ctx, db.ListCommentsSinceForIssueParams{
			IssueID:     issueID,
			WorkspaceID: workspaceID,
			CreatedAt:   compactedAt,
			Limit:       archiveCommentFetchLimit,
		})
	}
	return m.Queries.ListCommentsForIssue(ctx, db.ListCommentsForIssueParams{
		IssueID:     issueID,
		WorkspaceID: workspaceID,
		Limit:       archiveCommentFetchLimit,
	})
}

func (m *MemoryArchiveService) withPriorSummary(ctx context.Context, scopeType string, id pgtype.UUID, compactedAt pgtype.Timestamptz, msgs []memorycompact.Message) []memorycompact.Message {
	if !compactedAt.Valid || len(msgs) == 0 {
		return msgs
	}
	prior := strings.TrimSpace(m.scopeSummary(ctx, scopeType, id))
	return prependPriorSummary(prior, compactedAt, msgs)
}

func prependPriorSummary(prior string, compactedAt pgtype.Timestamptz, msgs []memorycompact.Message) []memorycompact.Message {
	prior = strings.TrimSpace(prior)
	if prior == "" || !compactedAt.Valid || len(msgs) == 0 {
		return msgs
	}
	createdAt := compactedAt.Time
	if createdAt.IsZero() {
		createdAt = msgs[0].CreatedAt
	}
	out := make([]memorycompact.Message, 0, len(msgs)+1)
	out = append(out, memorycompact.Message{
		Author:    "memory_archive",
		Role:      "system",
		Content:   "Previous compacted memory summary:\n" + prior,
		CreatedAt: createdAt,
	})
	out = append(out, msgs...)
	return out
}

// taskResultToMessages builds the compactor input for a task-level archive:
// the trigger context (what the run was asked to do) plus the final output.
func taskResultToMessages(task db.AgentTaskQueue, result []byte) []memorycompact.Message {
	var msgs []memorycompact.Message
	if task.TriggerSummary.Valid && strings.TrimSpace(task.TriggerSummary.String) != "" {
		msgs = append(msgs, memorycompact.Message{
			Author:    "trigger",
			Role:      "user",
			Content:   task.TriggerSummary.String,
			CreatedAt: task.CreatedAt.Time,
		})
	}
	if out := strings.TrimSpace(extractTaskOutput(result)); out != "" {
		msgs = append(msgs, memorycompact.Message{
			Author:    "agent",
			Role:      "assistant",
			Content:   out,
			CreatedAt: task.CompletedAt.Time,
		})
	}
	return msgs
}

// extractTaskOutput pulls the agent's final output text from a completion
// result payload, applying the same backslash-unescape as the comment path.
// Returns "" on any parse failure (fail-safe).
func extractTaskOutput(result []byte) string {
	var payload protocol.TaskCompletedPayload
	if err := json.Unmarshal(result, &payload); err != nil {
		return ""
	}
	return util.UnescapeBackslashEscapes(payload.Output)
}

func (m *MemoryArchiveService) runArchive(ctx context.Context, in memorycompact.Input, workspaceID, agentID string) error {
	return m.inTx(ctx, func(q *db.Queries) error {
		arch := memorycompact.Archiver{Compactor: m.Compactor, Store: dbArchiveStore{q: q}}
		_, err := arch.Archive(ctx, in, workspaceID, agentID)
		return err
	})
}

func (m *MemoryArchiveService) inTx(ctx context.Context, fn func(*db.Queries) error) error {
	if m.TxStarter == nil {
		return fn(m.Queries)
	}
	tx, err := m.TxStarter.Begin(ctx)
	if err != nil {
		return fmt.Errorf("begin tx: %w", err)
	}
	defer tx.Rollback(ctx)
	if err := fn(m.Queries.WithTx(tx)); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

// commentsToMessages normalises issue comments into compactor input.
func commentsToMessages(comments []db.Comment) []memorycompact.Message {
	out := make([]memorycompact.Message, 0, len(comments))
	for _, c := range comments {
		out = append(out, memorycompact.Message{
			Author:    c.AuthorType,
			Role:      c.AuthorType,
			Content:   c.Content,
			CreatedAt: c.CreatedAt.Time,
		})
	}
	return out
}

// chatMessagesToMessages normalises chat messages into compactor input.
func chatMessagesToMessages(rows []db.ChatMessage) []memorycompact.Message {
	out := make([]memorycompact.Message, 0, len(rows))
	for _, r := range rows {
		out = append(out, memorycompact.Message{
			Author:    r.Role,
			Role:      r.Role,
			Content:   r.Content,
			CreatedAt: r.CreatedAt.Time,
		})
	}
	return out
}

// filterChatSince returns only the messages created strictly after `since`
// (the last compaction point). A NULL `since` means never compacted, so all
// messages are returned. Builds a new slice — never mutates the input.
func filterChatSince(rows []db.ChatMessage, since pgtype.Timestamptz) []db.ChatMessage {
	if !since.Valid {
		return rows
	}
	out := make([]db.ChatMessage, 0, len(rows))
	for _, r := range rows {
		if r.CreatedAt.Valid && r.CreatedAt.Time.After(since.Time) {
			out = append(out, r)
		}
	}
	return out
}

// dbArchiveStore adapts a (tx-bound) *db.Queries to memorycompact.Store.
type dbArchiveStore struct{ q *db.Queries }

func (s dbArchiveStore) SaveArchive(ctx context.Context, rec memorycompact.ArchiveRecord) error {
	workspaceID, err := util.ParseUUID(rec.WorkspaceID)
	if err != nil {
		return fmt.Errorf("parse workspace id: %w", err)
	}
	scopeID, err := util.ParseUUID(rec.ScopeID)
	if err != nil {
		return fmt.Errorf("parse scope id: %w", err)
	}
	var agentID pgtype.UUID
	if rec.CreatedByAgentID != "" {
		// Best-effort: an unparyseable agent id just leaves the column NULL.
		agentID, _ = util.ParseUUID(rec.CreatedByAgentID)
	}
	_, err = s.q.CreateMemoryArchive(ctx, db.CreateMemoryArchiveParams{
		WorkspaceID:      workspaceID,
		ScopeType:        rec.ScopeType,
		ScopeID:          scopeID,
		Level:            rec.Level,
		Content:          rec.Content,
		SourceCount:      int32(rec.Count),
		Generator:        rec.Generator,
		SourceFrom:       tsOrNull(rec.SourceFrom),
		SourceTo:         tsOrNull(rec.SourceTo),
		CreatedByAgentID: agentID,
	})
	return err
}

func (s dbArchiveStore) MarkCompacted(ctx context.Context, scopeType, scopeID string) error {
	id, err := util.ParseUUID(scopeID)
	if err != nil {
		return fmt.Errorf("parse scope id: %w", err)
	}
	switch scopeType {
	case "chat_session":
		return s.q.MarkChatSessionCompacted(ctx, id)
	case "issue":
		return s.q.MarkIssueMemoryCompacted(ctx, id)
	default:
		// task / squad markers are not persisted as columns yet; the archive
		// rows themselves are the record. No-op keeps the contract idempotent.
		return nil
	}
}

func tsOrNull(t time.Time) pgtype.Timestamptz {
	if t.IsZero() {
		return pgtype.Timestamptz{}
	}
	return pgtype.Timestamptz{Time: t, Valid: true}
}
