CREATE TABLE IF NOT EXISTS squad_canvas_layout (
  squad_id   uuid PRIMARY KEY REFERENCES squad(id) ON DELETE CASCADE,
  layout     jsonb NOT NULL DEFAULT '{}'::jsonb,
  updated_at timestamptz NOT NULL DEFAULT now()
);
