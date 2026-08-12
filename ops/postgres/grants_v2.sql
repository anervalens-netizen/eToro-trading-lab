REVOKE ALL ON DATABASE etoro_v2 FROM PUBLIC;
GRANT CONNECT ON DATABASE etoro_v2 TO
  "etoro-candidate", "etoro-ai", "etoro-decision", "etoro-decision-exec", "etoro-exit",
  "etoro-reconciler", "etoro-control", "etoro-executor",
  "etoro-observer", "etoro-collector";
REVOKE CONNECT ON DATABASE etoro_v2 FROM "etoro-engine";

REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO
  "etoro-candidate", "etoro-ai", "etoro-decision", "etoro-decision-exec", "etoro-exit",
  "etoro-reconciler", "etoro-control", "etoro-executor",
  "etoro-observer", "etoro-collector";

-- Repeated provisioning converges to this exact least-privilege matrix.
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM
  "etoro-engine", "etoro-candidate", "etoro-ai", "etoro-decision", "etoro-decision-exec",
  "etoro-exit", "etoro-reconciler", "etoro-control", "etoro-executor",
  "etoro-observer", "etoro-collector";
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM
  "etoro-engine", "etoro-candidate", "etoro-ai", "etoro-decision", "etoro-decision-exec",
  "etoro-exit", "etoro-reconciler", "etoro-control", "etoro-executor",
  "etoro-observer", "etoro-collector";
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM
  "etoro-engine", "etoro-candidate", "etoro-ai", "etoro-decision", "etoro-decision-exec",
  "etoro-exit", "etoro-reconciler", "etoro-control", "etoro-executor",
  "etoro-observer", "etoro-collector";

GRANT SELECT ON v2_meta,v2_schema_migrations,v2_trading_state,
  v2_broker_orders,v2_risk_reservations,v2_positions,v2_events
TO "etoro-candidate";
GRANT SELECT,INSERT ON v2_ai_packets TO "etoro-candidate";

GRANT SELECT ON v2_meta,v2_schema_migrations,v2_trading_state,v2_events,
  v2_ai_packets,v2_ai_runs,v2_ai_budget_claims TO "etoro-ai";
GRANT INSERT,UPDATE ON v2_ai_packets TO "etoro-ai";
GRANT INSERT ON v2_ai_runs,v2_ai_budget_claims TO "etoro-ai";

GRANT SELECT ON v2_meta,v2_schema_migrations,v2_trading_state,v2_ai_packets
TO "etoro-decision";
GRANT UPDATE ON v2_ai_packets TO "etoro-decision";

GRANT SELECT ON v2_meta,v2_schema_migrations,v2_trading_state,v2_intents,
  v2_decisions,v2_order_commands,v2_broker_orders,v2_risk_reservations,
  v2_fills,v2_positions,v2_reconciliation_cases,v2_outbox,v2_events,
  v2_ai_packets TO "etoro-decision-exec";
GRANT INSERT,UPDATE ON v2_intents,v2_decisions,
  v2_order_commands,v2_broker_orders,v2_risk_reservations,v2_outbox,
  v2_ai_packets
TO "etoro-decision-exec";
GRANT INSERT ON v2_events TO "etoro-decision-exec";

GRANT SELECT ON v2_meta,v2_schema_migrations,v2_trading_state,v2_decisions,
  v2_order_commands,v2_broker_orders,v2_risk_reservations,v2_positions,
  v2_fills,v2_reconciliation_cases,v2_outbox,v2_events TO "etoro-exit";
GRANT INSERT,UPDATE ON v2_decisions,v2_order_commands,
  v2_broker_orders,v2_risk_reservations,v2_outbox
TO "etoro-exit";
GRANT INSERT ON v2_events TO "etoro-exit";

GRANT SELECT ON v2_meta,v2_schema_migrations,v2_trading_state,v2_intents,
  v2_decisions,v2_order_commands,v2_broker_orders,v2_risk_reservations,
  v2_fills,v2_positions,v2_reconciliation_cases,v2_outbox,v2_events,
  v2_pnl_daily TO "etoro-reconciler";
GRANT INSERT,UPDATE ON v2_decisions,v2_order_commands,
  v2_broker_orders,v2_risk_reservations,v2_positions,v2_reconciliation_cases,
  v2_outbox,v2_pnl_daily TO "etoro-reconciler";
GRANT INSERT ON v2_fills,v2_events TO "etoro-reconciler";

GRANT SELECT ON v2_meta,v2_schema_migrations,v2_trading_state,v2_broker_orders,
  v2_risk_reservations,v2_outbox,v2_events TO "etoro-control";
GRANT UPDATE ON v2_broker_orders,v2_risk_reservations,v2_outbox
TO "etoro-control";
GRANT INSERT ON v2_events TO "etoro-control";

GRANT SELECT ON v2_meta,v2_schema_migrations,v2_trading_state,v2_intents,
  v2_order_commands,v2_broker_orders,v2_risk_reservations,v2_fills,
  v2_positions,v2_reconciliation_cases,v2_outbox,v2_events,
  v2_service_heartbeats TO "etoro-executor";
GRANT UPDATE ON v2_broker_orders,v2_risk_reservations,v2_outbox
TO "etoro-executor";
GRANT INSERT ON v2_broker_orders,v2_events TO "etoro-executor";

GRANT SELECT ON ALL TABLES IN SCHEMA public TO "etoro-observer";
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO "etoro-observer";

GRANT SELECT ON v2_meta,v2_schema_migrations TO "etoro-collector";
GRANT EXECUTE ON FUNCTION v2_record_market_heartbeat(TEXT,JSONB,TIMESTAMPTZ)
TO "etoro-collector";
GRANT EXECUTE ON FUNCTION v2_record_service_heartbeat(TEXT,TEXT,JSONB,TIMESTAMPTZ)
TO "etoro-candidate", "etoro-ai", "etoro-decision", "etoro-decision-exec",
  "etoro-exit", "etoro-reconciler", "etoro-executor";

GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO
  "etoro-candidate", "etoro-ai", "etoro-decision-exec", "etoro-exit",
  "etoro-reconciler", "etoro-control", "etoro-executor";

REVOKE UPDATE,DELETE,TRUNCATE ON
  v2_events,v2_fills,v2_audit_anchors,v2_ai_runs,v2_ai_budget_claims
FROM "etoro-engine", "etoro-candidate", "etoro-ai", "etoro-decision", "etoro-decision-exec",
  "etoro-exit", "etoro-reconciler", "etoro-control", "etoro-executor",
  "etoro-observer", "etoro-collector";
REVOKE ALL ON FUNCTION v2_reject_append_only_mutation() FROM PUBLIC;
REVOKE ALL ON FUNCTION v2_trip_audit_integrity_failure() FROM PUBLIC;
REVOKE ALL ON FUNCTION v2_update_peak_equity(NUMERIC) FROM PUBLIC;
REVOKE ALL ON FUNCTION v2_set_runtime_meta(TEXT,TEXT,TIMESTAMPTZ) FROM PUBLIC;
REVOKE ALL ON FUNCTION v2_transition_trading_state(TEXT,TEXT,TEXT,TIMESTAMPTZ) FROM PUBLIC;
REVOKE ALL ON FUNCTION v2_lock_trading_state() FROM PUBLIC;
REVOKE ALL ON FUNCTION v2_append_ai_telemetry_event(
  TEXT,TEXT,INTEGER,TIMESTAMPTZ,TIMESTAMPTZ,TEXT,TEXT,TEXT,JSONB,TEXT
) FROM PUBLIC;
REVOKE ALL ON FUNCTION v2_lock_ai_authority() FROM PUBLIC;
REVOKE ALL ON FUNCTION v2_record_service_heartbeat(TEXT,TEXT,JSONB,TIMESTAMPTZ)
FROM PUBLIC;
GRANT EXECUTE ON FUNCTION v2_reject_append_only_mutation() TO
  "etoro-candidate", "etoro-ai", "etoro-decision-exec", "etoro-exit",
  "etoro-reconciler", "etoro-control", "etoro-executor";
GRANT EXECUTE ON FUNCTION v2_trip_audit_integrity_failure() TO
  "etoro-candidate", "etoro-ai", "etoro-decision", "etoro-decision-exec", "etoro-exit",
  "etoro-reconciler", "etoro-control", "etoro-executor";
GRANT EXECUTE ON FUNCTION v2_update_peak_equity(NUMERIC) TO
  "etoro-decision-exec", "etoro-exit", "etoro-executor";
GRANT EXECUTE ON FUNCTION v2_set_runtime_meta(TEXT,TEXT,TIMESTAMPTZ) TO
  "etoro-candidate", "etoro-ai", "etoro-reconciler";
GRANT EXECUTE ON FUNCTION v2_transition_trading_state(TEXT,TEXT,TEXT,TIMESTAMPTZ) TO
  "etoro-decision-exec", "etoro-exit", "etoro-reconciler", "etoro-control", "etoro-executor";
GRANT EXECUTE ON FUNCTION v2_lock_trading_state() TO
  "etoro-decision-exec", "etoro-exit", "etoro-reconciler", "etoro-control", "etoro-executor";
GRANT EXECUTE ON FUNCTION v2_append_ai_telemetry_event(
  TEXT,TEXT,INTEGER,TIMESTAMPTZ,TIMESTAMPTZ,TEXT,TEXT,TEXT,JSONB,TEXT
) TO "etoro-ai", "etoro-decision", "etoro-decision-exec";
GRANT EXECUTE ON FUNCTION v2_lock_ai_authority() TO
  "etoro-candidate", "etoro-ai", "etoro-decision", "etoro-decision-exec";
