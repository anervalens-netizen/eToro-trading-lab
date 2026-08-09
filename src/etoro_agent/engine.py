from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Mapping

from .audit import AuditLog
from .config import AppConfig
from .market import MarketDataCollector, MarketSnapshot
from .models import KillState, RiskContext, Side
from .nautilus_runtime import NautilusReplayClock
from .portfolio import SHADOW_PORTFOLIO_IDS, ShadowPortfolioLedger
from .risk import DeterministicRiskEngine
from .strategy import StrategyContext, build_strategy_suite


STRATEGY_SYMBOLS: tuple[str, ...] = (
    "SPX500",
    "NSDQ100",
    "SPX500",
    "BTC",
    "AAPL",
    "ETH",
    "AAPL",
    "TSLA",
    "EURUSD",
    "EURUSD",
    "SPX500",
    "EURUSD",
)


@dataclass(frozen=True)
class ShadowTickResult:
    generated_at: str
    market_event_hash: str
    strategy_results: tuple[dict[str, object], ...]


class AutonomousShadowEngine:
    """Fully autonomous shadow runtime; it has no eToro write capability."""

    def __init__(self, config: AppConfig, audit: AuditLog) -> None:
        self.config = config
        self.audit = audit
        self.runtime_dir = audit.path.parent
        self.strategies = build_strategy_suite(config.strategy)
        if len(self.strategies) != 12 or len(STRATEGY_SYMBOLS) != 12:
            raise RuntimeError("shadow engine requires exactly twelve strategies")
        self.ledger = ShadowPortfolioLedger(
            audit,
            initial_cash_usd=config.initial_cash_usd,
            reporting_timezone=config.report_timezone,
        )
        self.risk = DeterministicRiskEngine(config.risk)
        self.clock = NautilusReplayClock()

    def _position(self, portfolio_id: str) -> tuple[str, Decimal, Decimal] | None:
        row = self.audit.db.execute(
            """
            SELECT symbol,units,average_price FROM shadow_positions
            WHERE portfolio_id=? AND units!='0' LIMIT 1
            """,
            (portfolio_id,),
        ).fetchone()
        return None if row is None else (str(row[0]), Decimal(row[1]), Decimal(row[2]))

    def _period_pnl(self, portfolio_id: str, days: int) -> Decimal:
        rows = self.audit.db.execute(
            """
            SELECT daily_pnl_usd FROM shadow_daily_pnl
            WHERE portfolio_id=? ORDER BY day DESC LIMIT ?
            """,
            (portfolio_id, days),
        ).fetchall()
        return sum((Decimal(row[0]) for row in rows), Decimal("0"))

    @staticmethod
    def _context(snapshot: MarketSnapshot, related: MarketSnapshot | None = None) -> StrategyContext:
        return StrategyContext(
            symbol=snapshot.symbol,
            closes=snapshot.closes,
            highs=tuple(candle.high for candle in snapshot.candles),
            lows=tuple(candle.low for candle in snapshot.candles),
            timestamps=tuple(candle.timestamp for candle in snapshot.candles),
            related_closes={} if related is None else {related.symbol: related.closes},
        )

    def tick(self, snapshots: Mapping[str, MarketSnapshot]) -> ShadowTickResult:
        required = set(STRATEGY_SYMBOLS) | {"NSDQ100"}
        missing = required - {key.upper() for key in snapshots}
        if missing:
            raise ValueError(f"shadow tick missing market snapshots: {','.join(sorted(missing))}")
        normalized = {key.upper(): value for key, value in snapshots.items()}
        observed_at = max(
            snapshot.captured_at or datetime.now(timezone.utc)
            for snapshot in normalized.values()
        )
        combined_hash = "".join(
            normalized[symbol].content_hash for symbol in sorted(normalized)
        )
        event = self.clock.observe(observed_at, "market_batch", combined_hash)
        self.audit.append(
            "nautilus_market_batch",
            {
                "event_hash": event.event_hash,
                "timestamp_ns": event.timestamp_ns,
                "snapshot_hashes": {
                    symbol: snapshot.content_hash for symbol, snapshot in normalized.items()
                },
            },
        )
        results: list[dict[str, object]] = []
        for index, (strategy, symbol, portfolio_id) in enumerate(
            zip(self.strategies, STRATEGY_SYMBOLS, SHADOW_PORTFOLIO_IDS, strict=True)
        ):
            snapshot = normalized[symbol]
            related = normalized["NSDQ100"] if index == 10 else None
            intent = strategy.decide_context(self._context(snapshot, related))
            position = self._position(portfolio_id)
            marks = {symbol: snapshot.bid}
            state = self.ledger.snapshot(portfolio_id, marks, as_of=observed_at)
            status = "hold"
            last_signal: str | None = None
            reasons: tuple[str, ...] = ()
            if intent is not None:
                last_signal = intent.side.value
                self.audit.append("trade_intent", asdict(intent))
                if position is not None:
                    position_symbol, units, _ = position
                    opposing = (units > 0 and intent.side is Side.SELL) or (
                        units < 0 and intent.side is Side.BUY
                    )
                    if opposing:
                        close_side = "sell" if units > 0 else "buy"
                        close_price = snapshot.bid if units > 0 else snapshot.ask
                        close_notional = abs(units) * close_price
                        fee = close_notional * Decimal("0.001")
                        self.ledger.record_fill(
                            portfolio_id,
                            position_symbol,
                            close_side,
                            abs(units),
                            close_price,
                            fee_usd=fee,
                            executed_at=observed_at,
                        )
                        status = "shadow_reduced"
                        self.audit.append(
                            "shadow_reduce_only",
                            {
                                "strategy_id": strategy.strategy_id,
                                "portfolio_id": portfolio_id,
                                "symbol": position_symbol,
                                "reason": "opposing_signal",
                            },
                        )
                    else:
                        status = "position_open"
                else:
                    risk_context = RiskContext(
                        equity_usd=state.equity_usd,
                        peak_equity_usd=state.peak_equity_usd,
                        daily_pnl_usd=state.daily_pnl_usd,
                        gross_exposure_usd=state.gross_exposure_usd,
                        symbol_exposure_usd=Decimal("0"),
                        trades_today=state.trades_today,
                        bid=snapshot.bid,
                        ask=snapshot.ask,
                        kill_switch_active=(
                            (self.runtime_dir / "KILL_SWITCH").exists()
                            or self.audit.kill_state() is not KillState.ACTIVE
                        ),
                        weekly_pnl_usd=self._period_pnl(portfolio_id, 7),
                        monthly_pnl_usd=self._period_pnl(portfolio_id, 31),
                        correlated_exposure_usd=state.gross_exposure_usd,
                        open_positions=0,
                        quote_observed_at=int(observed_at.timestamp()),
                        evaluated_at=int(observed_at.timestamp()),
                        data_quality_ok=(
                            snapshot.market_open
                            and bool(
                                snapshot.quality is None
                                or snapshot.quality.is_valid
                            )
                        ),
                        audit_writable=True,
                        reconciliation_ok=True,
                    )
                    risk_result = self.risk.evaluate(intent, risk_context)
                    if risk_result.approved and risk_result.order is not None:
                        price = snapshot.ask if intent.side is Side.BUY else snapshot.bid
                        units = intent.amount_usd / price
                        fee = intent.amount_usd * Decimal("0.001")
                        self.ledger.record_fill(
                            portfolio_id,
                            symbol,
                            "buy" if intent.side is Side.BUY else "sell",
                            units,
                            price,
                            fee_usd=fee,
                            executed_at=observed_at,
                        )
                        status = "shadow_filled"
                        self.audit.append(
                            "shadow_risk_approval",
                            {
                                "strategy_id": strategy.strategy_id,
                                "portfolio_id": portfolio_id,
                                "intent_hash": risk_result.order.intent_hash,
                                "risk_snapshot_hash": risk_result.order.risk_snapshot_hash,
                                "risk_config_hash": risk_result.order.risk_config_hash,
                            },
                        )
                    else:
                        status = "risk_rejected"
                        reasons = risk_result.reasons
                        self.audit.append(
                            "risk_rejection",
                            {
                                "strategy_id": strategy.strategy_id,
                                "portfolio_id": portfolio_id,
                                "symbol": symbol,
                                "reasons": reasons,
                            },
                        )
            refreshed = self.ledger.snapshot(portfolio_id, marks, as_of=observed_at)
            drawdown = (
                Decimal("0")
                if refreshed.peak_equity_usd <= 0
                else (refreshed.peak_equity_usd - refreshed.equity_usd)
                / refreshed.peak_equity_usd
            )
            strategy_snapshot: dict[str, object] = {
                "strategy_id": strategy.strategy_id,
                "portfolio_id": portfolio_id,
                "status": status,
                "nav_usd": refreshed.equity_usd,
                "daily_pnl_usd": refreshed.daily_pnl_usd,
                "total_pnl_usd": refreshed.equity_usd - refreshed.initial_cash_usd,
                "drawdown_fraction": drawdown,
                "trades": refreshed.trades_today,
                "last_signal": last_signal,
                "reasons": reasons,
            }
            self.audit.append("strategy_snapshot", strategy_snapshot)
            results.append(strategy_snapshot)
        self.audit.heartbeat(
            "shadow-engine",
            "healthy",
            {"strategies": 12, "market_event_hash": event.event_hash},
        )
        return ShadowTickResult(
            observed_at.isoformat(), event.event_hash, tuple(results)
        )

    def collect_and_tick(self, collector: MarketDataCollector) -> ShadowTickResult:
        snapshots: dict[str, MarketSnapshot] = {}
        for symbol, instrument_id in self.config.symbols.items():
            snapshots[symbol] = collector.collect(
                symbol,
                instrument_id,
                self.config.candle_interval,
                self.config.candle_count,
            )
        return self.tick(snapshots)

    def run_forever(self, collector: MarketDataCollector, interval_seconds: int = 60) -> None:
        if interval_seconds < 15:
            raise ValueError("collector loop interval must be at least 15 seconds")
        while True:
            try:
                self.collect_and_tick(collector)
            except Exception as exc:
                self.audit.heartbeat(
                    "shadow-engine",
                    "error",
                    {"error_type": type(exc).__name__},
                )
                self.audit.append(
                    "shadow_engine_error", {"error_type": type(exc).__name__}
                )
            time.sleep(interval_seconds)
