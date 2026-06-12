DROP TABLE IF EXISTS line_run;
DROP TABLE IF EXISTS line;
ALTER TABLE issue DROP CONSTRAINT IF EXISTS issue_origin_type_check;
ALTER TABLE issue ADD CONSTRAINT issue_origin_type_check
  CHECK (origin_type = ANY (ARRAY['autopilot'::text, 'quick_create'::text, 'lark_chat'::text]));
