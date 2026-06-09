package service

import (
	"encoding/json"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgtype"

	"github.com/multica-ai/multica/server/internal/service/memorycompact"
	db "github.com/multica-ai/multica/server/pkg/db/generated"
	"github.com/multica-ai/multica/server/pkg/protocol"
)

func TestExtractTaskOutput(t *testing.T) {
	result, _ := json.Marshal(protocol.TaskCompletedPayload{Output: "fixed the login bug"})
	if got := extractTaskOutput(result); got != "fixed the login bug" {
		t.Errorf("extractTaskOutput = %q, want 'fixed the login bug'", got)
	}
	if got := extractTaskOutput([]byte("not json")); got != "" {
		t.Errorf("extractTaskOutput on bad JSON should be empty, got %q", got)
	}
}

func TestTaskResultToMessages(t *testing.T) {
	t0 := time.Date(2026, 6, 1, 12, 0, 0, 0, time.UTC)
	result, _ := json.Marshal(protocol.TaskCompletedPayload{Output: "done: shipped"})
	task := db.AgentTaskQueue{
		TriggerSummary: pgtype.Text{String: "investigate login redirect", Valid: true},
		CreatedAt:      pgtype.Timestamptz{Time: t0, Valid: true},
		CompletedAt:    pgtype.Timestamptz{Time: t0.Add(time.Minute), Valid: true},
	}
	msgs := taskResultToMessages(task, result)
	if len(msgs) != 2 {
		t.Fatalf("expected trigger + output = 2 messages, got %d", len(msgs))
	}
	if msgs[0].Content != "investigate login redirect" || msgs[0].Role != "user" {
		t.Errorf("msg[0] (trigger) = %+v", msgs[0])
	}
	if msgs[1].Content != "done: shipped" || msgs[1].Role != "assistant" {
		t.Errorf("msg[1] (output) = %+v", msgs[1])
	}

	// No trigger summary, no output => no archive (nothing to record).
	empty := taskResultToMessages(db.AgentTaskQueue{}, []byte("not json"))
	if len(empty) != 0 {
		t.Errorf("empty task should yield no messages, got %d", len(empty))
	}
}

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

func TestChatSessionNeedsCompaction(t *testing.T) {
	cases := []struct {
		name           string
		count, byteLen int64
		want           bool
	}{
		{"small", 5, 1000, false},
		{"just-under-count", chatCompactMessageThreshold - 1, 1000, false},
		{"at-count", chatCompactMessageThreshold, 1000, true},
		{"at-bytes", 3, chatCompactByteThreshold, true},
		{"over-bytes-under-count", 2, chatCompactByteThreshold + 50, true},
	}
	for _, c := range cases {
		if got := ChatSessionNeedsCompaction(c.count, c.byteLen); got != c.want {
			t.Errorf("%s: ChatSessionNeedsCompaction(%d,%d) = %v, want %v", c.name, c.count, c.byteLen, got, c.want)
		}
	}
}

func TestChatMessagesToMessages(t *testing.T) {
	when := time.Date(2026, 6, 1, 9, 0, 0, 0, time.UTC)
	rows := []db.ChatMessage{
		{Role: "user", Content: "hi", CreatedAt: pgtype.Timestamptz{Time: when, Valid: true}},
		{Role: "assistant", Content: "hello", CreatedAt: pgtype.Timestamptz{Time: when.Add(time.Minute), Valid: true}},
	}
	msgs := chatMessagesToMessages(rows)
	if len(msgs) != 2 || msgs[0].Role != "user" || msgs[1].Content != "hello" {
		t.Fatalf("unexpected conversion: %+v", msgs)
	}
}

func TestFilterChatSince(t *testing.T) {
	t0 := time.Date(2026, 6, 1, 9, 0, 0, 0, time.UTC)
	rows := []db.ChatMessage{
		{Content: "old", CreatedAt: pgtype.Timestamptz{Time: t0, Valid: true}},
		{Content: "boundary", CreatedAt: pgtype.Timestamptz{Time: t0.Add(time.Minute), Valid: true}},
		{Content: "new", CreatedAt: pgtype.Timestamptz{Time: t0.Add(2 * time.Minute), Valid: true}},
	}
	// NULL since => everything (never compacted).
	if got := filterChatSince(rows, pgtype.Timestamptz{}); len(got) != 3 {
		t.Errorf("NULL since should return all, got %d", len(got))
	}
	// since = boundary => only strictly-after ("new").
	got := filterChatSince(rows, pgtype.Timestamptz{Time: t0.Add(time.Minute), Valid: true})
	if len(got) != 1 || got[0].Content != "new" {
		t.Errorf("since boundary should return only newer messages, got %+v", got)
	}
	// Input must not be mutated.
	if rows[0].Content != "old" {
		t.Errorf("filterChatSince mutated its input")
	}
}

func TestPrependPriorSummary(t *testing.T) {
	t0 := time.Date(2026, 6, 1, 9, 0, 0, 0, time.UTC)
	msgs := []memorycompact.Message{{
		Author:    "user",
		Role:      "user",
		Content:   "new active-window message",
		CreatedAt: t0.Add(time.Minute),
	}}

	got := prependPriorSummary("T1 old\n\nT2 old", pgtype.Timestamptz{Time: t0, Valid: true}, msgs)
	if len(got) != 2 {
		t.Fatalf("expected prior summary + original message, got %d", len(got))
	}
	if got[0].Author != "memory_archive" || got[0].Role != "system" {
		t.Fatalf("prior summary message identity wrong: %+v", got[0])
	}
	if got[0].Content != "Previous compacted memory summary:\nT1 old\n\nT2 old" {
		t.Errorf("prior summary content = %q", got[0].Content)
	}
	if got[1].Content != "new active-window message" {
		t.Errorf("original message not preserved: %+v", got[1])
	}

	unchanged := prependPriorSummary("   ", pgtype.Timestamptz{Time: t0, Valid: true}, msgs)
	if len(unchanged) != 1 || unchanged[0].Content != msgs[0].Content {
		t.Errorf("blank prior summary should leave messages unchanged: %+v", unchanged)
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
