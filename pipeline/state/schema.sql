CREATE TABLE IF NOT EXISTS pipeline_items (
    message_id BIGINT NOT NULL,
    stage VARCHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    error TEXT,
    PRIMARY KEY (message_id, stage)
);

CREATE INDEX IF NOT EXISTS idx_pipeline_items_status
    ON pipeline_items (status);

CREATE INDEX IF NOT EXISTS idx_pipeline_items_updated_at
    ON pipeline_items (updated_at);

-- Immutable source metadata is kept separately from per-stage status.  A message
-- can have many stage rows but exactly one captured Telegram payload description.
CREATE TABLE IF NOT EXISTS pipeline_messages (
    message_id BIGINT PRIMARY KEY,
    channel_id TEXT NOT NULL,
    message_timestamp TIMESTAMP WITH TIME ZONE NOT NULL,
    duration_seconds REAL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);
