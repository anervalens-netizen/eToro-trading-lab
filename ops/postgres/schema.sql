CREATE TABLE IF NOT EXISTS operational_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO operational_meta(key, value)
VALUES ('schema_version', '1')
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now();

CREATE TABLE IF NOT EXISTS events (
    id BIGSERIAL PRIMARY KEY,
    ts TIMESTAMPTZ NOT NULL,
    event_type TEXT NOT NULL CHECK (event_type <> ''),
    payload JSONB NOT NULL,
    canonical_body TEXT NOT NULL,
    previous_hash CHAR(64) NOT NULL CHECK (previous_hash ~ '^[0-9a-f]{64}$'),
    event_hash CHAR(64) NOT NULL UNIQUE CHECK (event_hash ~ '^[0-9a-f]{64}$')
);

CREATE INDEX IF NOT EXISTS events_ts_idx ON events(ts DESC);
CREATE INDEX IF NOT EXISTS events_type_ts_idx ON events(event_type, ts DESC);

CREATE OR REPLACE FUNCTION reject_append_only_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
END;
$$;

DROP TRIGGER IF EXISTS events_append_only ON events;
CREATE TRIGGER events_append_only
BEFORE UPDATE OR DELETE ON events
FOR EACH ROW EXECUTE FUNCTION reject_append_only_mutation();

CREATE TABLE IF NOT EXISTS proposals (
    proposal_id TEXT PRIMARY KEY,
    request JSONB NOT NULL,
    request_hash CHAR(64) NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    envelope_hash CHAR(64) NOT NULL CHECK (envelope_hash ~ '^[0-9a-f]{64}$'),
    sealed_order JSONB,
    state TEXT NOT NULL CHECK (state IN (
        'PROPOSED', 'RISK_REJECTED', 'SEALED', 'AWAITING_APPROVAL', 'APPROVED',
        'SENDING', 'ACKNOWLEDGED', 'UNKNOWN', 'REJECTED', 'PARTIAL', 'FILLED',
        'CANCELLED', 'RECONCILED'
    )),
    expires_at TIMESTAMPTZ,
    x_request_id TEXT,
    response JSONB,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS proposals_x_request_id_uidx
ON proposals(x_request_id) WHERE x_request_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS proposals_state_updated_idx ON proposals(state, updated_at DESC);

CREATE TABLE IF NOT EXISTS approvals (
    proposal_id TEXT PRIMARY KEY REFERENCES proposals(proposal_id) ON DELETE RESTRICT,
    envelope_hash CHAR(64) NOT NULL CHECK (envelope_hash ~ '^[0-9a-f]{64}$'),
    actor TEXT NOT NULL CHECK (actor <> ''),
    approved_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    CHECK (consumed_at IS NULL OR consumed_at >= approved_at)
);

CREATE TABLE IF NOT EXISTS execution_transitions (
    id BIGSERIAL PRIMARY KEY,
    proposal_id TEXT NOT NULL REFERENCES proposals(proposal_id) ON DELETE RESTRICT,
    from_state TEXT,
    to_state TEXT NOT NULL CHECK (to_state IN (
        'PROPOSED', 'RISK_REJECTED', 'SEALED', 'AWAITING_APPROVAL', 'APPROVED',
        'SENDING', 'ACKNOWLEDGED', 'UNKNOWN', 'REJECTED', 'PARTIAL', 'FILLED',
        'CANCELLED', 'RECONCILED'
    )),
    reason TEXT NOT NULL DEFAULT '',
    response JSONB,
    recorded_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS execution_transitions_proposal_idx
ON execution_transitions(proposal_id, id);

DROP TRIGGER IF EXISTS execution_transitions_append_only ON execution_transitions;
CREATE TRIGGER execution_transitions_append_only
BEFORE UPDATE OR DELETE ON execution_transitions
FOR EACH ROW EXECUTE FUNCTION reject_append_only_mutation();

CREATE TABLE IF NOT EXISTS kill_switch (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    state TEXT NOT NULL CHECK (state IN ('ACTIVE', 'HALT_NEW', 'REDUCE_ONLY', 'LOCKED')),
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    version BIGINT NOT NULL CHECK (version >= 1),
    changed_at TIMESTAMPTZ NOT NULL
);

INSERT INTO kill_switch(singleton, state, actor, reason, version, changed_at)
VALUES (TRUE, 'LOCKED', 'bootstrap', 'fail-closed initialization', 1, now())
ON CONFLICT (singleton) DO NOTHING;

CREATE TABLE IF NOT EXISTS pnl_daily (
    portfolio_id TEXT NOT NULL,
    day DATE NOT NULL,
    realized_usd NUMERIC(38, 18) NOT NULL,
    unrealized_usd NUMERIC(38, 18) NOT NULL,
    fees_usd NUMERIC(38, 18) NOT NULL,
    financing_usd NUMERIC(38, 18) NOT NULL,
    daily_pnl_usd NUMERIC(38, 18) NOT NULL,
    equity_usd NUMERIC(38, 18) NOT NULL CHECK (equity_usd >= 0),
    recorded_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (portfolio_id, day)
);

CREATE TABLE IF NOT EXISTS service_heartbeats (
    service TEXT PRIMARY KEY CHECK (service <> ''),
    status TEXT NOT NULL CHECK (status <> ''),
    details JSONB NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL
);
