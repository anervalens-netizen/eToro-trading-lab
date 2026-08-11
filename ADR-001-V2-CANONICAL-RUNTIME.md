# ADR-001: PostgreSQL v2 is the only operational trading runtime

Status: accepted, 2026-08-11.

The mutable-checkout v1 executor is retired. Production uses the immutable exact-SHA v2 release, PostgreSQL state, one isolated DEMO writer, dynamic execution gate, deterministic exit manager and read-only reconciliation worker.

Legacy SQLite/source modules remain only for replay, regression tests and forensic interpretation. They are not deployment alternatives and receive no broker write credential. The v1 executor unit and CLI commands are absent; provisioning masks any previously installed `etoro-demo-executor.service`. Enabling a second writer violates this decision.

This decision does not authorize DEMO activation and does not introduce REAL trading. Empirical promotion gates remain separate.
