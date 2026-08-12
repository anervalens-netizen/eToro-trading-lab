from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from .ai_store_postgres_v2 import CanonicalPostgresAIStoreV2
from .ai_v2 import AIIntentOutputV2, AIRole, DecisionPacketV2
from .candidates_v2 import canonical_candidate_engine
from .codec_v2 import decode_dataclass
from .config_v2 import AppConfigV2, load_config_v2
from .decision_v2 import DecisionApplierV2
from .domain_v2 import OrderStatus, QuoteProvenance, Side
from .etoro_api_current_v2 import EtoroPublicApiDemoClientV2
from .execution_gate_v2 import authority_for_state, execution_gate_path, execution_gate_present
from .kernel_v2 import UnifiedTradingKernel
from .postgres_runtime_v2 import PostgresRuntimeStoreV2
from .risk_seal_v2 import risk_mandate_hash
from .risk_signer_ipc_v2 import SocketRiskCommandSignerV2
from .risk_v2 import BrokerTruth, GlobalRiskKernel
from .strategy_release_v2 import require_deployed_strategy_release
from .systemd_notify_v2 import ready, watchdog

EXECUTION_GATE = execution_gate_path()


def _dsn(config_path: str) -> str:
    config = load_config_v2(config_path)
    path = os.getenv("ETORO_V2_POSTGRES_DSN_FILE") or config.postgres_dsn_file
    if not path:
        raise RuntimeError("PostgreSQL DSN credential file is required")
    value = Path(path).read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError("PostgreSQL DSN credential file is empty")
    return value


def _rate_row(client: EtoroPublicApiDemoClientV2, instrument_id: int) -> Mapping[str, Any]:
    response = client.rates((instrument_id,))
    rows = response.body.get("rates", []) if response.ok and isinstance(response.body, dict) else []
    if len(rows) != 1 or not isinstance(rows[0], Mapping):
        raise RuntimeError("fresh rate unavailable for AI decision application")
    return rows[0]


def _timestamp(value: object) -> datetime:
    if isinstance(value, (int, float)):
        seconds = float(value) / 1000 if float(value) > 10_000_000_000 else float(value)
        return datetime.fromtimestamp(seconds, UTC)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("broker timestamp is not timezone-aware")
    return parsed.astimezone(UTC)


def _quote(
    client: EtoroPublicApiDemoClientV2,
    *,
    symbol: str,
    instrument_id: int,
    broker_hash: str,
    received_at: datetime,
) -> QuoteProvenance:
    row = _rate_row(client, instrument_id)
    observed = _timestamp(row.get("date", row.get("timestamp", received_at.isoformat())))
    canonical = json.dumps(dict(row), sort_keys=True, separators=(",", ":"), default=str)
    return QuoteProvenance(
        symbol.upper(),
        Decimal(str(row["bid"])),
        Decimal(str(row["ask"])),
        observed,
        received_at,
        "etoro-public-api-decision-apply",
        str(row.get("sequence", "rest")),
        hashlib.sha256(canonical.encode()).hexdigest(),
        broker_hash,
    )


def _period_loss_metrics(
    realized_events: tuple[tuple[datetime, Decimal], ...],
    *,
    unrealized_usd: Decimal,
    now: datetime,
) -> tuple[Decimal, Decimal, Decimal]:
    current = now.astimezone(UTC)
    day_start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = day_start - timedelta(days=day_start.weekday())
    month_start = day_start.replace(day=1)
    conservative_unrealized = min(Decimal("0"), unrealized_usd)

    def since(start: datetime) -> Decimal:
        return (
            sum(
                (
                    amount
                    for event_time, amount in realized_events
                    if event_time.astimezone(UTC) >= start
                ),
                Decimal("0"),
            )
            + conservative_unrealized
        )

    return since(day_start), since(week_start), since(month_start)


def _shadow_effect(packet: DecisionPacketV2, output: AIIntentOutputV2) -> Mapping[str, Any]:
    output.validate(packet)
    candidate = output.selected_candidate(packet)
    return {
        "status": "shadow_only",
        "action": output.action.value,
        "candidate_id": output.candidate_id,
        "candidate_plan_hash": None
        if candidate is None
        else hashlib.sha256(
            json.dumps(candidate, sort_keys=True, separators=(",", ":"), default=str).encode()
        ).hexdigest(),
        "position_id": None if packet.position is None else packet.position.get("position_id"),
        "broker_write": False,
        "order_command_created": False,
    }


def _dated_period_pnl(
    store: PostgresRuntimeStoreV2,
    *,
    unrealized_usd: Decimal,
    now: datetime,
) -> tuple[Decimal, Decimal, Decimal]:
    current = now.astimezone(UTC)
    day_start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    earliest = min(day_start - timedelta(days=day_start.weekday()), day_start.replace(day=1))
    with store.connection.cursor() as cursor:
        cursor.execute(
            """SELECT event_time,payload->>'realized_delta_usd'
               FROM v2_events
               WHERE event_type IN ('PositionReduced','PositionClosed')
                 AND event_time >= %s
               ORDER BY event_time,sequence""",
            (earliest,),
        )
        rows = cursor.fetchall()
    events: list[tuple[datetime, Decimal]] = []
    for event_time, raw_amount in rows:
        if raw_amount is None:
            raise RuntimeError("dated realized P&L provenance is incomplete")
        events.append((event_time, Decimal(str(raw_amount))))
    return _period_loss_metrics(tuple(events), unrealized_usd=unrealized_usd, now=current)


def _broker_truth(
    store: PostgresRuntimeStoreV2,
    client: EtoroPublicApiDemoClientV2,
    *,
    config: AppConfigV2,
    now: datetime,
) -> BrokerTruth:
    snapshot = client.account_snapshot()
    positions = snapshot.positions
    open_orders = snapshot.open_orders
    pending_orders = snapshot.pending_orders
    equity = snapshot.equity_usd
    cash = snapshot.available_cash_usd
    gross = snapshot.gross_exposure_usd
    unrealized = snapshot.unrealized_pnl_usd
    pending_notional = snapshot.pending_manual_orders_usd + snapshot.pending_orders_usd
    peak = store.update_peak_equity(equity, at=now)
    daily_pnl, weekly_pnl, monthly_pnl = _dated_period_pnl(
        store,
        unrealized_usd=unrealized,
        now=now,
    )
    with store.connection.cursor() as cursor:
        cursor.execute("SELECT MAX(event_time) FROM v2_fills")
        last_trade_row = cursor.fetchone()
        last_trade_at = None if last_trade_row is None else last_trade_row[0]
    local_positions = store.positions(open_only=True)
    broker_by_id = {
        str(position.get("positionID", position.get("positionId", ""))).strip(): position
        for position in positions
        if isinstance(position, Mapping)
        and str(position.get("positionID", position.get("positionId", ""))).strip()
    }
    local_by_id = {
        str(position.broker_position_id): position
        for position in local_positions
        if position.broker_position_id is not None
    }
    failures: list[str] = list(snapshot.foreign_activity)
    if len(local_by_id) != len(local_positions):
        failures.append("local_position_without_broker_id")
    for broker_id in sorted(set(local_by_id) - set(broker_by_id)):
        failures.append(f"missing_broker_position:{broker_id}")
    for broker_id in sorted(set(broker_by_id) - set(local_by_id)):
        failures.append(f"unbound_broker_position:{broker_id}")
    for broker_id in sorted(set(local_by_id) & set(broker_by_id)):
        local = local_by_id[broker_id]
        broker_position = broker_by_id[broker_id]
        instrument_id = int(
            broker_position.get("instrumentID", broker_position.get("instrumentId", 0)) or 0
        )
        if instrument_id != config.symbols.get(local.symbol):
            failures.append(f"instrument_mismatch:{broker_id}")
        raw_side = broker_position.get("isBuy")
        broker_side = Side.BUY if raw_side is True else Side.SELL if raw_side is False else None
        if broker_side is not local.side:
            failures.append(f"side_mismatch:{broker_id}")
        try:
            broker_quantity = abs(
                Decimal(
                    str(
                        broker_position.get(
                            "units",
                            broker_position.get(
                                "quantity",
                                broker_position.get("unitsOwned", broker_position.get("netUnits")),
                            ),
                        )
                    )
                )
            )
            broker_entry = abs(
                Decimal(
                    str(
                        broker_position.get(
                            "openRate",
                            broker_position.get(
                                "averageOpenRate", broker_position.get("entryPrice")
                            ),
                        )
                    )
                )
            )
        except Exception:
            failures.append(f"invalid_economics:{broker_id}")
            continue
        if not broker_quantity.is_finite() or abs(broker_quantity - local.quantity) > Decimal(
            "0.00000001"
        ):
            failures.append(f"quantity_mismatch:{broker_id}")
        if not broker_entry.is_finite() or abs(broker_entry - local.entry_price) > max(
            Decimal("0.00000001"), local.entry_price * Decimal("0.0005")
        ):
            failures.append(f"entry_mismatch:{broker_id}")
        raw_amount = broker_position.get("amount")
        if raw_amount is not None:
            broker_notional = abs(Decimal(str(raw_amount)))
            local_notional = local.quantity * local.entry_price
            if not broker_notional.is_finite() or abs(broker_notional - local_notional) > max(
                Decimal("0.01"), local_notional * Decimal("0.02")
            ):
                failures.append(f"exposure_mismatch:{broker_id}")
        for raw_name, local_value, label in (
            ("fees", local.fees_accrued, "fees"),
            ("financing", local.financing_accrued, "financing"),
        ):
            if broker_position.get(raw_name) is None:
                continue
            broker_value = abs(Decimal(str(broker_position[raw_name])))
            if not broker_value.is_finite() or abs(broker_value - local_value) > Decimal("0.01"):
                failures.append(f"{label}_mismatch:{broker_id}")

    local_pending = store.broker_orders_by_status(
        (
            OrderStatus.ACKNOWLEDGED.value,
            OrderStatus.PARTIALLY_FILLED.value,
            OrderStatus.UNKNOWN.value,
        )
    )
    broker_pending_tokens: set[str] = set()
    for broker_order in (*open_orders, *pending_orders):
        if not isinstance(broker_order, Mapping):
            continue
        for name in (
            "orderID",
            "orderId",
            "referenceID",
            "referenceId",
            "requestID",
            "requestId",
        ):
            value = str(broker_order.get(name, "")).strip()
            if value:
                broker_pending_tokens.add(value)
    local_pending_tokens: set[str] = set()
    for local_order in local_pending:
        candidates = {
            str(local_order.broker_order_id or "").strip(),
            str(local_order.client_order_id or "").strip(),
        }
        candidates.discard("")
        local_pending_tokens.update(candidates)
        if not candidates & broker_pending_tokens:
            failures.append(f"pending_order_unresolved:{local_order.order_command_id}")
    if broker_pending_tokens - local_pending_tokens:
        failures.append("unbound_broker_pending_order")
    reconciliation_ok = not failures
    return BrokerTruth(
        equity_usd=equity,
        peak_equity_usd=peak,
        available_cash_usd=cash,
        gross_exposure_usd=gross,
        correlated_exposure_usd=gross,
        open_positions=len(positions),
        pending_order_notional_usd=pending_notional,
        daily_pnl_usd=daily_pnl,
        weekly_pnl_usd=weekly_pnl,
        monthly_pnl_usd=monthly_pnl,
        snapshot_hash=snapshot.snapshot_hash,
        observed_at=snapshot.observed_at,
        last_trade_at=last_trade_at,
        reconciliation_ok=reconciliation_ok,
        reconciliation_detail=tuple(sorted(set(failures))),
    )


class DecisionApplyWorkerV2:
    def __init__(self, config_path: str, *, shadow_only: bool = False) -> None:
        self.config = load_config_v2(config_path)
        self.shadow_only = shadow_only
        if shadow_only and self.config.live_demo_execution_enabled:
            raise PermissionError("shadow decision worker requires execution-disabled config")
        if not shadow_only and (
            not self.config.live_demo_execution_enabled
            or not execution_gate_present(EXECUTION_GATE)
        ):
            raise PermissionError("execution decision worker requires the explicit DEMO gate")
        self.store = PostgresRuntimeStoreV2.from_dsn(_dsn(config_path))
        self.store.require_schema()
        self.queue = CanonicalPostgresAIStoreV2(self.store)
        self.kernel: UnifiedTradingKernel | None = None
        self.applier: DecisionApplierV2 | None = None
        self.client: EtoroPublicApiDemoClientV2 | None = None
        if shadow_only:
            return
        signer_socket = os.getenv("ETORO_V2_RISK_SIGNER_SOCKET", "")
        verifying_key_path = os.getenv("ETORO_V2_RISK_VERIFYING_KEY_FILE", "")
        if not signer_socket or not verifying_key_path:
            raise RuntimeError("v2 isolated signer socket and public verifying key are required")
        self.kernel = UnifiedTradingKernel(
            self.store,
            GlobalRiskKernel(self.config.mandate),
            command_signer=SocketRiskCommandSignerV2.from_public_key_file(
                signer_socket,
                verifying_key_path,
                expected_risk_config_hash=risk_mandate_hash(self.config.mandate),
            ),
        )
        self.applier = DecisionApplierV2(
            self.kernel,
            portfolio_id=self.config.portfolio_id,
            model_id=self.config.model_id,
        )
        self.client = EtoroPublicApiDemoClientV2()
        self.client.verify_isolated_demo_read_scope()

    def close(self) -> None:
        self.store.close()

    def _authority(self) -> tuple[str, int | None] | None:
        snapshot = self.store.trading_state_snapshot()
        return authority_for_state(
            str(snapshot["state"]),
            int(snapshot["version"]),
            execution_gate=execution_gate_present(EXECUTION_GATE),
        )

    def _run_once(self, limit: int = 20) -> int:
        if self.shadow_only and execution_gate_present(EXECUTION_GATE):
            raise PermissionError("shadow decision worker refuses an active execution gate")
        if not self.shadow_only and not execution_gate_present(EXECUTION_GATE):
            self.store.lock_and_invalidate_unstarted(
                actor="v2-decision-apply",
                reason="execution gate absent at decision iteration start",
            )
            return 0
        authority = self._authority()
        expected_mode = "SHADOW" if self.shadow_only else "EXECUTION"
        if authority is None or authority[0] != expected_mode:
            return 0
        authority_mode, execution_epoch = authority
        applied = 0
        for _ in range(max(1, min(limit, 100))):
            now = datetime.now(UTC)
            row = self.queue.claim_decided(
                "v2-decision-apply",
                AIRole.PORTFOLIO_DECIDER,
                now=now,
                authority_mode=authority_mode,
                execution_epoch=execution_epoch,
            )
            if row is None:
                break
            packet_id = str(row["packet_id"])
            claim_token = str(row["apply_claim_token"])
            try:
                if not self.shadow_only and not execution_gate_present(EXECUTION_GATE):
                    self.store.lock_and_invalidate_unstarted(
                        actor="v2-decision-apply",
                        reason="execution gate absent after decision claim",
                    )
                    self.queue.release_apply_claim(
                        packet_id,
                        claim_token,
                        now=datetime.now(UTC),
                    )
                    break
                row_authority = (
                    str(row["authority_mode"]),
                    None if row["execution_epoch"] is None else int(row["execution_epoch"]),
                )
                if self._authority() != row_authority:
                    self.queue.release_apply_claim(
                        packet_id,
                        claim_token,
                        now=datetime.now(UTC),
                        reason="authority_epoch_changed",
                    )
                    break
                packet = decode_dataclass(DecisionPacketV2, row["packet"])
                if (
                    packet.packet_id != packet_id
                    or packet.lane != str(row["lane"])
                    or packet.packet_hash != str(row["packet_hash"])
                ):
                    raise ValueError("claimed decision packet identity or hash is invalid")
                output = decode_dataclass(AIIntentOutputV2, row["output"])
                output.validate(packet)
                if self.shadow_only:
                    effect = _shadow_effect(packet, output)
                elif output.action.value == "HOLD":
                    if self.applier is None:
                        raise RuntimeError("deterministic decision applier is unavailable")
                    result = self.applier.apply_hold(packet, output, now=now)
                    effect = {
                        "action": result.action,
                        "applied": result.applied,
                        **dict(result.effect),
                    }
                else:
                    if self.client is None or self.applier is None:
                        raise RuntimeError("execution decision dependencies are unavailable")
                    symbol = output.symbol
                    if output.action.value == "OPEN":
                        engine = canonical_candidate_engine()
                        try:
                            strategy_release = require_deployed_strategy_release(
                                engine,
                                expected_feature_schema_hash=engine.feature_schema_hash,
                                expected_cost_model_hash=engine.cost_model_hash,
                                now=now,
                            )
                        except PermissionError as exc:
                            effect = {
                                "action": "OPEN",
                                "applied": False,
                                "status": "rejected",
                                "reason": "strategy_release_unavailable",
                                "error_type": type(exc).__name__,
                            }
                            self.queue.mark_applied(
                                packet_id,
                                claim_token,
                                effect,
                                now=datetime.now(UTC),
                            )
                            applied += 1
                            continue
                        candidate = output.selected_candidate(packet)
                        if candidate is not None:
                            symbol = str(candidate.get("symbol", ""))
                            execution_plan = candidate.get("execution_plan")
                            candidate_release = (
                                execution_plan.get("strategy_release_id")
                                if isinstance(execution_plan, Mapping)
                                else None
                            )
                            candidate_release_hash = (
                                execution_plan.get("strategy_release_manifest_hash")
                                if isinstance(execution_plan, Mapping)
                                else None
                            )
                            if (
                                candidate_release != strategy_release.strategy_release_id
                                or candidate_release_hash != strategy_release.manifest_hash
                            ):
                                raise PermissionError("candidate strategy release mismatch")
                    if symbol is None and packet.position is not None:
                        symbol = str(packet.position.get("symbol", ""))
                    if not symbol or symbol not in self.config.symbols:
                        effect = {"status": "rejected", "reason": "symbol_unavailable"}
                    else:
                        truth = _broker_truth(
                            self.store,
                            self.client,
                            config=self.config,
                            now=now,
                        )
                        quote = _quote(
                            self.client,
                            symbol=symbol,
                            instrument_id=self.config.symbols[symbol],
                            broker_hash=truth.snapshot_hash,
                            received_at=now,
                        )
                        result = self.applier.apply(
                            packet,
                            output,
                            quote=quote,
                            broker=truth,
                            now=now,
                            execution_epoch=(
                                None
                                if row["execution_epoch"] is None
                                else int(row["execution_epoch"])
                            ),
                        )
                        effect = {
                            "action": result.action,
                            "applied": result.applied,
                            **dict(result.effect),
                        }
                self.queue.mark_applied(
                    packet_id,
                    claim_token,
                    effect,
                    now=datetime.now(UTC),
                )
                applied += 1
            except Exception:
                self.queue.release_apply_claim(
                    packet_id,
                    claim_token,
                    now=datetime.now(UTC),
                )
                raise
        return applied

    def run_once(self, limit: int = 20) -> int:
        service = "v2-decision-shadow" if self.shadow_only else "v2-decision-apply"
        try:
            applied = self._run_once(limit)
        except Exception as exc:
            self.store.heartbeat(
                service,
                "error",
                {"error_type": type(exc).__name__, "real_money": False},
            )
            raise
        trading_state = self.store.state_get("trading_state", "LOCKED")
        self.store.heartbeat(
            service,
            "healthy" if trading_state == "ACTIVE" else "halted",
            {
                "decisions_applied": applied,
                "shadow_only": self.shadow_only,
                "trading_state": trading_state,
                "real_money": False,
            },
        )
        return applied

    def run_forever(self, interval_seconds: int = 5) -> None:
        if interval_seconds < 1:
            raise ValueError("decision apply interval must be positive")
        ready()
        while True:
            try:
                self.run_once()
                watchdog()
            except Exception as exc:
                if self.shadow_only and execution_gate_present(EXECUTION_GATE):
                    raise
                print(
                    f"V2_DECISION_APPLY_ERROR={type(exc).__name__}",
                    flush=True,
                )
            time.sleep(interval_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Apply validated v2 AI decisions through deterministic kernel"
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--interval", type=int, default=5)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--shadow-only", action="store_true")
    args = parser.parse_args()
    worker = DecisionApplyWorkerV2(args.config, shadow_only=args.shadow_only)
    try:
        if args.once:
            print(f"V2_AI_APPLIED={worker.run_once()}")
        else:
            worker.run_forever(args.interval)
    finally:
        worker.close()


if __name__ == "__main__":
    main()
