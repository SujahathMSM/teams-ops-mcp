-- Team Ops MCP Database Schema (PostgreSQL 16)

CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    username VARCHAR(64) NOT NULL UNIQUE,
    role VARCHAR(16) NOT NULL CHECK (role IN ('viewer', 'editor', 'admin')),
    token_hash CHAR(64) NOT NULL UNIQUE,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at TIMESTAMPTZ
);

-- Idempotent upgrade for databases created before last_used_at existed
-- (see sql/migrations/001_users_last_used_at.sql for existing Docker volumes).
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_used_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_users_token_hash ON users (token_hash);

CREATE TABLE IF NOT EXISTS incidents (
    id SERIAL PRIMARY KEY,
    incident_key VARCHAR(32) NOT NULL UNIQUE,
    title TEXT NOT NULL,
    service VARCHAR(64) NOT NULL,
    severity VARCHAR(16) NOT NULL CHECK (severity IN ('SEV1', 'SEV2', 'SEV3', 'SEV4')),
    status VARCHAR(24) NOT NULL CHECK (status IN ('resolved', 'mitigated', 'open')),
    summary TEXT NOT NULL,
    root_cause TEXT NOT NULL,
    runbook_steps TEXT NOT NULL,
    tags TEXT[] NOT NULL DEFAULT '{}',
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    search_vector TSVECTOR GENERATED ALWAYS AS (
        setweight(to_tsvector('english', coalesce(incident_key, '') || ' ' || coalesce(title, '')), 'A') ||
        setweight(to_tsvector('english', coalesce(service, '') || ' ' || coalesce(summary, '')), 'B') ||
        setweight(to_tsvector('english', coalesce(root_cause, '') || ' ' || coalesce(runbook_steps, '')), 'C')
    ) STORED
);

CREATE INDEX IF NOT EXISTS idx_incidents_search_vector ON incidents USING GIN (search_vector);
CREATE INDEX IF NOT EXISTS idx_incidents_service ON incidents (service);
CREATE INDEX IF NOT EXISTS idx_incidents_severity ON incidents (severity);

CREATE TABLE IF NOT EXISTS tool_calls (
    id BIGSERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    tool_name VARCHAR(64) NOT NULL,
    arguments JSONB NOT NULL DEFAULT '{}'::jsonb,
    status VARCHAR(16) NOT NULL CHECK (status IN ('ok', 'error', 'denied')),
    latency_ms INTEGER NOT NULL CHECK (latency_ms >= 0),
    response_chars INTEGER NOT NULL DEFAULT 0 CHECK (response_chars >= 0),
    est_tokens INTEGER NOT NULL DEFAULT 0 CHECK (est_tokens >= 0),
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tool_calls_created_at ON tool_calls (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tool_calls_user_id ON tool_calls (user_id);
CREATE INDEX IF NOT EXISTS idx_tool_calls_tool_name ON tool_calls (tool_name);