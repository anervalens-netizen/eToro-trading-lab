# v2 security and reliability boundary

## Authority separation

| Process | Market read key | DEMO write key | PostgreSQL | Risk signing authority | ChatGPT auth |
|---|---:|---:|---:|---:|---:|
| market archive | yes | no | archive/index only | no | no |
| coordinator | yes | no | yes | no | no |
| role/decision appliers | decision applier: read only | no | yes | deterministic kernel only | no |
| Sol runner (Dell) | no | no | wire over SSH only | no | yes, read-only mount inside sandbox |
| DEMO executor | no separate read key; DEMO write credential includes required broker reads | yes | yes | no | no |
| dashboard | no | no | read-only | no | no |
| audit anchor | no | no | event head/read | independent anchor key only | no |

A process that owns broker write credentials does not run an LLM. A process that runs an LLM does not receive eToro credentials, a PostgreSQL DSN, server SSH keys, or a generic broker tool.

This table is the required authority model, not current OS-isolation evidence. The checked-in units still use the shared `etoro-agent` user/state boundary. V2 unattended execution remains disabled until collector, signer and executor have distinct OS identities and state paths, the signer has no network or broker credential, the executor has only the public verification key and DEMO write credential, and negative permission tests pass.

## DEMO write boundary

Canonical production write service: `etoro-v2-executor-postgres.service`.

It starts only when all required credential files exist and `/etc/etoro-agent/ENABLE_V2_DEMO_EXECUTION` exists. The gateway allows only:

- current DEMO open route;
- current DEMO market-close position route;
- required DEMO read/preflight routes.

There is no REAL route/config/service in v2.

## Model boundary

The Sol runner is stateless. Each run receives one hash-bound packet and one strict output schema. The subprocess is launched under a transient systemd sandbox with:

- `NoNewPrivileges=yes`;
- read-only ChatGPT auth bind;
- temporary HOME/CODEX_HOME;
- SSH paths inaccessible to the model subprocess;
- no arbitrary executable surface beyond the Codex binary;
- no file/browser/tool authority supplied in the prompt contract.

External headlines/text are data, never instructions. Structured event ingestion rejects obvious instruction-like injection patterns, and `prompt_eval_v2.py` provides adversarial regression cases.

## Idempotency and network ambiguity

Every economic command carries a stable idempotency key/client request ID. Before send, the command is persisted and enters `SUBMITTING`. If an exception, timeout, 429/5xx or other ambiguous network outcome can have crossed the network, the order becomes `UNKNOWN`; new risk halts and the command is reconciled rather than retried blindly.

ACK is not a fill. Only fill evidence mutates a position.

For opens, the sealed command includes the intent hash, final-entry band, stop/target fractions, slippage cap, dollar-loss cap and available notional/loss/slot budgets. Command, pending order, risk reservation, execution outbox and approval event are committed atomically. Reservations remain active through `UNKNOWN` and partial fills.

The executor uses one final entry quote for stop/target construction and broker preparation. Before the network-write boundary it validates long/short direction, entry band, quote freshness/spread and worst-case loss including broker cost-preview components. The immutable preflight evidence is stored in the submit event.

## Audit integrity

Economic state changes and corresponding domain events are committed in the same database transaction. The event log is hash chained. PostgreSQL uses transactional/advisory locking for chain serialization. An independent Ed25519 anchor signs the current event-chain head and exports it to a separate backup path hourly.

Hash chaining is tamper-evident, not immutable storage by itself; signed off-process anchors are the external evidence boundary.

## Secrets

- no eToro key is stored in Git, database payloads, dashboard or logs;
- systemd `LoadCredential` is used for service credentials;
- read and write user keys are separate;
- required OS users/state paths are separate before activation; this is still a deployment blocker, not a current claim;
- DSN is a credential file, not a command-line argument;
- dashboard proxy boundary secret is a credential file;
- the CI secret-pattern guard rejects common credential material.

## Dashboard

The v2 dashboard listens on a Unix socket only. Requests except `/healthz` require:

- expected local trusted proxy when configured;
- exact proxy boundary secret;
- exact Authentik owner username.

No broker credentials are loaded into the dashboard process.

## Failure defaults

- missing/stale/future quote -> no new open;
- data-quality failure -> no new open;
- AI unavailable/invalid/expired -> `HOLD` for new AI risk;
- audit invalid -> no promotion / operational error;
- broker cash/eligibility/cost preview unavailable -> no open;
- reconciliation ambiguity -> `HALT_NEW`;
- drawdown/loss gate -> `REDUCE_ONLY` or `LOCKED` according to mandate;
- reduce-only safety exits remain possible when new risk is halted where broker truth permits.
