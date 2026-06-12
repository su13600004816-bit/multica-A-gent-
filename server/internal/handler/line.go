package handler

import (
	"encoding/json"
	"net/http"

	"github.com/go-chi/chi/v5"
	"github.com/jackc/pgx/v5/pgtype"
	"github.com/multica-ai/multica/server/internal/service"
	"github.com/multica-ai/multica/server/internal/util"
	db "github.com/multica-ai/multica/server/pkg/db/generated"
)

// CreateLineRequest is the payload for POST /api/lines.
type CreateLineRequest struct {
	Title     string            `json:"title"`
	Graph     service.LineGraph `json:"graph"`
	ProjectID *string           `json:"project_id,omitempty"`
}

// CreateLine validates a pipeline graph and stores it as a reusable line.
func (h *Handler) CreateLine(w http.ResponseWriter, r *http.Request) {
	var req CreateLineRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid request body")
		return
	}
	if req.Title == "" {
		writeError(w, http.StatusBadRequest, "title is required")
		return
	}
	if err := service.ValidateLineGraph(req.Graph); err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}

	wsUUID, ok := parseUUIDOrBadRequest(w, h.resolveWorkspaceID(r), "workspace id")
	if !ok {
		return
	}
	graphJSON, err := json.Marshal(req.Graph)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "failed to encode graph")
		return
	}

	var projectID pgtype.UUID
	if req.ProjectID != nil && *req.ProjectID != "" {
		pid, ok := parseUUIDOrBadRequest(w, *req.ProjectID, "project_id")
		if !ok {
			return
		}
		projectID = pid
	}

	userID, ok := requireUserID(w, r)
	if !ok {
		return
	}
	var creator pgtype.UUID
	if u, err := util.ParseUUID(userID); err == nil {
		creator = u
	}

	line, err := h.Queries.CreateLine(r.Context(), db.CreateLineParams{
		WorkspaceID:   wsUUID,
		ProjectID:     projectID,
		Title:         req.Title,
		Graph:         graphJSON,
		Status:        "active",
		CreatedByType: "agent",
		CreatedByID:   creator,
	})
	if err != nil {
		writeError(w, http.StatusInternalServerError, "failed to create line")
		return
	}
	writeJSON(w, http.StatusCreated, map[string]any{"line": line})
}

// ListLines returns all lines in the workspace.
func (h *Handler) ListLines(w http.ResponseWriter, r *http.Request) {
	wsUUID, ok := parseUUIDOrBadRequest(w, h.resolveWorkspaceID(r), "workspace id")
	if !ok {
		return
	}
	lines, err := h.Queries.ListLines(r.Context(), wsUUID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "failed to list lines")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"lines": lines, "total": len(lines)})
}

// GetLine returns one line by id, scoped to the workspace.
func (h *Handler) GetLine(w http.ResponseWriter, r *http.Request) {
	wsUUID, ok := parseUUIDOrBadRequest(w, h.resolveWorkspaceID(r), "workspace id")
	if !ok {
		return
	}
	id, ok := parseUUIDOrBadRequest(w, chi.URLParam(r, "lineId"), "line id")
	if !ok {
		return
	}
	line, err := h.Queries.GetLineInWorkspace(r.Context(), db.GetLineInWorkspaceParams{
		ID:          id,
		WorkspaceID: wsUUID,
	})
	if err != nil {
		writeError(w, http.StatusNotFound, "line not found")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"line": line})
}

// StartLineRun snapshots the line graph and creates a running line_run. The
// backend line runner goroutine picks it up and drives it to completion.
func (h *Handler) StartLineRun(w http.ResponseWriter, r *http.Request) {
	wsUUID, ok := parseUUIDOrBadRequest(w, h.resolveWorkspaceID(r), "workspace id")
	if !ok {
		return
	}
	id, ok := parseUUIDOrBadRequest(w, chi.URLParam(r, "lineId"), "line id")
	if !ok {
		return
	}
	line, err := h.Queries.GetLineInWorkspace(r.Context(), db.GetLineInWorkspaceParams{
		ID:          id,
		WorkspaceID: wsUUID,
	})
	if err != nil {
		writeError(w, http.StatusNotFound, "line not found")
		return
	}

	// Re-validate the stored graph before committing to a run.
	graph, err := parseLineGraphForRun(line.Graph)
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	if err := service.ValidateLineGraph(graph); err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}

	run, err := h.Queries.CreateLineRun(r.Context(), db.CreateLineRunParams{
		LineID:      line.ID,
		WorkspaceID: wsUUID,
		Graph:       line.Graph,
		NodeState:   []byte("{}"),
	})
	if err != nil {
		writeError(w, http.StatusInternalServerError, "failed to start line run")
		return
	}
	writeJSON(w, http.StatusCreated, map[string]any{"run": run})
}

// GetLineRun returns a run's live status (node_state shows per-stage progress).
func (h *Handler) GetLineRun(w http.ResponseWriter, r *http.Request) {
	wsUUID, ok := parseUUIDOrBadRequest(w, h.resolveWorkspaceID(r), "workspace id")
	if !ok {
		return
	}
	id, ok := parseUUIDOrBadRequest(w, chi.URLParam(r, "runId"), "run id")
	if !ok {
		return
	}
	run, err := h.Queries.GetLineRunInWorkspace(r.Context(), db.GetLineRunInWorkspaceParams{
		ID:          id,
		WorkspaceID: wsUUID,
	})
	if err != nil {
		writeError(w, http.StatusNotFound, "line run not found")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"run": run})
}

// ListLineRuns returns all runs for a line.
func (h *Handler) ListLineRuns(w http.ResponseWriter, r *http.Request) {
	id, ok := parseUUIDOrBadRequest(w, chi.URLParam(r, "lineId"), "line id")
	if !ok {
		return
	}
	runs, err := h.Queries.ListLineRunsForLine(r.Context(), id)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "failed to list line runs")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"runs": runs, "total": len(runs)})
}

// parseLineGraphForRun decodes a stored graph; kept in the handler package to
// avoid exporting the service-internal parser.
func parseLineGraphForRun(raw []byte) (service.LineGraph, error) {
	var g service.LineGraph
	if len(raw) == 0 {
		return g, nil
	}
	err := json.Unmarshal(raw, &g)
	return g, err
}
