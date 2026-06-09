package service

import (
	"testing"
	"time"
)

func mustTime(s string) time.Time {
	t, err := time.Parse(time.RFC3339, s)
	if err != nil {
		panic(err)
	}
	return t
}

func TestParseVerdict(t *testing.T) {
	cases := []struct {
		name   string
		in     string
		want   string
		wantOK bool
	}{
		// Canonical forms.
		{"plain pass", "VERDICT: PASS", "pass", true},
		{"plain fail", "VERDICT: FAIL", "fail", true},
		{"lowercase", "verdict: pass", "pass", true},
		{"mixed case", "Verdict: Fail", "fail", true},
		// Affixes / surrounding prose.
		{"prefix prose", "审计完成。VERDICT: PASS 后续交给门禁。", "pass", true},
		{"suffix prose", "VERDICT: FAIL — 详见下方原因。", "fail", true},
		{"bracketed", "【VERDICT: FAIL】审计未通过", "fail", true},
		{"equals separator", "审计结论 VERDICT = PASS ✅", "pass", true},
		{"fullwidth colon", "VERDICT：PASS", "pass", true},
		{"passed word", "Final verdict: passed all checks", "pass", true},
		{"failed word", "verdict: failed on step 3", "fail", true},
		// Chinese markers / results.
		{"cn marker pass", "结论：通过", "pass", true},
		{"cn marker fail", "结论：失败", "fail", true},
		{"cn negation fail adjacent", "判定：不通过", "fail", true},
		{"cn negation fail prose", "审计结论 VERDICT 审计不通过，需返工", "fail", true},
		{"cn traditional pass", "裁决：通過", "pass", true},
		{"cn mixed", "审计结论 verdict 最终 通过 ✅", "pass", true},
		// Negative / ambiguous.
		{"no marker", "这次改动看起来不错，应该能 pass", "", false},
		{"empty", "", "", false},
		{"whitespace", "   \n  ", "", false},
		{"marker only no result", "VERDICT: 待定", "", false},
		{"both polarities ambiguous", "VERDICT 包含 pass 和 fail 两种可能", "", false},
		{"marker plus unrelated", "结论待补充", "", false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, ok := ParseVerdict(tc.in)
			if ok != tc.wantOK || got != tc.want {
				t.Fatalf("ParseVerdict(%q) = (%q, %v), want (%q, %v)", tc.in, got, ok, tc.want, tc.wantOK)
			}
		})
	}
}

func TestOrchConfigActionEnabled(t *testing.T) {
	cases := []struct {
		name   string
		cfg    orchConfig
		action string
		want   bool
	}{
		{"disabled blocks all", orchConfig{enabled: false}, ActionAudit, false},
		{"enabled nil actions defaults on", orchConfig{enabled: true}, ActionGate, true},
		{"enabled empty actions defaults on", orchConfig{enabled: true, actions: map[string]bool{}}, ActionRework, true},
		{"explicit off", orchConfig{enabled: true, actions: map[string]bool{ActionGate: false}}, ActionGate, false},
		{"explicit on", orchConfig{enabled: true, actions: map[string]bool{ActionGate: true}}, ActionGate, true},
		{"unrelated key stays default on", orchConfig{enabled: true, actions: map[string]bool{ActionGate: false}}, ActionAudit, true},
		{"disabled overrides explicit on", orchConfig{enabled: false, actions: map[string]bool{ActionAudit: true}}, ActionAudit, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := tc.cfg.actionEnabled(tc.action); got != tc.want {
				t.Fatalf("actionEnabled(%q) = %v, want %v", tc.action, got, tc.want)
			}
		})
	}
}

func TestStallKeyStableAndDistinct(t *testing.T) {
	a := stallKey(StageAudit, mustTime("2026-06-09T10:00:00Z"))
	aSame := stallKey(StageAudit, mustTime("2026-06-09T10:00:00Z"))
	bStage := stallKey(StageGate, mustTime("2026-06-09T10:00:00Z"))
	bTime := stallKey(StageAudit, mustTime("2026-06-09T10:05:00Z"))
	if a != aSame {
		t.Fatalf("stallKey not stable: %q vs %q", a, aSame)
	}
	if a == bStage || a == bTime {
		t.Fatalf("stallKey should differ on stage/time: %q %q %q", a, bStage, bTime)
	}
}
