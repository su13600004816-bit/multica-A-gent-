package handler

import (
	"encoding/json"
	"log/slog"
	"net/http"
	"strings"

	"github.com/go-chi/chi/v5"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"
	"github.com/multica-ai/multica/server/internal/logger"
)

const (
	p0NotificationBodyLimit = 64 * 1024
	p0NotificationMaxBody   = 10000
	p0AckNoteMaxLen         = 2000
)

type P0NotifyRequest struct {
	Body   string `json:"body"`
	Source string `json:"source,omitempty"`
}

type P0AckRequest struct {
	Note string `json:"note,omitempty"`
}

type P0NotificationResponse struct {
	ID            string  `json:"id"`
	WorkspaceID   string  `json:"workspace_id"`
	Body          string  `json:"body"`
	Source        string  `json:"source"`
	CreatedByType string  `json:"created_by_type"`
	CreatedByID   string  `json:"created_by_id,omitempty"`
	AckedByType   *string `json:"acked_by_type,omitempty"`
	AckedByID     *string `json:"acked_by_id,omitempty"`
	AckNote       *string `json:"ack_note,omitempty"`
	AckedAt       *string `json:"acked_at,omitempty"`
	CreatedAt     string  `json:"created_at"`
	UpdatedAt     string  `json:"updated_at"`
}

type P0PendingResponse struct {
	Items []P0NotificationResponse `json:"items"`
}

func (h *Handler) CreateP0Notification(w http.ResponseWriter, r *http.Request) {
	workspaceID := chi.URLParam(r, "id")
	wsUUID, ok := parseUUIDOrBadRequest(w, workspaceID, "workspace id")
	if !ok {
		return
	}
	userID, ok := requireUserID(w, r)
	if !ok {
		return
	}

	r.Body = http.MaxBytesReader(w, r.Body, p0NotificationBodyLimit)
	var req P0NotifyRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid request body")
		return
	}
	body := strings.TrimSpace(req.Body)
	if body == "" {
		writeError(w, http.StatusBadRequest, "body is required")
		return
	}
	if len(body) > p0NotificationMaxBody {
		writeError(w, http.StatusBadRequest, "body too long")
		return
	}
	source := strings.TrimSpace(req.Source)
	if source == "" {
		source = "manual"
	}
	if len(source) > 100 {
		writeError(w, http.StatusBadRequest, "source too long")
		return
	}

	row := p0NotificationRow{}
	err := h.DB.QueryRow(r.Context(), `
		INSERT INTO p0_notification (workspace_id, body, source, created_by_type, created_by_id)
		VALUES ($1, $2, $3, 'member', $4)
		RETURNING id, workspace_id, body, source, created_by_type, created_by_id,
			acked_by_type, acked_by_id, ack_note, acked_at, created_at, updated_at
	`, wsUUID, body, source, parseUUID(userID)).Scan(
		&row.ID, &row.WorkspaceID, &row.Body, &row.Source, &row.CreatedByType, &row.CreatedByID,
		&row.AckedByType, &row.AckedByID, &row.AckNote, &row.AckedAt, &row.CreatedAt, &row.UpdatedAt,
	)
	if err != nil {
		slog.Warn("create p0 notification failed", append(logger.RequestAttrs(r), "error", err)...)
		writeError(w, http.StatusInternalServerError, "failed to create p0 notification")
		return
	}

	writeJSON(w, http.StatusCreated, row.toResponse())
}

func (h *Handler) ListPendingP0Notifications(w http.ResponseWriter, r *http.Request) {
	workspaceID := chi.URLParam(r, "id")
	wsUUID, ok := parseUUIDOrBadRequest(w, workspaceID, "workspace id")
	if !ok {
		return
	}

	rows, err := h.DB.Query(r.Context(), `
		SELECT id, workspace_id, body, source, created_by_type, created_by_id,
			acked_by_type, acked_by_id, ack_note, acked_at, created_at, updated_at
		FROM p0_notification
		WHERE workspace_id = $1 AND acked_at IS NULL
		ORDER BY created_at ASC
	`, wsUUID)
	if err != nil {
		slog.Warn("list pending p0 notifications failed", append(logger.RequestAttrs(r), "error", err)...)
		writeError(w, http.StatusInternalServerError, "failed to list pending p0 notifications")
		return
	}
	defer rows.Close()

	items := []P0NotificationResponse{}
	for rows.Next() {
		row := p0NotificationRow{}
		if err := row.scan(rows); err != nil {
			slog.Warn("scan pending p0 notification failed", append(logger.RequestAttrs(r), "error", err)...)
			writeError(w, http.StatusInternalServerError, "failed to list pending p0 notifications")
			return
		}
		items = append(items, row.toResponse())
	}
	if err := rows.Err(); err != nil {
		slog.Warn("iterate pending p0 notifications failed", append(logger.RequestAttrs(r), "error", err)...)
		writeError(w, http.StatusInternalServerError, "failed to list pending p0 notifications")
		return
	}

	writeJSON(w, http.StatusOK, P0PendingResponse{Items: items})
}

func (h *Handler) AckP0Notification(w http.ResponseWriter, r *http.Request) {
	workspaceID := chi.URLParam(r, "id")
	wsUUID, ok := parseUUIDOrBadRequest(w, workspaceID, "workspace id")
	if !ok {
		return
	}
	p0ID, ok := parseUUIDOrBadRequest(w, chi.URLParam(r, "p0Id"), "p0 id")
	if !ok {
		return
	}
	userID, ok := requireUserID(w, r)
	if !ok {
		return
	}

	r.Body = http.MaxBytesReader(w, r.Body, p0NotificationBodyLimit)
	var req P0AckRequest
	if r.Body != nil {
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			writeError(w, http.StatusBadRequest, "invalid request body")
			return
		}
	}
	note := strings.TrimSpace(req.Note)
	if len(note) > p0AckNoteMaxLen {
		writeError(w, http.StatusBadRequest, "note too long")
		return
	}

	row := p0NotificationRow{}
	err := h.DB.QueryRow(r.Context(), `
		UPDATE p0_notification
		SET acked_by_type = 'member',
			acked_by_id = $3,
			ack_note = NULLIF($4, ''),
			acked_at = COALESCE(acked_at, now()),
			updated_at = now()
		WHERE id = $1 AND workspace_id = $2
		RETURNING id, workspace_id, body, source, created_by_type, created_by_id,
			acked_by_type, acked_by_id, ack_note, acked_at, created_at, updated_at
	`, p0ID, wsUUID, parseUUID(userID), note).Scan(
		&row.ID, &row.WorkspaceID, &row.Body, &row.Source, &row.CreatedByType, &row.CreatedByID,
		&row.AckedByType, &row.AckedByID, &row.AckNote, &row.AckedAt, &row.CreatedAt, &row.UpdatedAt,
	)
	if err != nil {
		if err == pgx.ErrNoRows {
			writeError(w, http.StatusNotFound, "p0 notification not found")
			return
		}
		slog.Warn("ack p0 notification failed", append(logger.RequestAttrs(r), "error", err)...)
		writeError(w, http.StatusInternalServerError, "failed to ack p0 notification")
		return
	}

	writeJSON(w, http.StatusOK, row.toResponse())
}

type p0NotificationRow struct {
	ID            pgtype.UUID
	WorkspaceID   pgtype.UUID
	Body          string
	Source        string
	CreatedByType string
	CreatedByID   pgtype.UUID
	AckedByType   pgtype.Text
	AckedByID     pgtype.UUID
	AckNote       pgtype.Text
	AckedAt       pgtype.Timestamptz
	CreatedAt     pgtype.Timestamptz
	UpdatedAt     pgtype.Timestamptz
}

type p0RowScanner interface {
	Scan(dest ...any) error
}

func (r *p0NotificationRow) scan(row p0RowScanner) error {
	return row.Scan(
		&r.ID, &r.WorkspaceID, &r.Body, &r.Source, &r.CreatedByType, &r.CreatedByID,
		&r.AckedByType, &r.AckedByID, &r.AckNote, &r.AckedAt, &r.CreatedAt, &r.UpdatedAt,
	)
}

func (r p0NotificationRow) toResponse() P0NotificationResponse {
	resp := P0NotificationResponse{
		ID:            uuidToString(r.ID),
		WorkspaceID:   uuidToString(r.WorkspaceID),
		Body:          r.Body,
		Source:        r.Source,
		CreatedByType: r.CreatedByType,
		CreatedByID:   uuidToString(r.CreatedByID),
		CreatedAt:     timestampToString(r.CreatedAt),
		UpdatedAt:     timestampToString(r.UpdatedAt),
	}
	if r.AckedByType.Valid {
		v := r.AckedByType.String
		resp.AckedByType = &v
	}
	if r.AckedByID.Valid {
		v := uuidToString(r.AckedByID)
		resp.AckedByID = &v
	}
	if r.AckNote.Valid {
		v := r.AckNote.String
		resp.AckNote = &v
	}
	if r.AckedAt.Valid {
		v := timestampToString(r.AckedAt)
		resp.AckedAt = &v
	}
	return resp
}
