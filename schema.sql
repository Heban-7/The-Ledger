-- PostgreSQL Schema for The Ledger (Apex Financial Services)
-- Event Store & Enterprise Audit Infrastructure

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- 1. Core append-only events table
CREATE TABLE IF NOT EXISTS events (
    event_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    stream_id TEXT NOT NULL,
    stream_position BIGINT NOT NULL,
    global_position BIGINT GENERATED ALWAYS AS IDENTITY,
    event_type TEXT NOT NULL,
    event_version SMALLINT NOT NULL DEFAULT 1,
    payload JSONB NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT uq_stream_position UNIQUE (stream_id, stream_position)
);

CREATE INDEX IF NOT EXISTS idx_events_stream_id ON events (stream_id, stream_position);
CREATE INDEX IF NOT EXISTS idx_events_global_pos ON events (global_position);
CREATE INDEX IF NOT EXISTS idx_events_type ON events (event_type);
CREATE INDEX IF NOT EXISTS idx_events_recorded ON events (recorded_at);

-- 2. Stream metadata and optimistic concurrency tracking
CREATE TABLE IF NOT EXISTS event_streams (
    stream_id TEXT PRIMARY KEY,
    aggregate_type TEXT NOT NULL,
    current_version BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    archived_at TIMESTAMPTZ,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_event_streams_agg_type ON event_streams (aggregate_type);

-- 3. Projection checkpoints for async daemon
CREATE TABLE IF NOT EXISTS projection_checkpoints (
    projection_name TEXT PRIMARY KEY,
    last_position BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 4. Outbox table for guaranteed projection/message delivery
CREATE TABLE IF NOT EXISTS outbox (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id UUID NOT NULL REFERENCES events(event_id),
    destination TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    published_at TIMESTAMPTZ,
    attempts SMALLINT NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_outbox_unpublished ON outbox (created_at) WHERE published_at IS NULL;

-- 5. Materialized Read Models (Projections)
CREATE TABLE IF NOT EXISTS application_summary (
    application_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    lifecycle_phase TEXT NOT NULL,
    previous_state TEXT,
    state_entered_at TIMESTAMPTZ,
    loan_stream_position BIGINT,
    terminal_outcome TEXT,
    applicant_id TEXT,
    requested_amount_usd NUMERIC,
    approved_amount_usd NUMERIC,
    risk_tier TEXT,
    fraud_score DOUBLE PRECISION,
    compliance_status TEXT,
    decision TEXT,
    agent_sessions_completed TEXT[] DEFAULT '{}',
    last_event_type TEXT,
    last_event_at TIMESTAMPTZ,
    last_stream_id TEXT,
    last_stream_position BIGINT,
    global_position BIGINT,
    human_reviewer_id TEXT,
    final_decision_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS agent_performance_ledger (
    agent_id TEXT NOT NULL,
    model_version TEXT NOT NULL,
    analyses_completed INT NOT NULL DEFAULT 0,
    decisions_generated INT NOT NULL DEFAULT 0,
    avg_confidence_score DOUBLE PRECISION,
    avg_duration_ms DOUBLE PRECISION,
    approve_rate DOUBLE PRECISION,
    decline_rate DOUBLE PRECISION,
    refer_rate DOUBLE PRECISION,
    human_override_rate DOUBLE PRECISION,
    first_seen_at TIMESTAMPTZ,
    last_seen_at TIMESTAMPTZ,
    PRIMARY KEY (agent_id, model_version)
);

CREATE TABLE IF NOT EXISTS compliance_audit_view (
    application_id TEXT PRIMARY KEY,
    regulation_set_version TEXT,
    rules_required TEXT[] DEFAULT '{}',
    rules_passed TEXT[] DEFAULT '{}',
    rules_failed TEXT[] DEFAULT '{}',
    has_hard_block BOOLEAN DEFAULT FALSE,
    compliance_status TEXT NOT NULL DEFAULT 'PENDING',
    events JSONB DEFAULT '[]'::jsonb,
    last_updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- 6. Distributed Projection Leases (for multi-node ProjectionDaemon coordination)
CREATE TABLE IF NOT EXISTS projection_leases (
    projection_name TEXT PRIMARY KEY,
    lease_holder TEXT NOT NULL,
    leased_until TIMESTAMPTZ NOT NULL,
    last_position BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
