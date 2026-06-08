// Package memorycompact turns a span of chat messages or issue comments
// into a four-level memory archive (T1..T4) for PL-91.
//
// The point of the feature is token 止血: a fresh agent session must carry
// only a low-gradient summary instead of re-ingesting the entire transcript
// every turn. T1 is the shortest headline (always injected), T4 is the
// richest near-verbatim digest (only fetched on explicit drill-down).
//
// Two compactors are provided:
//
//   - DeterministicCompactor — no model, no network, no randomness. This is
//     the fail-safe: it ALWAYS succeeds, so an archive can be produced even
//     when the Qwen bridge is down or unconfigured. Compaction failure must
//     never block 止血 or, worse, drop the original transcript.
//   - ModelCompactor — wraps a ModelClient (e.g. a future Qwen bridge) and
//     transparently falls back to DeterministicCompactor on any error.
//
// Nothing in this package touches the database or deletes source rows; it is
// pure transformation and is unit-testable without a DB.
package memorycompact

import (
	"context"
	"fmt"
	"sort"
	"strings"
	"time"
)

// Level identifies a gradient in the archive. Lower number = less detail.
type Level string

const (
	LevelT1 Level = "T1" // one-line headline; injected into every fresh session
	LevelT2 Level = "T2" // short structured summary; injected alongside T1
	LevelT3 Level = "T3" // per-message condensed digest; drill-down
	LevelT4 Level = "T4" // near-verbatim bounded transcript; deepest drill-down
)

// AllLevels is the canonical T1->T4 ordering.
var AllLevels = []Level{LevelT1, LevelT2, LevelT3, LevelT4}

// Message is one source record (a chat_message or an issue comment),
// normalised so the compactor does not depend on db row types.
type Message struct {
	Author    string // display name or role ("user", "assistant", agent name)
	Role      string // "user" | "assistant" | "agent" | "member" | "system"
	Content   string
	CreatedAt time.Time
}

// Input is the span to compact.
type Input struct {
	ScopeType string // "chat_session" | "issue" | "task" | "squad"
	ScopeID   string
	Messages  []Message
}

// Levels holds the rendered content for each gradient, plus the audit window
// the caller persists into memory_archive.source_from / source_to.
type Levels struct {
	Generator  string // "deterministic" or a model id
	T1, T2, T3 string
	T4         string
	SourceFrom time.Time
	SourceTo   time.Time
	Count      int
}

// Get returns the rendered content for a level.
func (l Levels) Get(lv Level) string {
	switch lv {
	case LevelT1:
		return l.T1
	case LevelT2:
		return l.T2
	case LevelT3:
		return l.T3
	case LevelT4:
		return l.T4
	}
	return ""
}

// Compactor produces the four levels for an input span.
type Compactor interface {
	Compact(ctx context.Context, in Input) (Levels, error)
}

// ModelClient is the abstraction over an external summarising model (the
// future Qwen bridge). Keeping it an interface means the bridge can be wired
// later without touching callers; until then ModelCompactor degrades to the
// deterministic fallback.
type ModelClient interface {
	// Summarize returns a summary of msgs at the requested detail level. It
	// must be deterministic-friendly (no side effects on the source) and may
	// return an error, in which case the caller falls back.
	Summarize(ctx context.Context, level Level, msgs []Message) (string, error)
	// ID identifies the model for the memory_archive.generator column.
	ID() string
}

// DeterministicCompactor builds all four levels with pure string reduction.
// It never errors and never calls out, which makes it the safe default and
// the floor every other compactor falls back to.
type DeterministicCompactor struct {
	// PerMessageT4Cap bounds each message in the T4 digest; 0 uses the
	// default. This is what keeps even the deepest archive from reproducing
	// an unbounded transcript.
	PerMessageT4Cap int
	// PerMessageT3Cap bounds each message in the T3 digest; 0 uses default.
	PerMessageT3Cap int
}

const (
	defaultT4Cap = 600
	defaultT3Cap = 160
)

// Compact implements Compactor. It always returns a nil error.
func (d DeterministicCompactor) Compact(_ context.Context, in Input) (Levels, error) {
	return d.render(in, "deterministic"), nil
}

func (d DeterministicCompactor) render(in Input, generator string) Levels {
	msgs := normalize(in.Messages)
	from, to := window(msgs)
	lv := Levels{
		Generator:  generator,
		SourceFrom: from,
		SourceTo:   to,
		Count:      len(msgs),
	}
	if len(msgs) == 0 {
		lv.T1 = "(empty conversation; nothing to archive)"
		lv.T2, lv.T3, lv.T4 = lv.T1, lv.T1, lv.T1
		return lv
	}

	t4Cap := d.PerMessageT4Cap
	if t4Cap <= 0 {
		t4Cap = defaultT4Cap
	}
	t3Cap := d.PerMessageT3Cap
	if t3Cap <= 0 {
		t3Cap = defaultT3Cap
	}

	lv.T4 = renderDigest(msgs, t4Cap)
	lv.T3 = renderDigest(msgs, t3Cap)
	lv.T2 = renderT2(in.ScopeType, msgs, from, to)
	lv.T1 = renderT1(in.ScopeType, msgs, from, to)
	return lv
}

// renderDigest emits one bounded block per message, oldest first.
func renderDigest(msgs []Message, cap int) string {
	var b strings.Builder
	for _, m := range msgs {
		fmt.Fprintf(&b, "[%s] %s: %s\n", m.CreatedAt.UTC().Format("2006-01-02 15:04"), speaker(m), truncate(collapse(m.Content), cap))
	}
	return strings.TrimRight(b.String(), "\n")
}

// renderT2 is a short structured summary: window, participants, turn counts,
// and any lines that look like decisions / blockers / outcomes.
func renderT2(scope string, msgs []Message, from, to time.Time) string {
	roleCount := map[string]int{}
	participants := map[string]struct{}{}
	for _, m := range msgs {
		roleCount[strings.ToLower(m.Role)]++
		participants[speaker(m)] = struct{}{}
	}
	var b strings.Builder
	fmt.Fprintf(&b, "Scope: %s | %d records | %s -> %s\n",
		scope, len(msgs), from.UTC().Format("2006-01-02 15:04"), to.UTC().Format("2006-01-02 15:04"))
	fmt.Fprintf(&b, "Participants: %s\n", strings.Join(sortedKeys(participants), ", "))

	roles := make([]string, 0, len(roleCount))
	for r := range roleCount {
		roles = append(roles, r)
	}
	sort.Strings(roles)
	parts := make([]string, 0, len(roles))
	for _, r := range roles {
		parts = append(parts, fmt.Sprintf("%s=%d", r, roleCount[r]))
	}
	fmt.Fprintf(&b, "Turns: %s\n", strings.Join(parts, " "))

	if highlights := keyLines(msgs, 8); len(highlights) > 0 {
		b.WriteString("Key points:\n")
		for _, h := range highlights {
			fmt.Fprintf(&b, "- %s\n", h)
		}
	}
	return strings.TrimRight(b.String(), "\n")
}

// renderT1 is the one-paragraph headline injected into every fresh session.
func renderT1(scope string, msgs []Message, from, to time.Time) string {
	last := msgs[len(msgs)-1]
	return fmt.Sprintf(
		"Prior %s context compacted: %d messages from %s to %s. Latest (%s): %s",
		scope, len(msgs),
		from.UTC().Format("2006-01-02 15:04"),
		to.UTC().Format("2006-01-02 15:04"),
		speaker(last), truncate(collapse(last.Content), 240),
	)
}

// keyLines surfaces lines that read like decisions, blockers, or results so
// the cheap summaries still retain the load-bearing facts.
func keyLines(msgs []Message, max int) []string {
	markers := []string{
		"decided", "decision", "conclusion", "blocked", "blocker", "fixed",
		"error", "failed", "todo", "next step", "结论", "决定", "阻塞", "已修复",
		"失败", "错误", "下一步", "待办",
	}
	var out []string
	for _, m := range msgs {
		for _, line := range strings.Split(m.Content, "\n") {
			lc := strings.ToLower(line)
			for _, mk := range markers {
				if strings.Contains(lc, mk) {
					out = append(out, fmt.Sprintf("%s: %s", speaker(m), truncate(collapse(line), 160)))
					break
				}
			}
			if len(out) >= max {
				return out
			}
		}
	}
	return out
}

// ModelCompactor uses a ModelClient when available and falls back to the
// deterministic compactor on any failure. The deterministic levels are
// computed first so a partial model failure still yields a complete archive.
type ModelCompactor struct {
	Client   ModelClient
	Fallback DeterministicCompactor
}

// Compact implements Compactor.
func (m ModelCompactor) Compact(ctx context.Context, in Input) (Levels, error) {
	base := m.Fallback.render(in, "deterministic")
	if m.Client == nil || len(in.Messages) == 0 {
		return base, nil
	}
	msgs := normalize(in.Messages)
	out := base
	out.Generator = m.Client.ID()
	anyOK := false
	for _, lv := range AllLevels {
		s, err := m.Client.Summarize(ctx, lv, msgs)
		if err != nil || strings.TrimSpace(s) == "" {
			continue // keep the deterministic level for this gradient
		}
		anyOK = true
		switch lv {
		case LevelT1:
			out.T1 = s
		case LevelT2:
			out.T2 = s
		case LevelT3:
			out.T3 = s
		case LevelT4:
			out.T4 = s
		}
	}
	if !anyOK {
		// Model produced nothing usable; record the honest generator so a
		// later run knows this archive can be upgraded.
		out.Generator = "deterministic"
	}
	return out, nil
}

// --- helpers ---

func normalize(in []Message) []Message {
	out := make([]Message, len(in))
	copy(out, in)
	sort.SliceStable(out, func(i, j int) bool { return out[i].CreatedAt.Before(out[j].CreatedAt) })
	return out
}

func window(msgs []Message) (time.Time, time.Time) {
	if len(msgs) == 0 {
		return time.Time{}, time.Time{}
	}
	return msgs[0].CreatedAt, msgs[len(msgs)-1].CreatedAt
}

func speaker(m Message) string {
	if s := strings.TrimSpace(m.Author); s != "" {
		return s
	}
	if s := strings.TrimSpace(m.Role); s != "" {
		return s
	}
	return "unknown"
}

func collapse(s string) string {
	return strings.Join(strings.Fields(s), " ")
}

func truncate(s string, max int) string {
	r := []rune(s)
	if len(r) <= max {
		return s
	}
	if max <= 1 {
		return string(r[:max])
	}
	return string(r[:max-1]) + "…"
}

func sortedKeys(m map[string]struct{}) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}
