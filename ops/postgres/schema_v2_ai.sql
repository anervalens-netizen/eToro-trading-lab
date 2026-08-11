CREATE TABLE IF NOT EXISTS v2_ai_packets(
    packet_id TEXT PRIMARY KEY,
    packet_hash CHAR(64) NOT NULL UNIQUE CHECK(packet_hash ~ '^[0-9a-f]{64}$'),
    packet JSONB NOT NULL,
    role TEXT NOT NULL,
    lane TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN (
      'PENDING','CLAIMED','DECIDED','ERROR','EXPIRED','APPLIED'
    )),
    created_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    claimed_by TEXT,
    claim_token TEXT,
    lease_expires_at TIMESTAMPTZ,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count>=0),
    output JSONB,
    model TEXT,
    prompt_hash CHAR(64),
    apply_claimed_by TEXT,
    apply_claim_token TEXT,
    apply_lease_expires_at TIMESTAMPTZ,
    apply_attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(apply_attempt_count>=0),
    applied_effect JSONB,
    updated_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS v2_ai_packets_claim_idx
ON v2_ai_packets(state,expires_at,lease_expires_at,created_at);

CREATE TABLE IF NOT EXISTS v2_ai_runs(
    run_id TEXT PRIMARY KEY,
    packet_id TEXT NOT NULL REFERENCES v2_ai_packets(packet_id) ON DELETE RESTRICT,
    role TEXT NOT NULL,
    lane TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_hash CHAR(64) NOT NULL,
    output_hash CHAR(64),
    status TEXT NOT NULL CHECK(status IN ('COMPLETED','ERROR')),
    input_tokens INTEGER,
    output_tokens INTEGER,
    reasoning_tokens INTEGER,
    latency_ms INTEGER NOT NULL CHECK(latency_ms>=0),
    error_type TEXT,
    created_at TIMESTAMPTZ NOT NULL
);
DROP TRIGGER IF EXISTS v2_ai_runs_append_only ON v2_ai_runs;
CREATE TRIGGER v2_ai_runs_append_only BEFORE UPDATE OR DELETE ON v2_ai_runs
FOR EACH ROW EXECUTE FUNCTION v2_reject_append_only_mutation();

CREATE TABLE IF NOT EXISTS v2_ai_budget_claims(
    day DATE NOT NULL,
    role TEXT NOT NULL,
    lane TEXT NOT NULL,
    claim_key TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY(day,role,lane,claim_key)
);
DROP TRIGGER IF EXISTS v2_ai_budget_claims_append_only ON v2_ai_budget_claims;
CREATE TRIGGER v2_ai_budget_claims_append_only BEFORE UPDATE OR DELETE ON v2_ai_budget_claims
FOR EACH ROW EXECUTE FUNCTION v2_reject_append_only_mutation();
