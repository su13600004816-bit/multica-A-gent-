package handler

import (
	"encoding/json"
	"net/http"
)

// H: 画布布局 JSON 上限,防无界写库/DoS。
const maxCanvasLayoutBytes = 1 << 20 // 1 MB

// GetSquadCanvasLayout 返回某小队画布编排的已存布局(无则 {"layout":null})。
// C: 经 loadSquadInWorkspace 校验该 squad 属于当前 workspace(防跨租户 IDOR)。
func (h *Handler) GetSquadCanvasLayout(w http.ResponseWriter, r *http.Request) {
	squad, _, ok := h.loadSquadInWorkspace(w, r)
	if !ok {
		return
	}
	var layout []byte
	err := h.DB.QueryRow(r.Context(),
		`SELECT layout FROM squad_canvas_layout WHERE squad_id = $1`, squad.ID).Scan(&layout)
	w.Header().Set("Content-Type", "application/json")
	if err != nil || len(layout) == 0 {
		_, _ = w.Write([]byte(`{"layout":null}`))
		return
	}
	out := append([]byte(`{"layout":`), layout...)
	out = append(out, '}')
	_, _ = w.Write(out)
}

// SetSquadCanvasLayout upsert 某小队的画布编排布局(手工编排持久化,跨设备)。
func (h *Handler) SetSquadCanvasLayout(w http.ResponseWriter, r *http.Request) {
	squad, _, ok := h.loadSquadInWorkspace(w, r) // C: workspace 隔离
	if !ok {
		return
	}
	r.Body = http.MaxBytesReader(w, r.Body, maxCanvasLayoutBytes) // H: 体积上限
	var body struct {
		Layout json.RawMessage `json:"layout"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		writeError(w, http.StatusBadRequest, "invalid json")
		return
	}
	if len(body.Layout) == 0 {
		body.Layout = json.RawMessage(`{}`)
	}
	if _, err := h.DB.Exec(r.Context(),
		`INSERT INTO squad_canvas_layout (squad_id, layout, updated_at) VALUES ($1, $2, now())
		 ON CONFLICT (squad_id) DO UPDATE SET layout = EXCLUDED.layout, updated_at = now()`,
		squad.ID, []byte(body.Layout)); err != nil {
		writeError(w, http.StatusInternalServerError, "failed to save canvas layout")
		return
	}
	writeJSON(w, http.StatusOK, map[string]bool{"ok": true})
}
