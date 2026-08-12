# ADR-001: PostgreSQL V2 is the only operational trading runtime

Status: accepted 2026-08-11; strengthened 2026-08-12.

Production is an immutable exact-SHA V2 release with PostgreSQL state, one
isolated DEMO writer, a dynamic gate, deterministic reduce-only exits and a
read-only reconciler. Each service uses its own OS identity and least-privilege
database role.

V1 is absent from the current tree and production wheel. SQLite code is an
isolated simulation/research aid and raw market index; it has no installed
state-mutating CLI, systemd unit, gate authority or broker credential. A second
operational writer is an architecture violation.

This ADR does not authorize DEMO activation. REAL trading remains unsupported.
