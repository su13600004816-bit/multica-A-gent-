package service

import (
	"context"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5/pgtype"

	"github.com/multica-ai/multica/server/internal/service/memorycompact"
	"github.com/multica-ai/multica/server/internal/util"
	db "github.com/multica-ai/multica/server/pkg/db/generated"
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
	comments, err := m.Queries.ListCommentsForIssue(ctx, db.ListCommentsForIssueParams{
		IssueID:     issueID,
		WorkspaceID: issue.WorkspaceID,
		Limit:       archiveCommentFetchLimit,
	})
	if err != nil {
		return fmt.Errorf("list comments: %w", err)
	}
	if len(comments) == 0 {
		// Nothing to summarise; do not mark compacted (a fresh session with
		// no history to inject would just lose context for no token saving).
		return nil
	}
	in := memorycompact.Input{
		ScopeType: "issue",
		ScopeID:   util.UUIDToString(issueID),
		Messages:  commentsToMessages(comments),
	}
	return m.runArchive(ctx, in, util.UUIDToString(issue.WorkspaceID), util.UUIDToString(agentID))
}

// IssueMemorySummary returns the low-gradient (T1 + T2) summary to inject
// into a fresh session's prompt, or "" when the issue has no archive. Always
// best-effort: any error yields "".
func (m *MemoryArchiveService) IssueMemorySummary(ctx context.Context, issueID pgtype.UUID) string {
	if m == nil || !issueID.Valid {
		return ""
	}
	scope := util.UUIDToString(issueID)
	t1 := m.latestLevel(ctx, scope, string(memorycompact.LevelT1))
	t2 := m.latestLevel(ctx, scope, string(memorycompact.LevelT2))
	switch {
	case t1 != "" && t2 != "":
		return t1 + "\n\n" + t2
	case t1 != "":
		return t1
	default:
		return t2
	}
}

func (m *MemoryArchiveService) latestLevel(ctx context.Context, scopeID, level string) string {
	id, err := util.ParseUUID(scopeID)
	if err != nil {
		return ""
	}
	row, err := m.Queries.GetLatestMemoryArchiveLevel(ctx, db.GetLatestMemoryArchiveLevelParams{
		ScopeType: "issue",
		ScopeID:   id,
		Level:     level,
	})
	if err != nil {
		return ""
	}
	return row.Content
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
