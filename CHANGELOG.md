# Changelog

## 0.5.0 - 2026-08-11

- repaired signed full/partial CLOSE execution and duplicate reduce-fill replay;
- added exact order/history reconciliation and continuous broker-side SL/TP projection;
- added dynamic execution gate, atomic lock/invalidation, systemd path isolation and legacy writer retirement;
- added deterministic exit manager independent of AI decisions;
- hardened event idempotency, PostgreSQL serialization, economic fingerprints and AI dead-letter flows;
- made commodities research-only under the current 1,000 USD mandate;
- added exit slippage, financing, calendar P&L and durable shadow peak equity;
- expanded health freshness/SLO checks and complete checksum/restore/application smoke drills;
- removed the installable v1 broker-writer unit and CLI entrypoints;
- added push-to-main CI, coverage/type/shell/systemd/SQL gates, pinned CI dependencies, hashed runtime lock, SBOM and attested offline release provenance.

## 0.4.0 - 2026-08-11

- introduced the PostgreSQL v2 unified kernel, isolated signer, AI queue, immutable release flow, backup/restore and shadow runtime.
