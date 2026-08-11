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
from .codec_v2 import decode_dataclass
from .config_v2 import load_config_v2
from .decision_v2 import DecisionApplierV2
from .domain_v2 import QuoteProvenance
from .etoro_api_current_v2 import EtoroPublicApiDemoClientV2
from .kernel_v2 import UnifiedTradingKernel
from .postgres_runtime_v2 import PostgresRuntimeStoreV2
from .risk_seal_v2 import risk_mandate_hash
from .risk_signer_ipc_v2 import SocketRiskCommandSignerV2
from .risk_v2 import BrokerTruth, GlobalRiskKernel
from .systemd_notify_v2 import ready, watchdog

EXECUTION_GATE = Path("/etc/etoro-agent/ENABLE_V2_DEMO_EXECUTION")


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
    now: datetime,
) -> BrokerTruth:
    response = client.demo_pnl()
    if not response.ok or not isinstance(response.body, dict):
        raise RuntimeError("DEMO P&L snapshot unavailable")
    portfolio = response.body.get("clientPortfolio", response.body)
    if not isinstance(portfolio, Mapping):
        raise RuntimeError("DEMO portfolio shape invalid")
    credit = Decimal(str(portfolio.get("credit", "0")))
    positions = portfolio.get("positions", [])
    if not isinstance(positions, list):
        raise RuntimeError("DEMO positions shape invalid")
    open_orders = portfolio.get("ordersForOpen", [])
    pending_orders = portfolio.get("orders", [])
    if not isinstance(open_orders, list) or not isinstance(pending_orders, list):
        raise RuntimeError("DEMO pending order collections are invalid")
    gross = Decimal("0")
    unrealized = Decimal("0")
    invested = Decimal("0")
    for position in positions:
        if not isinstance(position, Mapping):
            continue
        pnl = position.get("unrealizedPnL") or {}
        if not isinstance(pnl, Mapping):
            pnl = {}
        gross += abs(Decimal(str(pnl.get("exposureInAccountCurrency", position.get("amount", 0)))))
        unrealized += Decimal(str(pnl.get("pnL", 0)))
        invested += Decimal(str(position.get("amount", 0)))
    equity = credit + invested + unrealized
    if equity <= 0:
        raise RuntimeError("DEMO broker equity is invalid")
    cash = client.cash_truth().available_cash_usd
    pending_notional = sum(
        (
            abs(Decimal(str(order.get("amount", order.get("exposure", 0)))))
            for order in (*open_orders, *pending_orders)
            if isinstance(order, Mapping)
        ),
        Decimal("0"),
    )
    peak_raw = store.state_get("broker_peak_equity_v2", str(equity))
    peak = max(equity, Decimal(peak_raw))
    store.state_set("broker_peak_equity_v2", str(peak))
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
    broker_position_ids = {
        str(position.get("positionID", position.get("positionId", "")))
        for position in positions
        if isinstance(position, Mapping)
        and str(position.get("positionID", position.get("positionId", ""))).strip()
    }
    local_position_ids = {
        str(position.broker_position_id)
        for position in local_positions
        if position.broker_position_id is not None
    }
    reconciliation_ok = (
        len(local_positions) == len(positions)
        and len(local_position_ids) == len(local_positions)
        and local_position_ids == broker_position_ids
    )
    canonical = json.dumps(portfolio, sort_keys=True, separators=(",", ":"), default=str)
    snapshot_hash = hashlib.sha256(canonical.encode()).hexdigest()
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
        snapshot_hash=snapshot_hash,
        observed_at=now,
        last_trade_at=last_trade_at,
        reconciliation_ok=reconciliation_ok,
    )


class DecisionApplyWorkerV2:
    def __init__(self, config_path: str, *, shadow_only: bool = False) -> None:
        self.config = load_config_v2(config_path)
        self.shadow_only = shadow_only
        if shadow_only and self.config.live_demo_execution_enabled:
            raise PermissionError("shadow decision worker requires execution-disabled config")
        if not shadow_only and (
            not self.config.live_demo_execution_enabled or not EXECUTION_GATE.is_file()
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
        self.kernel = UnifiedTradingKernel(  # type: ignore[arg-type]
            self.store,
            GlobalRiskKernel(self.config.mandate),
            command_signer=SocketRiskCommandSignerV2.from_public_key_file(
                signer_socket,
                verifying_key_path,
                expected_risk_config_hash=risk_mandate_hash(self.config.mandate),
            ),
        )
        self.applier = DecisionApplierV2(self.kernel)
        self.client = EtoroPublicApiDemoClientV2()
        self.client.verify_isolated_demo_read_scope()

    def close(self) -> None:
        self.store.close()

    def _run_once(self, limit: int = 20) -> int:
        if self.shadow_only and EXECUTION_GATE.exists():
            raise PermissionError("shadow decision worker refuses an active execution gate")
        applied = 0
        for _ in range(max(1, min(limit, 100))):
            now = datetime.now(UTC)
            row = self.queue.claim_decided(
                "v2-decision-apply",
                AIRole.PORTFOLIO_DECIDER,
                now=now,
            )
            if row is None:
                break
            packet_id = str(row["packet_id"])
            claim_token = str(row["apply_claim_token"])
            try:
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
                    effect: Mapping[str, Any] = {"status": "no_new_risk"}
                else:
                    if self.client is None or self.applier is None:
                        raise RuntimeError("execution decision dependencies are unavailable")
                    symbol = output.symbol
                    if output.action.value == "OPEN":
                        candidate = output.selected_candidate(packet)
                        if candidate is not None:
                            symbol = str(candidate.get("symbol", ""))
                    if symbol is None and packet.position is not None:
                        symbol = str(packet.position.get("symbol", ""))
                    if not symbol or symbol not in self.config.symbols:
                        effect = {"status": "rejected", "reason": "symbol_unavailable"}
                    else:
                        truth = _broker_truth(
                            self.store,
                            self.client,
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
                if self.shadow_only and EXECUTION_GATE.exists():
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
