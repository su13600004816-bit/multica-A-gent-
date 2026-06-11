package handler

import (
	"context"
	"strings"

	"github.com/jackc/pgx/v5/pgtype"
	db "github.com/multica-ai/multica/server/pkg/db/generated"
)

const issueDoneGateCommentScanLimit int32 = 2000

func (h *Handler) canMarkIssueDone(ctx context.Context, issue db.Issue) bool {
	if issueDoneGateMetadataPass(issue.Metadata) {
		return true
	}
	return h.issueHasTrustedPassVerdict(ctx, issue.ID, issue.WorkspaceID)
}

func issueDoneGateMetadataPass(raw []byte) bool {
	metadata := parseIssueMetadata(raw)
	return metadataStringEqual(metadata["gate_status"], "pass") &&
		metadataStringEqual(metadata["pipeline_status"], "done_real")
}

func metadataStringEqual(value any, want string) bool {
	s, ok := value.(string)
	return ok && strings.EqualFold(strings.TrimSpace(s), want)
}

func (h *Handler) issueHasTrustedPassVerdict(ctx context.Context, issueID, workspaceID pgtype.UUID) bool {
	comments, err := h.Queries.ListCommentsForIssue(ctx, db.ListCommentsForIssueParams{
		IssueID:     issueID,
		WorkspaceID: workspaceID,
		Limit:       issueDoneGateCommentScanLimit,
	})
	if err != nil {
		return false
	}

	for i := len(comments) - 1; i >= 0; i-- {
		c := comments[i]
		if !isTrustedDoneGateAuthor(c.AuthorType) {
			continue
		}
		verdict, ok := doneGateCommentVerdict(c.Content)
		if ok {
			return verdict
		}
	}
	return false
}

func isTrustedDoneGateAuthor(authorType string) bool {
	return authorType == "agent" || authorType == "member"
}

func doneGateCommentVerdict(content string) (bool, bool) {
	var verdict bool
	var found bool
	for _, line := range strings.Split(content, "\n") {
		if v, ok := doneGateLineVerdict(line); ok {
			verdict = v
			found = true
		}
	}
	return verdict, found
}

func doneGateLineVerdict(line string) (bool, bool) {
	trimmed := strings.TrimLeft(line, " \t`")
	normalized := strings.ToUpper(trimmed)
	if strings.HasPrefix(normalized, "[MSG] AUDIT_PASS") {
		return true, true
	}
	if strings.HasPrefix(normalized, "[MSG] AUDIT_FAIL") {
		return false, true
	}

	candidates := []struct {
		marker  string
		verdict bool
	}{
		{"VERDICT: PASS", true},
		{"VERDICT：PASS", true},
		{"VERDICT: FAIL", false},
		{"VERDICT：FAIL", false},
	}
	for _, candidate := range candidates {
		if verdictMarkerInConclusionPosition(normalized, candidate.marker) {
			return candidate.verdict, true
		}
	}
	return false, false
}

func verdictMarkerInConclusionPosition(line, marker string) bool {
	start := 0
	for {
		idx := strings.Index(line[start:], marker)
		if idx < 0 {
			return false
		}
		idx += start
		if idx == 0 || precededBySentenceEnd(line[:idx]) {
			return true
		}
		start = idx + len(marker)
	}
}

func precededBySentenceEnd(prefix string) bool {
	prefix = strings.TrimSpace(prefix)
	prefix = strings.TrimRight(prefix, "`")
	if prefix == "" {
		return true
	}
	last := []rune(prefix)[len([]rune(prefix))-1]
	return strings.ContainsRune("。．.!?！？", last)
}
