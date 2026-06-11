package memorycompact

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"
)

func ts(s string) time.Time {
	t, err := time.Parse("2006-01-02 15:04", s)
	if err != nil {
		panic(err)
	}
	return t
}

func sampleInput() Input {
	return Input{
		ScopeType: "issue",
		ScopeID:   "issue-1",
		Messages: []Message{
			// Intentionally out of chronological order to exercise sorting.
			{Author: "assistant", Role: "assistant", Content: "I fixed the login redirect bug. Decision: ship to staging.", CreatedAt: ts("2026-06-01 10:05")},
			{Author: "user", Role: "user", Content: "The login page keeps redirecting in a loop.\nPlease investigate.", CreatedAt: ts("2026-06-01 10:00")},
			{Author: "assistant", Role: "assistant", Content: strings.Repeat("verbose log line ", 100), CreatedAt: ts("2026-06-01 10:10")},
		},
	}
}

func TestDeterministicCompact_AllLevelsPopulated(t *testing.T) {
	lv, err := DeterministicCompactor{}.Compact(context.Background(), sampleInput())
	if err != nil {
		t.Fatalf("deterministic compactor must never error, got %v", err)
	}
	for _, level := range AllLevels {
		if strings.TrimSpace(lv.Get(level)) == "" {
			t.Errorf("level %s is empty", level)
		}
	}
	if lv.Generator != "deterministic" {
		t.Errorf("generator = %q, want deterministic", lv.Generator)
	}
	if lv.Count != 3 {
		t.Errorf("count = %d, want 3", lv.Count)
	}
}

func TestDeterministicCompact_GradientShrinks(t *testing.T) {
	lv, _ := DeterministicCompactor{}.Compact(context.Background(), sampleInput())
	if len(lv.T1) >= len(lv.T4) {
		t.Errorf("T1 (%d) should be much shorter than T4 (%d)", len(lv.T1), len(lv.T4))
	}
	if len(lv.T3) >= len(lv.T4) {
		t.Errorf("T3 (%d) should be shorter than T4 (%d)", len(lv.T3), len(lv.T4))
	}
}

func TestDeterministicCompact_SortsByTime(t *testing.T) {
	lv, _ := DeterministicCompactor{}.Compact(context.Background(), sampleInput())
	if !lv.SourceFrom.Equal(ts("2026-06-01 10:00")) {
		t.Errorf("SourceFrom = %v, want 10:00", lv.SourceFrom)
	}
	if !lv.SourceTo.Equal(ts("2026-06-01 10:10")) {
		t.Errorf("SourceTo = %v, want 10:10", lv.SourceTo)
	}
	// T1 headline must reference the chronologically last message.
	if !strings.Contains(lv.T4, "redirecting in a loop") {
		t.Errorf("T4 missing earliest message content")
	}
}

func TestDeterministicCompact_KeyLinesSurfaceDecisions(t *testing.T) {
	lv, _ := DeterministicCompactor{}.Compact(context.Background(), sampleInput())
	if !strings.Contains(strings.ToLower(lv.T2), "decision") {
		t.Errorf("T2 should surface the 'Decision:' line, got:\n%s", lv.T2)
	}
}

func TestDeterministicCompact_CarriesPriorSummaryIntoInjectedLevels(t *testing.T) {
	in := Input{
		ScopeType: "chat_session",
		ScopeID:   "chat-1",
		Messages: []Message{
			{
				Author:    "memory_archive",
				Role:      "system",
				Content:   "Previous compacted memory summary:\nT1 old decision: use OAuth.\n\nT2 old blocker resolved.",
				CreatedAt: ts("2026-06-01 10:00"),
			},
			{
				Author:    "user",
				Role:      "user",
				Content:   "New follow-up: verify mobile login.",
				CreatedAt: ts("2026-06-01 10:10"),
			},
		},
	}

	lv, err := DeterministicCompactor{}.Compact(context.Background(), in)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if lv.Count != 1 {
		t.Errorf("source count must exclude synthetic carried summary, got %d", lv.Count)
	}
	if !lv.SourceFrom.Equal(ts("2026-06-01 10:10")) || !lv.SourceTo.Equal(ts("2026-06-01 10:10")) {
		t.Errorf("source window must be the new real-message window, got %v -> %v", lv.SourceFrom, lv.SourceTo)
	}
	if !strings.Contains(lv.T1, "Previous compacted summary carried forward") {
		t.Errorf("T1 must state prior memory was carried, got:\n%s", lv.T1)
	}
	if !strings.Contains(lv.T2, "T1 old decision: use OAuth") {
		t.Errorf("T2 must include carried prior summary, got:\n%s", lv.T2)
	}
	if !strings.Contains(lv.T2, "1 new records") {
		t.Errorf("T2 should label active-window records as new, got:\n%s", lv.T2)
	}
	if !strings.Contains(lv.T3, "memory_archive") || !strings.Contains(lv.T4, "memory_archive") {
		t.Errorf("T3/T4 should keep the carried summary for drill-down")
	}
}

func TestDeterministicCompact_Empty(t *testing.T) {
	lv, err := DeterministicCompactor{}.Compact(context.Background(), Input{ScopeType: "chat_session", ScopeID: "x"})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	for _, level := range AllLevels {
		if strings.TrimSpace(lv.Get(level)) == "" {
			t.Errorf("empty input still must yield non-empty placeholder for %s", level)
		}
	}
}

// fakeModel returns canned summaries, optionally failing on some levels.
type fakeModel struct {
	failLevels map[Level]bool
	outputs    map[Level]string
}

func (f fakeModel) ID() string { return "fake-qwen" }
func (f fakeModel) Summarize(_ context.Context, lv Level, _ []Message) (string, error) {
	if f.failLevels[lv] {
		return "", errors.New("model unavailable")
	}
	if f.outputs != nil {
		if out, ok := f.outputs[lv]; ok {
			return out, nil
		}
	}
	return "MODEL " + string(lv), nil
}

func TestModelCompactor_UsesModelWhenAvailable(t *testing.T) {
	mc := ModelCompactor{Client: fakeModel{}}
	lv, err := mc.Compact(context.Background(), sampleInput())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if lv.Generator != "fake-qwen" {
		t.Errorf("generator = %q, want fake-qwen", lv.Generator)
	}
	if lv.T1 != "MODEL T1" {
		t.Errorf("T1 = %q, want MODEL T1", lv.T1)
	}
}

func TestModelCompactor_FallsBackPerLevel(t *testing.T) {
	mc := ModelCompactor{Client: fakeModel{failLevels: map[Level]bool{LevelT4: true}}}
	lv, err := mc.Compact(context.Background(), sampleInput())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if lv.T1 != "MODEL T1" {
		t.Errorf("T1 should be model output, got %q", lv.T1)
	}
	// T4 model failed => deterministic digest must fill it, never empty.
	if strings.TrimSpace(lv.T4) == "" || strings.HasPrefix(lv.T4, "MODEL") {
		t.Errorf("T4 should fall back to deterministic, got %q", lv.T4)
	}
	if lv.Generator != "mixed:fake-qwen+deterministic" {
		t.Errorf("generator = %q, want mixed marker for partial fallback", lv.Generator)
	}
}

func TestModelCompactor_AllModelFailuresMarkDeterministic(t *testing.T) {
	mc := ModelCompactor{Client: fakeModel{failLevels: map[Level]bool{
		LevelT1: true, LevelT2: true, LevelT3: true, LevelT4: true,
	}}}
	lv, _ := mc.Compact(context.Background(), sampleInput())
	if lv.Generator != "deterministic" {
		t.Errorf("generator = %q, want deterministic when model fully fails", lv.Generator)
	}
}

func TestModelCompactor_BoundsModelOutputByLevel(t *testing.T) {
	mc := ModelCompactor{Client: fakeModel{outputs: map[Level]string{
		LevelT1: strings.Repeat("x", modelT1MaxRunes+50),
		LevelT2: strings.Repeat("y", modelT2MaxRunes+50),
	}}}
	lv, err := mc.Compact(context.Background(), sampleInput())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if got := len([]rune(lv.T1)); got != modelT1MaxRunes {
		t.Errorf("T1 length = %d, want cap %d", got, modelT1MaxRunes)
	}
	if got := len([]rune(lv.T2)); got != modelT2MaxRunes {
		t.Errorf("T2 length = %d, want cap %d", got, modelT2MaxRunes)
	}
	if !strings.HasSuffix(lv.T1, "…") || !strings.HasSuffix(lv.T2, "…") {
		t.Errorf("capped model outputs should end with ellipsis: T1 suffix=%q T2 suffix=%q", lv.T1[len(lv.T1)-1:], lv.T2[len(lv.T2)-1:])
	}
}

// recordingStore captures Archiver side effects.
type recordingStore struct {
	saved      []ArchiveRecord
	marked     []string
	failSaveAt int
	saveCalls  int
	failMark   bool
}

func (s *recordingStore) SaveArchive(_ context.Context, rec ArchiveRecord) error {
	s.saveCalls++
	if s.failSaveAt > 0 && s.saveCalls == s.failSaveAt {
		return errors.New("disk full")
	}
	s.saved = append(s.saved, rec)
	return nil
}
func (s *recordingStore) MarkCompacted(_ context.Context, scopeType, scopeID string) error {
	if s.failMark {
		return errors.New("mark failed")
	}
	s.marked = append(s.marked, scopeType+"/"+scopeID)
	return nil
}

func TestArchiver_SavesThenMarks(t *testing.T) {
	store := &recordingStore{}
	a := Archiver{Compactor: DeterministicCompactor{}, Store: store}
	_, err := a.Archive(context.Background(), sampleInput(), "ws-1", "agent-1")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(store.saved) != 4 {
		t.Errorf("expected 4 levels saved, got %d", len(store.saved))
	}
	if len(store.marked) != 1 || store.marked[0] != "issue/issue-1" {
		t.Errorf("expected scope marked once, got %v", store.marked)
	}
}

func TestArchiver_FailSafe_NoMarkOnSaveFailure(t *testing.T) {
	store := &recordingStore{failSaveAt: 2}
	a := Archiver{Compactor: DeterministicCompactor{}, Store: store}
	_, err := a.Archive(context.Background(), sampleInput(), "ws-1", "agent-1")
	if err == nil {
		t.Fatal("expected error when a level save fails")
	}
	if len(store.marked) != 0 {
		t.Errorf("scope must NOT be marked compacted when a save fails; marked=%v", store.marked)
	}
}
