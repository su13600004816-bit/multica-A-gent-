package service

import (
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgtype"

	db "github.com/multica-ai/multica/server/pkg/db/generated"
)

func TestCommentsToMessages(t *testing.T) {
	when := time.Date(2026, 6, 1, 10, 0, 0, 0, time.UTC)
	comments := []db.Comment{
		{AuthorType: "member", Content: "hello", CreatedAt: pgtype.Timestamptz{Time: when, Valid: true}},
		{AuthorType: "agent", Content: "fixed it", CreatedAt: pgtype.Timestamptz{Time: when.Add(time.Minute), Valid: true}},
	}
	msgs := commentsToMessages(comments)
	if len(msgs) != 2 {
		t.Fatalf("got %d messages, want 2", len(msgs))
	}
	if msgs[0].Role != "member" || msgs[0].Author != "member" || msgs[0].Content != "hello" {
		t.Errorf("msg[0] = %+v", msgs[0])
	}
	if !msgs[1].CreatedAt.Equal(when.Add(time.Minute)) {
		t.Errorf("msg[1] time = %v", msgs[1].CreatedAt)
	}
}

func TestTsOrNull(t *testing.T) {
	if got := tsOrNull(time.Time{}); got.Valid {
		t.Errorf("zero time must map to invalid (NULL) timestamptz, got %+v", got)
	}
	when := time.Date(2026, 6, 1, 10, 0, 0, 0, time.UTC)
	got := tsOrNull(when)
	if !got.Valid || !got.Time.Equal(when) {
		t.Errorf("non-zero time mapping wrong: %+v", got)
	}
}

// TestNewMemoryArchiveService_NilCompactorDefaults verifies the service is
// always safe to call even when constructed without a compactor.
func TestNewMemoryArchiveService_NilCompactorDefaults(t *testing.T) {
	svc := NewMemoryArchiveService(nil, nil, nil)
	if svc.Compactor == nil {
		t.Fatal("nil compactor must default to the deterministic fail-safe")
	}
}

// TestArchiveIssue_NilReceiverAndInvalidID is a guard: the best-effort entry
// points must no-op (not panic) on the nil/invalid paths CompleteTask relies
// on for fail-safety.
func TestArchiveIssue_NilSafe(t *testing.T) {
	var svc *MemoryArchiveService
	if err := svc.ArchiveIssue(t.Context(), pgtype.UUID{}, pgtype.UUID{}); err != nil {
		t.Errorf("nil service ArchiveIssue should be a no-op, got %v", err)
	}
	if s := svc.IssueMemorySummary(t.Context(), pgtype.UUID{}); s != "" {
		t.Errorf("nil service IssueMemorySummary should be empty, got %q", s)
	}
	real := NewMemoryArchiveService(nil, nil, nil)
	if err := real.ArchiveIssue(t.Context(), pgtype.UUID{}, pgtype.UUID{}); err != nil {
		t.Errorf("invalid issue id ArchiveIssue should be a no-op, got %v", err)
	}
}
