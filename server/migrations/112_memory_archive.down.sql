ALTER TABLE agent DROP COLUMN IF EXISTS session_resume_enabled;
ALTER TABLE workspace DROP COLUMN IF EXISTS session_resume_enabled;
ALTER TABLE issue DROP COLUMN IF EXISTS memory_compacted_at;
ALTER TABLE chat_session DROP COLUMN IF EXISTS compacted_at;
DROP TABLE IF EXISTS memory_archive;
