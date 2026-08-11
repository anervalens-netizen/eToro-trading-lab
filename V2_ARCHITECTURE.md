# eToro Trading Lab v2 — architecture

## Scope

v2 is a DEMO/paper autonomous trading research system. REAL execution is intentionally absent. The model may generate, rank, veto or close intents, but only deterministic code can create an executable order command.

## Canonical flow

```text
eToro WebSocket archive + validated closed candles + REST broker truth
              |
              v
immutable raw catalog -> normalized features -> compact strategy families
              |                                |
              |                                +-> deterministic lane A
              v
Decision Packet v2 -> stateless Regime / Critic / Portfolio Decider Sol roles
              |
              v
strict schema + evidence binding + packet hash + expiry
              |
              v
DecisionApplyWorkerV2
              |
              v
UnifiedTradingKernel
  - quote provenance / freshness / drift
  - global capital mandate
  - cash / exposure / drawdown / loss gates
  - signed execution band + max-loss envelope
  - deterministic exits and reduce-only closes
              |
              v
PostgreSQL command + pending order + risk reservation + event + transactional outbox
              |
              v
current eToro DEMO gateway
  eligibility -> one final quote -> exact cost preview -> deterministic re-risk -> open/close
              |
              v
ACK != fill -> reconciliation -> fills -> positions -> P&L
              |
              v
signed audit anchor + owner-only dashboard + research registry
```

## Canonical production components

- `PostgresRuntimeStoreV2`: canonical multi-process execution state.
- `UnifiedTradingKernel`: shared economic state machine for historical, shadow and broker adapters.
- `AutonomousCoordinatorV2`: closed-bar trigger, feature construction, compact candidates, packet creation.
- `CanonicalPostgresAIStoreV2`: immutable AI packet queue, lease/claim token, budgets and run telemetry.
- `sol_runner_v2`: stateless ChatGPT-authenticated Codex worker; no broker credentials/tools.
- `DecisionApplyWorkerV2`: turns validated AI output into an intent/reduce-only command only through the deterministic kernel.
- `DemoExecutionWorkerCurrentV2`: current eToro DEMO write adapter and preflight.
- `DemoReconciliationWorkerV2`: read-only broker-truth worker; resolves only exact identities and sends ambiguity to manual review.
- `etoro_api_current_v2`: pinned DEMO-only Public API gateway.
- `dashboard_worker_v2` and `anchor_worker_v2`: PostgreSQL-backed read/audit projections; neither receives broker write credentials.

`RuntimeStoreV2` remains the SQLite reference/replay implementation. It is not the canonical multi-service production store.

## Event-time rules

Every actionable price has both broker/event time and processing/received time. Missing provenance, future data, stale quotes, wide spreads, data-quality failure or excessive drift fail closed for new risk. Historical and shadow execution use next observable information and the same exit precedence as the live kernel.

## Order lifecycle

`CREATED -> RISK_APPROVED -> SUBMITTING -> ACKNOWLEDGED -> PARTIALLY_FILLED/FILLED` with explicit `REJECTED`, `UNKNOWN`, `CANCELLED`, and reconciliation states. Command, pending broker order, active risk reservation, execution outbox row and approval event become visible in one transaction. The executor can claim only that ready outbox row. A broker ACK never mutates position quantity. Only fill evidence does.

A reservation is released only after deterministic rejection/cancellation/expiry, authoritative absence, or final fill. It remains active for partial fills and `UNKNOWN` outcomes. A request that may have crossed the network but has no authoritative outcome becomes `UNKNOWN`, switches new risk to `HALT_NEW`, and is reconciled rather than blindly retried.

Immediately before `SUBMITTING`, the executor binds stop/target to one final broker quote, validates direction and the signed entry band, parses the exact broker cost preview, and proves worst-case stop loss plus signed slippage plus known costs is within the sealed dollar-loss cap. The quote and cost evidence is persisted with the submit transition.

An exact broker position is not projected as a final fill while the corresponding broker order is still pending. Missing request/position identity, incomplete close price/quantity, or ambiguous broker truth remains `MANUAL_REVIEW` and keeps trading locked.

## Exit precedence

1. explicit agent close / reduce-only safety action;
2. invalid data / mandatory risk reduction;
3. gap-through-stop at first observable executable price;
4. stop-loss;
5. take-profit;
6. maximum holding time;
7. strategy invalidation;
8. overnight/weekend policy;
9. end-of-test close in replay.

When OHLC ordering is unknowable and both stop and target are touched, stop wins.

## Strategy surface

v2 deliberately reduces the research surface to distinct families:

1. trend / breakout;
2. session momentum;
3. regime mean reversion;
4. true multi-leg relative value — shadow-only until leg execution is validated;
5. commodity quantitative event / term-structure carry;
6. simple statistical baseline;
7. Sol direct intent.

Parameter variations are experiments inside a family, not independent capital pools.

## AI authority

The AI process receives a sanitized immutable packet only. It has no eToro credentials, no generic tool access, no shell access, no risk-policy mutation and no direct broker route. Allowed portfolio actions are `OPEN`, `CLOSE`, `PARTIAL_CLOSE`, `HOLD`. Every non-HOLD action is revalidated against fresh broker truth after the model response.

If ChatGPT/Codex is unavailable, a packet expires or output validation fails, no new AI risk is opened. Deterministic/reduce-only safety paths remain available.

## Research truth

A research epoch binds dataset snapshot, feature version, strategy version, cost model, risk contract, prompt version, code SHA and config hash. A semantic change starts a new epoch and invalidates pending intents/decisions for comparison purposes instead of mixing statistics.

Promotion requires chronological OOS evidence, multiple-testing controls, stressed costs, historical/shadow parity, a DEMO soak period, no unresolved executions and one untouched final test. None of those gates is inferred from code quality alone.
