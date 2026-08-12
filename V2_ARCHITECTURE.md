# eToro Trading Lab V2 architecture

## Scope

V2 is a DEMO research and guarded-execution platform. Code quality proves
safety properties, not trading edge. REAL execution is absent.

## Canonical path

```text
eToro WS/REST -> raw archive -> strict schemas/calendar/snapshot eligibility
       -> CandidateEngineV2 -> immutable DecisionPacket -> stateless Sol critic/ranker
       -> exact supplied plan -> deterministic risk kernel -> isolated Ed25519 signer
       -> PostgreSQL command/reservation/event/outbox -> one DEMO executor
       -> ACK -> reconciliation -> fills -> position/P&L -> signed anchor
```

There is one candidate implementation: `CandidateEngineV2`. Backtest, parity,
shadow and coordinator call the same artifact and bind engine/parameter/feature
hashes. `StrategyFamilyEngine` is only an API-compatible facade over that engine.
Arbitrary signal factories are benchmark-only and cannot produce promotion
evidence.

Each decision uses one immutable `BrokerAccountSnapshotV2`: positions, pending
orders, available cash, request identity and defensible observation time come
from the same broker response window. Malformed, duplicate, manual/copy/mirror
or foreign activity blocks OPEN.

## Execution authority

| Gate | State | OPEN | exact reduce-only CLOSE |
|---|---|---:|---:|
| absent | any | no | no; manual freeze |
| present | `LOCKED` | no | yes |
| present | `ACTIVE` | current epoch only | yes |
| present | other halt state | no | mandate-specific reduce only |

Deleting the gate atomically locks trading, invalidates unstarted commands and
stops write-capable units. AI packets, signed OPEN commands and outbox envelopes
bind the current authority epoch; stale or epoch-less work is expired or
quarantined before model budget, claim, commit or broker write. Shadow and
execution decision paths use distinct OS and PostgreSQL identities, and shadow
cannot reach the signer.

OPEN additionally requires a deployment-pinned `StrategyReleaseManifestV2`.
It binds the exact candidate engine, point-in-time dataset, feature schema,
dynamic calendar, simulator, OOS evidence, promotion, soak, and observed p95
round-trip costs per symbol. Costs must have at least 100 observations, be at
most 30 days old and survive at least 2x stress. No manifest is fabricated or
checked into the repository.

## Persistence and concurrency

PostgreSQL is the only operational source of truth. Command, broker order,
risk reservation, event and outbox become visible atomically. Distinct fills
are serialized per broker position with transaction/advisory locks and
hash-CAS; peak equity uses atomic `GREATEST` semantics.

Outbox failures are classified before the possible network boundary. A
deterministically invalid row is terminally quarantined after the bounded
attempt policy, audited and skipped so later rows continue. Once a request may
have crossed the network, ambiguity becomes `UNKNOWN`, preserves reservation,
halts new risk and is reconciled without blind retry.

## Data and broker contracts

- strict JSON: exact booleans, finite Decimal values, unknown fields rejected;
- account mode exactly `demo`, leverage exactly 1, one open position maximum;
- with one-position policy, correlated exposure conservatively equals gross exposure;
  any future multi-position release requires explicit cluster/factor risk and new tests;
- WS handshake/order/topic/instrument/sequence aliases must agree;
- connection epoch and complete snapshot eligibility persist across restart;
- market calendar is versioned, hashed and finite-lived; unknown session closes entry;
- broker request bytes are canonical, quantized, hashed, persisted and transmitted identically;
- partial close enforces instrument precision, minimum quantity and residual rules;
- shared cross-process rate limiting reserves priority for exits;
- reconciliation paginates history and never fabricates price, fee, financing, time or exit reason.

## Process/database separation

Collector, candidate, AI, decision committer, exit manager, reconciler, control,
executor, signer and observer use distinct OS identities. Matching PostgreSQL
roles receive only required tables/actions; retired `etoro-engine` is NOLOGIN
and cannot connect. The LLM has neither broker credentials nor DB authority.
Runtime roles cannot update trading state, protected metadata or peak equity
directly. Narrow database functions enforce restrictive-only state transitions,
control-only activation, protected metadata keys and monotonic peak updates.

## Non-production SQLite

SQLite stores remain useful for deterministic simulation, unit tests, research
registries and the raw market index. They are not packaged as an operational
alternative: `etoro-v2` exposes no writer/state command and systemd has no
SQLite executor. Only PostgreSQL can own live authority.
