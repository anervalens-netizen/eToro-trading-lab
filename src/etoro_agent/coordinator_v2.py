from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import time
from collections.abc import Mapping
from dataclasses import asdict, replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .ai_store_postgres_v2 import CanonicalPostgresAIStoreV2
from .ai_v2 import AIRole, Lane
from .candidates_v2 import (
    PROVISIONAL_ROUND_TRIP_COST_BPS,
    expected_payoff_bps,
    generate_core_signals,
)
from .config_v2 import load_config_v2
from .decision_v2 import DecisionPacketBuilderV2, DecisionPacketContextV2
from .etoro_api_current_v2 import EtoroPublicApiDemoClientV2
from .execution_gate_v2 import authority_for_state, execution_gate_present
from .features_v2 import build_feature_snapshot
from .market import MarketDataCollector
from .mcp import EtoroMCPClient
from .postgres_runtime_v2 import PostgresRuntimeStoreV2
from .risk_seal_v2 import risk_mandate_hash
from .systemd_notify_v2 import ready, watchdog


def validate_snapshot_batch(
    snapshots: Mapping[str, Any],
    expected_symbols: frozenset[str],
    *,
    max_capture_skew_seconds: int = 20,
    max_quote_skew_seconds: int = 30,
) -> tuple[bool, str]:
    if frozenset(snapshots) != expected_symbols:
        return False, "incomplete_symbol_batch"
    capture_times = [item.captured_at for item in snapshots.values()]
    if (max(capture_times) - min(capture_times)).total_seconds() > max_capture_skew_seconds:
        return False, "capture_time_skew"
    open_snapshots = [item for item in snapshots.values() if item.market_open]
    if open_snapshots:
        quote_times = [item.quote_observed_at for item in open_snapshots]
        if (max(quote_times) - min(quote_times)).total_seconds() > max_quote_skew_seconds:
            return False, "quote_time_skew"
    for left, right in (("SPX500", "NSDQ100"),):
        if left not in snapshots or right not in snapshots:
            continue
        first = snapshots[left]
        second = snapshots[right]
        if first.market_open and second.market_open:
            if not first.candles or not second.candles:
                return False, "correlated_closed_bar_missing"
            if first.candles[-1].timestamp != second.candles[-1].timestamp:
                return False, "correlated_closed_bar_misaligned"
    return True, "aligned"


def coordinator_cycle_allowed(trading_state: str, *, execution_gate: bool) -> bool:
    """Allow only canonical LOCKED shadow or ACTIVE execution cycles."""

    return (trading_state == "LOCKED" and not execution_gate) or (
        trading_state == "ACTIVE" and execution_gate
    )


class AutonomousCoordinatorV2:
    """Closed-bar event coordinator for the v2 autonomous decision loop.

    It never writes broker orders. It creates immutable evidence, deterministic candidates,
    and stateless model packets. The downstream DecisionApplyWorkerV2 and risk kernel are
    the only bridge from a validated AI output to an execution command.
    """

    def __init__(self, config_path: str) -> None:
        self.config = load_config_v2(config_path)
        dsn_path = os.getenv("ETORO_V2_POSTGRES_DSN_FILE") or self.config.postgres_dsn_file
        if not dsn_path:
            raise RuntimeError("PostgreSQL DSN credential file is required")
        dsn = Path(dsn_path).read_text(encoding="utf-8").strip()
        if not dsn:
            raise RuntimeError("PostgreSQL DSN credential file is empty")
        self.store = PostgresRuntimeStoreV2.from_dsn(dsn)
        self.store.require_schema()
        self.ai = CanonicalPostgresAIStoreV2(self.store)
        EtoroMCPClient().verify_isolated_demo_read_scope()
        self.broker = EtoroPublicApiDemoClientV2()
        self.broker.verify_isolated_demo_read_scope()
        self.builder = DecisionPacketBuilderV2()
        self.master_lane = Lane(os.getenv("ETORO_V2_MASTER_LANE", Lane.SOL_CRITIC.value))
        self.role_research_enabled = os.getenv("ETORO_V2_ROLE_RESEARCH", "1") != "0"
        self.candle_interval = os.getenv("ETORO_V2_CANDLE_INTERVAL", "FifteenMinutes")
        self.candle_count = max(100, min(int(os.getenv("ETORO_V2_CANDLE_COUNT", "500")), 1000))
        self.compatibility = self.config.compatibility()
        self.execution_symbols = {
            symbol: instrument_id
            for symbol, instrument_id in self.config.symbols.items()
            if symbol in self.config.mandate.allowed_symbols
        }

    def close(self) -> None:
        self.store.close()

    @staticmethod
    def _returns(closes: tuple[Decimal, ...], horizon: int) -> Decimal:
        if len(closes) <= horizon or closes[-horizon - 1] <= 0:
            return Decimal("0")
        return closes[-1] / closes[-horizon - 1] - Decimal("1")

    @staticmethod
    def _volatility(closes: tuple[Decimal, ...], lookback: int = 20) -> Decimal:
        if len(closes) <= lookback:
            return Decimal("0")
        returns = [
            closes[index] / closes[index - 1] - Decimal("1")
            for index in range(len(closes) - lookback, len(closes))
            if closes[index - 1] > 0
        ]
        if not returns:
            return Decimal("0")
        average = sum(returns, Decimal("0")) / Decimal(len(returns))
        return (
            sum(((value - average) ** 2 for value in returns), Decimal("0")) / Decimal(len(returns))
        ).sqrt()

    def _role_memory(self, prefix: str) -> Mapping[str, Any] | None:
        raw = self.store.state_get(f"{prefix}:{self.master_lane.value}", "")
        if not raw:
            return None
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, Mapping) else None

    def _portfolio_context(self) -> tuple[str, Mapping[str, Any]]:
        cash = self.broker.cash_truth()
        local_positions = self.store.positions("master_1000", open_only=True)
        pnl = self.broker.demo_pnl()
        broker_portfolio = (
            pnl.body.get("clientPortfolio", pnl.body)
            if pnl.ok and isinstance(pnl.body, dict)
            else {}
        )
        payload = {
            "initial_cash_usd": str(self.config.initial_cash_usd),
            "available_cash_usd": str(cash.available_cash_usd),
            "open_local_positions": len(local_positions),
            "broker_position_count": len(broker_portfolio.get("positions", []))
            if isinstance(broker_portfolio, Mapping)
            and isinstance(broker_portfolio.get("positions", []), list)
            else None,
            "risk_limits": asdict(self.config.mandate),
            "allowed_symbols": sorted(self.config.mandate.allowed_symbols),
            "provisional_round_trip_cost_bps": {
                key: str(value) for key, value in PROVISIONAL_ROUND_TRIP_COST_BPS.items()
            },
            "trading_state": self.store.state_get("trading_state", "LOCKED"),
            "real_money": False,
        }
        return cash.snapshot_hash, payload

    def _risk_hash(self) -> str:
        return risk_mandate_hash(self.config.mandate)

    def _execution_plan(self, signal: Any) -> Mapping[str, Any] | None:
        matches = [
            item
            for item in self.compatibility
            if item.strategy_id == signal.family.value
            and item.symbol == signal.symbol
            and item.status.value == "EXECUTABLE"
        ]
        if len(matches) != 1:
            return None
        match = matches[0]
        if (
            match.feasible_amount_min_usd is None
            or match.feasible_stop_min is None
            or match.feasible_stop_max is None
            or not match.feasible_stop_min <= signal.stop_fraction <= match.feasible_stop_max
        ):
            return None
        amount = match.feasible_amount_min_usd
        slippage = min(Decimal("15"), self.config.mandate.max_mid_drift_bps)
        projected_loss = amount * (signal.stop_fraction + slippage / Decimal("10000"))
        if projected_loss > self.config.mandate.max_trade_risk_usd:
            return None
        return {
            "amount_usd": str(amount),
            "max_slippage_bps": str(slippage),
            "sizing_rule": "minimum_broker_compatible_notional_v1",
        }

    def _queue_role_packets(
        self,
        base_packet: object,
        *,
        authority_mode: str,
        execution_epoch: int | None,
    ) -> int:
        from .ai_v2 import DecisionPacketV2

        if not isinstance(base_packet, DecisionPacketV2):
            raise TypeError("coordinator packet must use the v2 decision contract")
        count = 0
        if self.role_research_enabled:
            for role, suffix in (
                (AIRole.MARKET_REGIME_ANALYST, "regime"),
                (AIRole.ADVERSARIAL_CRITIC, "critic"),
            ):
                role_packet = replace(base_packet, packet_id=f"{base_packet.packet_id}-{suffix}")
                count += int(
                    self.ai.queue(
                        role_packet,
                        role,
                        authority_mode=authority_mode,
                        execution_epoch=execution_epoch,
                    )
                )
        if base_packet.lane != Lane.SOL_CRITIC.value:
            count += int(
                self.ai.queue(
                    base_packet,
                    AIRole.PORTFOLIO_DECIDER,
                    authority_mode=authority_mode,
                    execution_epoch=execution_epoch,
                )
            )
        return count

    def _collect_snapshot(self, symbol: str, instrument_id: int) -> Any:
        return MarketDataCollector(EtoroMCPClient()).collect(
            symbol,
            instrument_id,
            self.candle_interval,
            self.candle_count,
            close_grace_seconds=60,
        )

    def _collect_aligned_batch(self) -> dict[str, Any]:
        snapshots: dict[str, Any] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            futures = {
                pool.submit(self._collect_snapshot, symbol, instrument_id): symbol
                for symbol, instrument_id in self.execution_symbols.items()
            }
            for future in concurrent.futures.as_completed(futures):
                symbol = futures[future]
                try:
                    snapshots[symbol] = future.result()
                except Exception as exc:
                    print(
                        f"V2_MARKET_COLLECTION_ERROR={symbol}:{type(exc).__name__}",
                        flush=True,
                    )
        aligned, reason = validate_snapshot_batch(
            snapshots,
            frozenset(self.execution_symbols),
            max_quote_skew_seconds=self.config.mandate.max_quote_age_seconds,
        )
        if not aligned:
            print(f"V2_MARKET_BATCH_REJECTED={reason}", flush=True)
            raise RuntimeError(f"market snapshot batch rejected: {reason}")
        return snapshots

    def _run_once(self) -> int:
        state_snapshot = self.store.trading_state_snapshot()
        authority = authority_for_state(
            str(state_snapshot["state"]),
            int(state_snapshot["version"]),
            execution_gate=execution_gate_present(),
        )
        if authority is None:
            return 0
        authority_mode, execution_epoch = authority
        unresolved = self.store.broker_orders_by_status(
            (
                "RISK_APPROVED",
                "SUBMITTING",
                "ACKNOWLEDGED",
                "PARTIALLY_FILLED",
                "UNKNOWN",
            )
        )
        if unresolved:
            return 0
        snapshots = self._collect_aligned_batch()
        if not snapshots:
            return 0
        broker_hash, portfolio_context = self._portfolio_context()
        risk_hash = self._risk_hash()
        open_positions = self.store.positions("master_1000", open_only=True)
        if len(open_positions) > 1:
            self.store.set_trading_state(
                "HALT_NEW",
                actor="v2-coordinator",
                reason="broker truth or decision coordination failed",
            )
            return 0

        selected_symbol = ""
        selected_snapshot = None
        selected_signals: tuple[Any, ...] = ()
        mode = "ENTRY_REVIEW"
        position = open_positions[0] if open_positions else None
        if position is not None:
            selected_symbol = position.symbol
            selected_snapshot = snapshots.get(position.symbol)
            if selected_snapshot is None:
                return 0
            mode = "POSITION_REVIEW"
        else:
            ranked: list[tuple[Decimal, str, Any, tuple[Any, ...]]] = []
            for symbol, snapshot in snapshots.items():
                if not snapshot.market_open or (
                    snapshot.quality is not None and not snapshot.quality.is_valid
                ):
                    continue
                highs = tuple(candle.high for candle in snapshot.candles)
                lows = tuple(candle.low for candle in snapshot.candles)
                signals = tuple(
                    signal
                    for signal in generate_core_signals(symbol, snapshot.closes, highs, lows)
                    if self._execution_plan(signal) is not None
                )
                if not signals:
                    continue
                score = max(
                    expected_payoff_bps(signal)
                    - PROVISIONAL_ROUND_TRIP_COST_BPS.get(symbol, Decimal("100")) * Decimal("1.5")
                    for signal in signals
                )
                ranked.append((score, symbol, snapshot, signals))
            if not ranked:
                return 0
            _, selected_symbol, selected_snapshot, selected_signals = max(
                ranked, key=lambda item: (item[0], item[1])
            )

        if selected_snapshot is None:
            raise RuntimeError("coordinator selected no market snapshot")
        if not selected_snapshot.candles:
            return 0
        last_bar = selected_snapshot.candles[-1]
        bar_key = f"v2_coordinator_bar:{mode}:{selected_symbol}"
        bar_fingerprint = hashlib.sha256(
            f"{selected_symbol}:{last_bar.timestamp.isoformat()}:{last_bar.close}:{mode}".encode()
        ).hexdigest()
        if self.store.state_get(bar_key, "") == bar_fingerprint:
            return 0
        source_ids = (selected_snapshot.content_hash, bar_fingerprint)
        mid = (selected_snapshot.bid + selected_snapshot.ask) / Decimal("2")
        feature = build_feature_snapshot(
            selected_symbol,
            last_bar.timestamp,
            {
                "return_1": self._returns(selected_snapshot.closes, 1),
                "return_4": self._returns(selected_snapshot.closes, 4),
                "return_16": self._returns(selected_snapshot.closes, 16),
                "volatility_20": self._volatility(selected_snapshot.closes, 20),
                "spread_bps": (selected_snapshot.ask - selected_snapshot.bid)
                / mid
                * Decimal("10000"),
            },
            source_ids,
            feature_version="closed-bar-features-v2.1",
            data_quality_ok=bool(
                selected_snapshot.quality is None or selected_snapshot.quality.is_valid
            ),
        )
        context = DecisionPacketContextV2(
            broker_snapshot_hash=broker_hash,
            risk_config_hash=risk_hash,
            portfolio=portfolio_context,
            regime=self._role_memory("latest_regime_v2"),
            critic=self._role_memory("latest_critic_v2"),
        )
        execution_plans: dict[str, Mapping[str, Any]] = {}
        for signal in selected_signals:
            plan = self._execution_plan(signal)
            if plan is not None:
                execution_plans[self.builder.signal_key(signal)] = plan
        packet = self.builder.build(
            lane=self.master_lane,
            mode=mode,
            feature=feature,
            market_snapshot_ids=(selected_snapshot.content_hash,),
            signals=selected_signals,
            context=context,
            position=position,
            created_at=datetime.now(UTC),
            ttl_seconds=300,
            execution_plans=execution_plans,
        )
        queued = self._queue_role_packets(
            packet,
            authority_mode=authority_mode,
            execution_epoch=execution_epoch,
        )
        if queued:
            self.store.state_set(bar_key, bar_fingerprint)
        return queued

    def run_once(self) -> int:
        try:
            queued = self._run_once()
        except Exception as exc:
            self.store.heartbeat(
                "v2-coordinator",
                "error",
                {"error_type": type(exc).__name__, "real_money": False},
            )
            raise
        trading_state = self.store.state_get("trading_state", "LOCKED")
        self.store.heartbeat(
            "v2-coordinator",
            "healthy" if trading_state == "ACTIVE" else "halted",
            {
                "packets_queued": queued,
                "trading_state": trading_state,
                "real_money": False,
            },
        )
        return queued

    def run_forever(self, interval_seconds: int = 60) -> None:
        if interval_seconds < 30:
            raise ValueError("coordinator interval must be at least 30 seconds")
        ready()
        while True:
            try:
                self.run_once()
                watchdog()
            except Exception as exc:
                print(f"V2_COORDINATOR_ERROR={type(exc).__name__}", flush=True)
            time.sleep(interval_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description="Autonomous closed-bar v2 trading coordinator")
    parser.add_argument("--config", required=True)
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    worker = AutonomousCoordinatorV2(args.config)
    try:
        if args.once:
            print(f"V2_AI_PACKETS_QUEUED={worker.run_once()}")
        else:
            worker.run_forever(args.interval)
    finally:
        worker.close()


if __name__ == "__main__":
    main()
