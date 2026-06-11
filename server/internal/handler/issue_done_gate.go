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
	normalized := strings.ToUpper(content)
	if strings.Contains(normalized, "[MSG] AUDIT_PASS") {
		return true, true
	}
	if strings.Contains(normalized, "[MSG] AUDIT_FAIL") {
		return false, true
	}
	if strings.Contains(normalized, "VERDICT: PASS") || strings.Contains(normalized, "VERDICT：PASS") {
		return true, true
	}
	if strings.Contains(normalized, "VERDICT: FAIL") || strings.Contains(normalized, "VERDICT：FAIL") {
		return false, true
	}
	return false, false
}
