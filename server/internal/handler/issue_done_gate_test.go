package handler

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestUpdateIssueDoneGateRejectsWithoutEvidence(t *testing.T) {
	issueID := createTestIssue(t, "done gate rejects missing evidence", "in_review", "medium")
	t.Cleanup(func() { deleteTestIssue(t, issueID) })

	w := httptest.NewRecorder()
	req := newRequest("PUT", "/api/issues/"+issueID, map[string]any{"status": "done"})
	req = withURLParam(req, "id", issueID)
	testHandler.UpdateIssue(w, req)
	if w.Code != http.StatusConflict {
		t.Fatalf("expected 409, got %d: %s", w.Code, w.Body.String())
	}

	got := getTestIssue(t, issueID)
	if got.Status != "in_review" {
		t.Fatalf("status changed despite missing gate evidence: got %q", got.Status)
	}
}

func TestUpdateIssueDoneGateAllowsMetadataPass(t *testing.T) {
	issueID := createTestIssue(t, "done gate allows metadata", "in_review", "medium")
	t.Cleanup(func() { deleteTestIssue(t, issueID) })
	setIssueMetadataForTest(t, issueID, `{"gate_status":"pass","pipeline_status":"done_real"}`)

	w := httptest.NewRecorder()
	req := newRequest("PUT", "/api/issues/"+issueID, map[string]any{"status": "done"})
	req = withURLParam(req, "id", issueID)
	testHandler.UpdateIssue(w, req)
	if w.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", w.Code, w.Body.String())
	}

	got := getTestIssue(t, issueID)
	if got.Status != "done" {
		t.Fatalf("expected done, got %q", got.Status)
	}
}

func TestUpdateIssueDoneGateAllowsLatestTrustedPassVerdict(t *testing.T) {
	issueID := createTestIssue(t, "done gate allows verdict", "in_review", "medium")
	t.Cleanup(func() { deleteTestIssue(t, issueID) })
	createCommentForDoneGateTest(t, issueID, "agent", "VERDICT: FAIL")
	createCommentForDoneGateTest(t, issueID, "agent", "[MSG] AUDIT_PASS\nVERDICT: PASS")

	w := httptest.NewRecorder()
	req := newRequest("PUT", "/api/issues/"+issueID, map[string]any{"status": "done"})
	req = withURLParam(req, "id", issueID)
	testHandler.UpdateIssue(w, req)
	if w.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", w.Code, w.Body.String())
	}
}

func TestUpdateIssueDoneGateRejectsWhenLatestVerdictFails(t *testing.T) {
	issueID := createTestIssue(t, "done gate rejects latest fail", "in_review", "medium")
	t.Cleanup(func() { deleteTestIssue(t, issueID) })
	createCommentForDoneGateTest(t, issueID, "agent", "VERDICT: PASS")
	createCommentForDoneGateTest(t, issueID, "agent", "VERDICT: FAIL")

	w := httptest.NewRecorder()
	req := newRequest("PUT", "/api/issues/"+issueID, map[string]any{"status": "done"})
	req = withURLParam(req, "id", issueID)
	testHandler.UpdateIssue(w, req)
	if w.Code != http.StatusConflict {
		t.Fatalf("expected 409, got %d: %s", w.Code, w.Body.String())
	}
}

func TestBatchUpdateIssuesDoneGateSkipsMissingEvidence(t *testing.T) {
	blockedID := createTestIssue(t, "batch done gate blocked", "in_review", "medium")
	allowedID := createTestIssue(t, "batch done gate allowed", "in_review", "medium")
	t.Cleanup(func() { deleteTestIssue(t, blockedID) })
	t.Cleanup(func() { deleteTestIssue(t, allowedID) })
	setIssueMetadataForTest(t, allowedID, `{"gate_status":"pass","pipeline_status":"done_real"}`)

	w := httptest.NewRecorder()
	req := newRequest("POST", "/api/issues/batch-update", map[string]any{
		"issue_ids": []string{blockedID, allowedID},
		"updates":   map[string]any{"status": "done"},
	})
	testHandler.BatchUpdateIssues(w, req)
	if w.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", w.Code, w.Body.String())
	}
	var resp struct {
		Updated int `json:"updated"`
	}
	if err := json.NewDecoder(w.Body).Decode(&resp); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	if resp.Updated != 1 {
		t.Fatalf("expected updated=1, got %d", resp.Updated)
	}
	if got := getTestIssue(t, blockedID); got.Status != "in_review" {
		t.Fatalf("blocked issue status = %q, want in_review", got.Status)
	}
	if got := getTestIssue(t, allowedID); got.Status != "done" {
		t.Fatalf("allowed issue status = %q, want done", got.Status)
	}
}

func getTestIssue(t *testing.T, issueID string) IssueResponse {
	t.Helper()
	w := httptest.NewRecorder()
	req := newRequest("GET", "/api/issues/"+issueID, nil)
	req = withURLParam(req, "id", issueID)
	testHandler.GetIssue(w, req)
	if w.Code != http.StatusOK {
		t.Fatalf("GetIssue: expected 200, got %d: %s", w.Code, w.Body.String())
	}
	var got IssueResponse
	if err := json.NewDecoder(w.Body).Decode(&got); err != nil {
		t.Fatalf("decode issue: %v", err)
	}
	return got
}

func setIssueMetadataForTest(t *testing.T, issueID string, metadata string) {
	t.Helper()
	if _, err := testPool.Exec(context.Background(), `UPDATE issue SET metadata = $2::jsonb WHERE id = $1`, issueID, metadata); err != nil {
		t.Fatalf("set issue metadata: %v", err)
	}
}

func createCommentForDoneGateTest(t *testing.T, issueID, authorType, content string) {
	t.Helper()
	if _, err := testPool.Exec(context.Background(), `
		INSERT INTO comment (issue_id, workspace_id, author_type, author_id, content, type)
		VALUES ($1, $2, $3, $4, $5, 'comment')
	`, issueID, testWorkspaceID, authorType, testUserID, content); err != nil {
		t.Fatalf("create gate comment: %v", err)
	}
}
