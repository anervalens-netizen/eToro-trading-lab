CREATE TABLE IF NOT EXISTS v2_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO v2_meta(key,value) VALUES ('schema_version','2')
ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_at=now();

CREATE OR REPLACE FUNCTION v2_reject_append_only_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
END;
$$;

CREATE TABLE IF NOT EXISTS v2_events (
    sequence BIGSERIAL PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL CHECK(event_type<>''),
    schema_version INTEGER NOT NULL CHECK(schema_version>=1),
    event_time TIMESTAMPTZ NOT NULL,
    processing_time TIMESTAMPTZ NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    causation_id TEXT NOT NULL DEFAULT '',
    correlation_id TEXT NOT NULL,
    payload JSONB NOT NULL,
    canonical_body TEXT NOT NULL,
    previous_hash CHAR(64) NOT NULL CHECK(previous_hash ~ '^[0-9a-f]{64}$'),
    event_hash CHAR(64) NOT NULL UNIQUE CHECK(event_hash ~ '^[0-9a-f]{64}$')
);
CREATE INDEX IF NOT EXISTS v2_events_type_time_idx ON v2_events(event_type,event_time DESC);
DROP TRIGGER IF EXISTS v2_events_append_only ON v2_events;
CREATE TRIGGER v2_events_append_only BEFORE UPDATE OR DELETE ON v2_events
FOR EACH ROW EXECUTE FUNCTION v2_reject_append_only_mutation();

CREATE TABLE IF NOT EXISTS v2_trading_state (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK(singleton),
    state TEXT NOT NULL CHECK(state IN ('ACTIVE','HALT_NEW','REDUCE_ONLY','LOCKED')),
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    version BIGINT NOT NULL CHECK(version>=1),
    changed_at TIMESTAMPTZ NOT NULL
);
INSERT INTO v2_trading_state(singleton,state,actor,reason,version,changed_at)
VALUES(TRUE,'LOCKED','bootstrap','fail-closed initialization',1,now())
ON CONFLICT(singleton) DO NOTHING;

CREATE TABLE IF NOT EXISTS v2_intents (
    intent_id TEXT PRIMARY KEY,
    portfolio_id TEXT NOT NULL,
    lane_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('ACTIVE','EXPIRED','REJECTED','CONSUMED')),
    envelope JSONB NOT NULL,
    envelope_hash CHAR(64) NOT NULL CHECK(envelope_hash ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS v2_intents_state_expiry_idx ON v2_intents(state,expires_at);

CREATE TABLE IF NOT EXISTS v2_decisions (
    decision_id TEXT PRIMARY KEY,
    packet_hash CHAR(64) NOT NULL CHECK(packet_hash ~ '^[0-9a-f]{64}$'),
    decision JSONB NOT NULL,
    state TEXT NOT NULL CHECK(state IN (
      'DECIDED','CLAIMED','APPLIED','FAILED_RETRYABLE','FAILED_TERMINAL','EXPIRED'
    )),
    created_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    claimed_by TEXT,
    claim_token TEXT,
    lease_expires_at TIMESTAMPTZ,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count>=0),
    applied_effect JSONB,
    updated_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS v2_decisions_claim_idx
ON v2_decisions(state,expires_at,lease_expires_at,created_at);

CREATE TABLE IF NOT EXISTS v2_order_commands (
    order_command_id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL REFERENCES v2_intents(intent_id) ON DELETE RESTRICT,
    proposal_id TEXT NOT NULL UNIQUE,
    client_order_id UUID NOT NULL UNIQUE,
    portfolio_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    reduce_only BOOLEAN NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    command JSONB NOT NULL,
    command_hash CHAR(64) NOT NULL CHECK(command_hash ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS v2_broker_orders (
    order_command_id TEXT PRIMARY KEY REFERENCES v2_order_commands(order_command_id) ON DELETE RESTRICT,
    status TEXT NOT NULL CHECK(status IN (
      'CREATED','RISK_APPROVED','SUBMITTING','ACKNOWLEDGED','PARTIALLY_FILLED','FILLED',
      'REJECTED','CANCELLED','EXPIRED','UNKNOWN','RECONCILED_FILLED',
      'RECONCILED_ABSENT','MANUAL_REVIEW'
    )),
    broker_order_id TEXT,
    broker_position_id TEXT,
    filled_quantity NUMERIC(38,18) NOT NULL DEFAULT 0 CHECK(filled_quantity>=0),
    average_fill_price NUMERIC(38,18),
    state JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS v2_broker_orders_status_idx ON v2_broker_orders(status,updated_at);

CREATE TABLE IF NOT EXISTS v2_fills (
    fill_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    order_command_id TEXT NOT NULL REFERENCES v2_order_commands(order_command_id) ON DELETE RESTRICT,
    broker_order_id TEXT,
    broker_position_id TEXT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK(side IN ('buy','sell')),
    quantity NUMERIC(38,18) NOT NULL CHECK(quantity>0),
    price NUMERIC(38,18) NOT NULL CHECK(price>0),
    fee_usd NUMERIC(38,18) NOT NULL CHECK(fee_usd>=0),
    financing_usd NUMERIC(38,18) NOT NULL CHECK(financing_usd>=0),
    event_time TIMESTAMPTZ NOT NULL,
    processing_time TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL
);
DROP TRIGGER IF EXISTS v2_fills_append_only ON v2_fills;
CREATE TRIGGER v2_fills_append_only BEFORE UPDATE OR DELETE ON v2_fills
FOR EACH ROW EXECUTE FUNCTION v2_reject_append_only_mutation();

CREATE TABLE IF NOT EXISTS v2_positions (
    position_id TEXT PRIMARY KEY,
    portfolio_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    lane_id TEXT NOT NULL,
    intent_id TEXT NOT NULL REFERENCES v2_intents(intent_id) ON DELETE RESTRICT,
    symbol TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('OPEN','CLOSED')),
    broker_position_id TEXT,
    state JSONB NOT NULL,
    version BIGINT NOT NULL CHECK(version>=1),
    updated_at TIMESTAMPTZ NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS v2_open_broker_position_uidx
ON v2_positions(broker_position_id) WHERE broker_position_id IS NOT NULL AND status='OPEN';
CREATE INDEX IF NOT EXISTS v2_positions_portfolio_status_idx ON v2_positions(portfolio_id,status);

CREATE TABLE IF NOT EXISTS v2_reconciliation_cases (
    case_id TEXT PRIMARY KEY,
    order_command_id TEXT NOT NULL REFERENCES v2_order_commands(order_command_id) ON DELETE RESTRICT,
    status TEXT NOT NULL CHECK(status IN ('OPEN','RESOLVED_FILLED','RESOLVED_ABSENT','MANUAL_REVIEW')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts>=0),
    broker_snapshot_hash CHAR(64) NOT NULL CHECK(broker_snapshot_hash ~ '^[0-9a-f]{64}$'),
    detail TEXT NOT NULL DEFAULT '',
    opened_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS v2_outbox (
    outbox_id TEXT PRIMARY KEY,
    topic TEXT NOT NULL,
    payload JSONB NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL,
    claimed_by TEXT,
    claim_token TEXT,
    lease_expires_at TIMESTAMPTZ,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count>=0),
    delivered_at TIMESTAMPTZ,
    last_error_type TEXT
);
CREATE INDEX IF NOT EXISTS v2_outbox_claim_idx
ON v2_outbox(delivered_at,lease_expires_at,created_at);

CREATE TABLE IF NOT EXISTS v2_inbox (
    message_id TEXT PRIMARY KEY,
    topic TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    payload_hash CHAR(64) NOT NULL CHECK(payload_hash ~ '^[0-9a-f]{64}$'),
    received_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS v2_pnl_daily (
    portfolio_id TEXT NOT NULL,
    day DATE NOT NULL,
    realized_usd NUMERIC(38,18) NOT NULL,
    unrealized_usd NUMERIC(38,18) NOT NULL,
    fees_usd NUMERIC(38,18) NOT NULL,
    financing_usd NUMERIC(38,18) NOT NULL,
    daily_pnl_usd NUMERIC(38,18) NOT NULL,
    equity_usd NUMERIC(38,18) NOT NULL CHECK(equity_usd>=0),
    recorded_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY(portfolio_id,day)
);

CREATE TABLE IF NOT EXISTS v2_service_heartbeats (
    service TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    details JSONB NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS v2_data_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    manifest_hash CHAR(64) NOT NULL CHECK(manifest_hash ~ '^[0-9a-f]{64}$'),
    manifest JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS v2_hypotheses (
    hypothesis_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    definition JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS v2_experiments (
    experiment_id TEXT PRIMARY KEY,
    hypothesis_id TEXT NOT NULL REFERENCES v2_hypotheses(hypothesis_id) ON DELETE RESTRICT,
    data_snapshot_id TEXT NOT NULL REFERENCES v2_data_snapshots(snapshot_id) ON DELETE RESTRICT,
    code_sha TEXT NOT NULL,
    config_hash CHAR(64) NOT NULL CHECK(config_hash ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS v2_parameter_trials (
    trial_id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL REFERENCES v2_experiments(experiment_id) ON DELETE RESTRICT,
    parameters JSONB NOT NULL,
    result JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS v2_parameter_trials_experiment_idx ON v2_parameter_trials(experiment_id,created_at);
CREATE TABLE IF NOT EXISTS v2_statistical_tests (
    test_id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL REFERENCES v2_experiments(experiment_id) ON DELETE RESTRICT,
    test_name TEXT NOT NULL,
    result JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS v2_untouched_sets (
    split_id TEXT PRIMARY KEY,
    data_snapshot_id TEXT NOT NULL REFERENCES v2_data_snapshots(snapshot_id) ON DELETE RESTRICT,
    definition JSONB NOT NULL,
    consumed_by_experiment_id TEXT REFERENCES v2_experiments(experiment_id) ON DELETE RESTRICT,
    consumed_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS v2_promotion_decisions (
    decision_id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL REFERENCES v2_experiments(experiment_id) ON DELETE RESTRICT,
    decision TEXT NOT NULL CHECK(decision IN ('PROMOTE','REJECT','RETIRE','CONTINUE_SHADOW')),
    evidence JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS v2_structured_memory (
    memory_id TEXT NOT NULL,
    category TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version>=1),
    body JSONB NOT NULL,
    evidence_refs JSONB NOT NULL,
    valid_from TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ,
    PRIMARY KEY(memory_id,version)
);
CREATE INDEX IF NOT EXISTS v2_structured_memory_active_idx
ON v2_structured_memory(category,valid_from,expires_at);

CREATE TABLE IF NOT EXISTS v2_audit_anchors (
    anchor_id TEXT PRIMARY KEY,
    head_event_hash CHAR(64) NOT NULL CHECK(head_event_hash ~ '^[0-9a-f]{64}$'),
    signature TEXT NOT NULL,
    algorithm TEXT NOT NULL,
    destination TEXT NOT NULL,
    anchored_at TIMESTAMPTZ NOT NULL
);
DROP TRIGGER IF EXISTS v2_audit_anchors_append_only ON v2_audit_anchors;
CREATE TRIGGER v2_audit_anchors_append_only BEFORE UPDATE OR DELETE ON v2_audit_anchors
FOR EACH ROW EXECUTE FUNCTION v2_reject_append_only_mutation();
