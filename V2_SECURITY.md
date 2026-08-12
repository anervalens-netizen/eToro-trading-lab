# V2 security boundary

## Authority matrix

| Service identity | Broker | PostgreSQL role | Other authority |
|---|---|---|---|
| `etoro-collector` | DEMO read | collector | raw market archive only |
| `etoro-candidate` | DEMO read | candidate | packets/candidates, no commands |
| `etoro-ai` | none | AI queue only | no broker/signer |
| `etoro-decision` | none | shadow decision | AI queue consume/telemetry only; no signer or command write |
| `etoro-decision-exec` | DEMO read | execution decision | signer client; epoch-bound command commit |
| `etoro-exit` | DEMO read | exit | signer client; reduce-only command |
| `etoro-reconciler` | DEMO read | reconciler | fills/reconciliation only |
| `etoro-control` | none | control | gate lock/invalidation only |
| `etoro-executor` | DEMO write | executor | only broker writer |
| `etoro-signer` | none | none | private risk key, AF_UNIX only |
| `etoro-observer` | none | observer | dashboard/anchor/backup reads |

`etoro-engine` is retained only as a revoked migration identity: NOLOGIN and no
database CONNECT. Grants are explicit in `ops/postgres/grants_v2.sql` and the
boundary test proves negative writes, not just positive connectivity.
Service roles cannot write `v2_service_heartbeats` directly. Schema 9 maps each
exact PostgreSQL login to one fixed heartbeat key, rejecting cross-service
spoofing even after a compromised worker.

## Broker and risk boundary

Read and write DEMO user keys are separate and delivered only through systemd
`LoadCredential`. Startup rejects REAL or incompatible scopes. No credential is
stored in Git, database payloads, argv, dashboard or logs.

The signer authenticates both allowed peer UIDs with `SO_PEERCRED`, revalidates
the fixed DEMO mandate and signs exact economics/provenance. It has no network,
broker key or DSN. Only `etoro-decision-exec` and `etoro-exit` can reach its
socket; the shadow UID cannot. The executor has only the public verification
key and cannot reach the signer socket.

Every execution boundary rechecks gate, the epoch sealed into the OPEN command
and outbox, expiry, seal, current risk
hash, broker snapshot, exact request hash and strategy release. OPEN fails
closed; a CLOSE is accepted in `LOCKED` only when the DEMO gate is present and
the order is exact broker-bound reduce-only.

Authenticated REST and WebSocket clients reject redirects before credentials
can be retransmitted. Broker identity, economic and timestamp aliases must be
exact, typed and mutually consistent; ambiguous HTTP success becomes
`UNKNOWN`, never an ACK.

## Sol/Codex boundary

Sol is a stateless critic/ranker over an immutable packet. It can select one
supplied deterministic candidate or veto; it cannot author economic terms.

The wire runner is credential-blind. A root-owned socket creates one bounded
model process with `NoNewPrivileges`, isolated temporary HOME, no SSH path,
strict executable allowlist, bounded stdout/stderr/time/memory/tasks and no
broker/database tool. Provisioning attests ChatGPT auth mode, account-id hash,
Codex executable hash and exact configured model. Any Platform API key or model
fallback fails startup.

Dell runs only this bounded model/runner surface. Its fixed SSH hop starts the
primary wire as `etoro-ai`; the read-only `status` operation exposes only
release/schema/server metadata and the database `session_user`. Dell has no
local PostgreSQL, broker credential, signer, gate or executor authority.

## Integrity and failure defaults

- economic state and its event commit in one transaction;
- append-only events are hash chained and externally Ed25519-anchored;
- ACK never mutates position; only fill evidence does;
- ambiguous send becomes `UNKNOWN`, never blind retry;
- poison pre-submit outbox becomes audited `QUARANTINED`;
- broker/API/schema/calendar/identity drift blocks new risk;
- dashboard is Unix-socket-only behind proxy secret plus owner identity;
- backup and restore credentials are separate from runtime writers.

`ops/security/verify-v2-boundaries.sh full` is mandatory after every host
provisioning or identity/grant change.
