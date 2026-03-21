-- ApplicationSummary projection table (logical read model; matches ApplicationSummaryProjection row keys)
-- Use for PostgreSQL materialization of the in-memory projection.

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
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

COMMENT ON COLUMN application_summary.lifecycle_phase IS 'INTAKE | CREDIT | FRAUD | COMPLIANCE | DECISION | HUMAN_REVIEW | TERMINAL';
COMMENT ON COLUMN application_summary.terminal_outcome IS 'APPROVED | DECLINED | DECLINED_COMPLIANCE | REFERRED when lifecycle_phase = TERMINAL';
