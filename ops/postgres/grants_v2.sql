REVOKE ALL ON DATABASE etoro_v2 FROM PUBLIC;
GRANT CONNECT ON DATABASE etoro_v2 TO
  "etoro-engine", "etoro-executor", "etoro-observer", "etoro-collector";
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO
  "etoro-engine", "etoro-executor", "etoro-observer", "etoro-collector";

-- Make repeated provisioning converge to this file's privilege model instead
-- of preserving grants from an older candidate.
REVOKE ALL ON ALL TABLES IN SCHEMA public
FROM "etoro-engine", "etoro-executor", "etoro-observer", "etoro-collector";
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public
FROM "etoro-engine", "etoro-executor", "etoro-observer", "etoro-collector";
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public
FROM "etoro-engine", "etoro-executor", "etoro-observer", "etoro-collector";

GRANT SELECT,INSERT,UPDATE ON
  v2_meta,v2_trading_state,v2_intents,v2_decisions,v2_order_commands,
  v2_broker_orders,v2_risk_reservations,v2_positions,v2_reconciliation_cases,
  v2_outbox,v2_inbox,v2_pnl_daily,v2_service_heartbeats,v2_data_snapshots,
  v2_hypotheses,v2_experiments,v2_parameter_trials,v2_statistical_tests,
  v2_untouched_sets,v2_promotion_decisions,v2_structured_memory,
  v2_ai_packets
TO "etoro-engine";
GRANT SELECT ON v2_schema_migrations TO "etoro-engine";
GRANT SELECT,INSERT ON
  v2_events,v2_fills,v2_audit_anchors,v2_ai_runs,v2_ai_budget_claims
TO "etoro-engine";
GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO "etoro-engine";

GRANT SELECT ON
  v2_meta,v2_schema_migrations,v2_trading_state,v2_intents,v2_order_commands,v2_broker_orders,
  v2_risk_reservations,v2_fills,v2_positions,v2_reconciliation_cases,
  v2_outbox,v2_events,v2_service_heartbeats
TO "etoro-executor";
GRANT UPDATE ON
  v2_trading_state,v2_broker_orders,v2_risk_reservations,v2_outbox
TO "etoro-executor";
GRANT INSERT,UPDATE ON
  v2_positions,v2_reconciliation_cases,v2_service_heartbeats
TO "etoro-executor";
GRANT INSERT ON v2_fills,v2_events TO "etoro-executor";
GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO "etoro-executor";

GRANT SELECT ON ALL TABLES IN SCHEMA public TO "etoro-observer";
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO "etoro-observer";

GRANT SELECT ON v2_meta,v2_schema_migrations TO "etoro-collector";
GRANT EXECUTE ON FUNCTION v2_record_market_heartbeat(TEXT,JSONB,TIMESTAMPTZ)
TO "etoro-collector";

REVOKE UPDATE,DELETE,TRUNCATE ON
  v2_events,v2_fills,v2_audit_anchors,v2_ai_runs,v2_ai_budget_claims
FROM "etoro-engine", "etoro-executor", "etoro-observer", "etoro-collector";
REVOKE ALL ON FUNCTION v2_reject_append_only_mutation() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION v2_reject_append_only_mutation()
TO "etoro-engine", "etoro-executor";
