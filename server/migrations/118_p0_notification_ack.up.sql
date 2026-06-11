CREATE TABLE p0_notification (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL REFERENCES workspace(id) ON DELETE CASCADE,
    body TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual',
    created_by_type TEXT NOT NULL CHECK (created_by_type IN ('member', 'agent', 'system')),
    created_by_id UUID,
    acked_by_type TEXT CHECK (acked_by_type IN ('member', 'agent', 'system')),
    acked_by_id UUID,
    ack_note TEXT,
    acked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (
        (acked_at IS NULL AND acked_by_type IS NULL AND acked_by_id IS NULL)
        OR
        (acked_at IS NOT NULL AND acked_by_type IS NOT NULL AND acked_by_id IS NOT NULL)
    )
);

CREATE INDEX idx_p0_notification_workspace_pending_created
    ON p0_notification (workspace_id, created_at DESC)
    WHERE acked_at IS NULL;

CREATE VIEW pending_p0_acks AS
SELECT
    id,
    workspace_id,
    body,
    source,
    created_by_type,
    created_by_id,
    created_at
FROM p0_notification
WHERE acked_at IS NULL;
