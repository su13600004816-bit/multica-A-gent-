package handler

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/go-chi/chi/v5"
)

func TestP0NotificationAckClearsPending(t *testing.T) {
	clearP0NotificationsForTestWorkspace(t)

	createReq := withURLParam(newRequest(http.MethodPost, "/api/workspaces/"+testWorkspaceID+"/p0/notifications", P0NotifyRequest{
		Body:   "P0: production smoke failed",
		Source: "test",
	}), "id", testWorkspaceID)
	createW := httptest.NewRecorder()
	testHandler.CreateP0Notification(createW, createReq)
	if createW.Code != http.StatusCreated {
		t.Fatalf("create: expected 201, got %d: %s", createW.Code, createW.Body.String())
	}
	var created P0NotificationResponse
	if err := json.NewDecoder(createW.Body).Decode(&created); err != nil {
		t.Fatalf("decode created: %v", err)
	}
	if created.ID == "" {
		t.Fatal("expected created id")
	}

	var viewCount int
	if err := testPool.QueryRow(context.Background(), `
		SELECT count(*) FROM pending_p0_acks WHERE workspace_id = $1 AND id = $2
	`, testWorkspaceID, created.ID).Scan(&viewCount); err != nil {
		t.Fatalf("query pending view: %v", err)
	}
	if viewCount != 1 {
		t.Fatalf("expected created notification in pending_p0_acks, got %d", viewCount)
	}

	pendingReq := withURLParam(newRequest(http.MethodGet, "/api/workspaces/"+testWorkspaceID+"/p0/pending", nil), "id", testWorkspaceID)
	pendingW := httptest.NewRecorder()
	testHandler.ListPendingP0Notifications(pendingW, pendingReq)
	if pendingW.Code != http.StatusOK {
		t.Fatalf("pending: expected 200, got %d: %s", pendingW.Code, pendingW.Body.String())
	}
	var pending P0PendingResponse
	if err := json.NewDecoder(pendingW.Body).Decode(&pending); err != nil {
		t.Fatalf("decode pending: %v", err)
	}
	if len(pending.Items) != 1 || pending.Items[0].ID != created.ID {
		t.Fatalf("expected only created notification pending, got %+v", pending.Items)
	}

	ackReq := withURLParam(newRequest(http.MethodPost, "/api/workspaces/"+testWorkspaceID+"/p0/notifications/"+created.ID+"/ack", P0AckRequest{
		Note: "handled",
	}), "id", testWorkspaceID)
	ackReq = withP0RouteParams(ackReq, testWorkspaceID, created.ID)
	ackW := httptest.NewRecorder()
	testHandler.AckP0Notification(ackW, ackReq)
	if ackW.Code != http.StatusOK {
		t.Fatalf("ack: expected 200, got %d: %s", ackW.Code, ackW.Body.String())
	}
	var acked P0NotificationResponse
	if err := json.NewDecoder(ackW.Body).Decode(&acked); err != nil {
		t.Fatalf("decode acked: %v", err)
	}
	if acked.AckedAt == nil {
		t.Fatal("expected acked_at after ACK")
	}

	if err := testPool.QueryRow(context.Background(), `
		SELECT count(*) FROM pending_p0_acks WHERE workspace_id = $1 AND id = $2
	`, testWorkspaceID, created.ID).Scan(&viewCount); err != nil {
		t.Fatalf("query pending view after ack: %v", err)
	}
	if viewCount != 0 {
		t.Fatalf("expected ACKed notification removed from pending_p0_acks, got %d", viewCount)
	}
}

func withP0RouteParams(req *http.Request, workspaceID, p0ID string) *http.Request {
	rctx := chi.NewRouteContext()
	rctx.URLParams.Add("id", workspaceID)
	rctx.URLParams.Add("p0Id", p0ID)
	return req.WithContext(context.WithValue(req.Context(), chi.RouteCtxKey, rctx))
}

func clearP0NotificationsForTestWorkspace(t *testing.T) {
	t.Helper()
	if _, err := testPool.Exec(context.Background(), `DELETE FROM p0_notification WHERE workspace_id = $1`, testWorkspaceID); err != nil {
		t.Fatalf("clear p0 notifications: %v", err)
	}
	t.Cleanup(func() {
		testPool.Exec(context.Background(), `DELETE FROM p0_notification WHERE workspace_id = $1`, testWorkspaceID)
	})
}
