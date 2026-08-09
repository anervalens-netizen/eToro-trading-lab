from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Mapping

from .ai_decision import AIDecisionStore
from .audit import AuditLog
from .backtest import costs_for_symbol
from .config import AppConfig
from .data_quality import INTERVAL_DURATIONS
from .market import MarketDataCollector, MarketSnapshot
from .models import ApprovedOrder, CloseIntent, KillState, RiskContext, Side, TradeIntent
from .nautilus_runtime import ReplayClock
from .portfolio import MASTER_PORTFOLIO_ID, SHADOW_PORTFOLIO_IDS, ShadowPortfolioLedger
from .risk import DeterministicRiskEngine, canonical_hash, load_private_signing_key
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
        self.master_ledger = ShadowPortfolioLedger(
            audit,
            initial_cash_usd=config.initial_cash_usd,
            portfolio_ids=(MASTER_PORTFOLIO_ID,),
            reporting_timezone=config.report_timezone,
        )
        strategy_ids = {strategy.strategy_id for strategy in self.strategies}
        if not set(config.master_strategy_ids) <= strategy_ids:
            raise ValueError("master_strategy_ids contains an unknown strategy")
        self.ai = AIDecisionStore(audit)
        key_path = Path(
            os.getenv(
                "ETORO_RISK_SIGNING_KEY_FILE",
                str(self.runtime_dir / "risk-signing.key"),
            )
        )
        if config.account_mode == "demo":
            if not key_path.exists():
                raise RuntimeError("DEMO mode requires the persistent Ed25519 signing key")
            self.risk = DeterministicRiskEngine(
                config.risk, load_private_signing_key(key_path)
            )
        else:
            self.risk = DeterministicRiskEngine(config.risk)
        self.clock = ReplayClock()
        self.demo_client = None

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
    def _serialize_intent(intent: TradeIntent) -> dict[str, object]:
        value = asdict(intent)
        value["side"] = intent.side.value
        return value

    @staticmethod
    def _deserialize_intent(value: Mapping[str, object]) -> TradeIntent:
        return TradeIntent(
            **{
                **dict(value),
                "side": Side(str(value["side"])),
                "amount_usd": Decimal(str(value["amount_usd"])),
                "confidence": Decimal(str(value["confidence"])),
                "stop_loss_fraction": Decimal(str(value["stop_loss_fraction"])),
                "take_profit_fraction": Decimal(str(value["take_profit_fraction"])),
                "leverage": int(value.get("leverage", 1)),
                "signal_ts": int(value.get("signal_ts", 0)),
                "max_holding_seconds": int(value.get("max_holding_seconds", 86400)),
            }
        )

    def _risk_context(
        self,
        portfolio_id: str,
        state: object,
        snapshot: MarketSnapshot,
        observed_at: datetime,
        *,
        open_positions: int,
    ) -> RiskContext:
        return RiskContext(
            equity_usd=state.equity_usd,
            peak_equity_usd=state.peak_equity_usd,
            daily_pnl_usd=state.daily_pnl_usd,
            gross_exposure_usd=state.gross_exposure_usd,
            symbol_exposure_usd=state.gross_exposure_usd,
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
            open_positions=open_positions,
            quote_observed_at=int(observed_at.timestamp()),
            evaluated_at=int(observed_at.timestamp()),
            data_quality_ok=(
                snapshot.market_open
                and bool(snapshot.quality is None or snapshot.quality.is_valid)
            ),
            audit_writable=self.audit.verify_chain(),
            reconciliation_ok=True,
        )

    def _fill_open(
        self,
        ledger: ShadowPortfolioLedger,
        portfolio_id: str,
        intent: TradeIntent,
        snapshot: MarketSnapshot,
        observed_at: datetime,
    ) -> tuple[bool, tuple[str, ...], ApprovedOrder | None]:
        state = ledger.snapshot(portfolio_id, {intent.symbol: snapshot.bid}, as_of=observed_at)
        risk_result = self.risk.evaluate(
            intent,
            self._risk_context(
                portfolio_id, state, snapshot, observed_at, open_positions=0
            ),
        )
        if not risk_result.approved or risk_result.order is None:
            return False, risk_result.reasons, None
        price = snapshot.ask if intent.side is Side.BUY else snapshot.bid
        units = intent.amount_usd / price
        fee = intent.amount_usd * costs_for_symbol(intent.symbol).commission_fraction
        ledger.record_fill(
            portfolio_id,
            intent.symbol,
            "buy" if intent.side is Side.BUY else "sell",
            units,
            price,
            fee_usd=fee,
            executed_at=observed_at,
        )
        self.audit.append(
            "shadow_risk_approval",
            {
                "strategy_id": intent.strategy_id,
                "portfolio_id": portfolio_id,
                "intent_hash": risk_result.order.intent_hash,
                "risk_snapshot_hash": risk_result.order.risk_snapshot_hash,
                "risk_config_hash": risk_result.order.risk_config_hash,
            },
        )
        return True, (), risk_result.order

    def _register_demo_proposal(self, order: ApprovedOrder, source: str) -> None:
        if self.config.account_mode != "demo" or not self.config.etoro_demo_execution_enabled:
            return
        request = {
            "account": "DEMO",
            "method": order.method,
            "path": order.route,
            "body": json.loads(order.body_json),
        }
        envelope_hash = self.audit.register_proposal(
            order.proposal_id, request, order
        )
        self.audit.append(
            "risk_approval",
            {
                "proposal_id": order.proposal_id,
                "envelope_hash": envelope_hash,
                "request": request,
                "expires_at": order.expires_at,
                "intent_hash": order.intent_hash,
                "risk_snapshot_hash": order.risk_snapshot_hash,
                "risk_config_hash": order.risk_config_hash,
                "source": source,
            },
        )

    def _register_demo_close_proposal(
        self,
        symbol: str,
        snapshot: MarketSnapshot,
        observed_at: datetime,
    ) -> None:
        if self.config.account_mode != "demo" or not self.config.etoro_demo_execution_enabled:
            return
        if self.demo_client is None:
            raise RuntimeError("DEMO close requires a reconciled broker client")
        result = self.demo_client.execute_read("/api/v1/trading/info/demo/portfolio")
        if not result.is_success or not isinstance(result.body, dict):
            raise RuntimeError("DEMO portfolio reconciliation failed before close")
        portfolio = result.body.get("clientPortfolio", result.body)
        positions = portfolio.get("positions", []) if isinstance(portfolio, dict) else []
        instrument_id = self.config.symbols[symbol]
        matches = [
            position
            for position in positions
            if isinstance(position, dict)
            and int(position.get("instrumentID", position.get("instrumentId", -1)))
            == instrument_id
        ]
        if len(matches) != 1:
            raise PermissionError("DEMO close requires exactly one reconciled broker position")
        position_id = int(matches[0].get("positionID", matches[0].get("positionId", 0)))
        state = self.master_ledger.snapshot(
            MASTER_PORTFOLIO_ID, {symbol: snapshot.bid}, as_of=observed_at
        )
        close_result = self.risk.evaluate_close(
            CloseIntent(
                symbol=symbol,
                position_id=position_id,
                instrument_id=instrument_id,
                units_to_deduct=None,
                rationale="Sol master requested a full risk-reducing DEMO close",
            ),
            self._risk_context(
                MASTER_PORTFOLIO_ID,
                state,
                snapshot,
                observed_at,
                open_positions=1,
            ),
        )
        if not close_result.approved or close_result.order is None:
            raise PermissionError(
                f"DEMO close risk seal rejected: {','.join(close_result.reasons)}"
            )
        self._register_demo_proposal(close_result.order, "sol_master_close")

    def _close_position(
        self,
        ledger: ShadowPortfolioLedger,
        portfolio_id: str,
        position: tuple[str, Decimal, Decimal],
        snapshot: MarketSnapshot,
        observed_at: datetime,
    ) -> None:
        symbol, units, _ = position
        close_price = snapshot.bid if units > 0 else snapshot.ask
        notional = abs(units) * close_price
        fee = notional * costs_for_symbol(symbol).commission_fraction
        ledger.record_fill(
            portfolio_id,
            symbol,
            "sell" if units > 0 else "buy",
            abs(units),
            close_price,
            fee_usd=fee,
            executed_at=observed_at,
        )

    def _consume_ai_decisions(
        self,
        snapshots: Mapping[str, MarketSnapshot],
        observed_at: datetime,
    ) -> None:
        for decision in self.ai.consume_ready():
            position = self._position(MASTER_PORTFOLIO_ID)
            if decision.action == "HOLD":
                self.audit.append("master_ai_hold", {"packet_id": decision.packet_id})
                continue
            if decision.action == "CLOSE":
                expected_position = decision.payload.get("position")
                expected_symbol = (
                    str(expected_position.get("symbol"))
                    if isinstance(expected_position, dict)
                    else ""
                )
                if (
                    position is None
                    or position[0] not in snapshots
                    or position[0] != expected_symbol
                ):
                    self.audit.append(
                        "master_ai_noop",
                        {"packet_id": decision.packet_id, "reason": "no_position_to_close"},
                    )
                    continue
                self._register_demo_close_proposal(
                    position[0], snapshots[position[0]], observed_at
                )
                self._close_position(
                    self.master_ledger,
                    MASTER_PORTFOLIO_ID,
                    position,
                    snapshots[position[0]],
                    observed_at,
                )
                self.audit.append(
                    "master_ai_closed",
                    {"packet_id": decision.packet_id, "symbol": position[0]},
                )
                continue
            candidates = {
                str(item["candidate_id"]): item
                for item in decision.payload.get("candidates", [])
                if isinstance(item, dict) and "candidate_id" in item
            }
            candidate = candidates.get(decision.candidate_id)
            if position is not None or candidate is None:
                self.audit.append(
                    "master_ai_noop",
                    {
                        "packet_id": decision.packet_id,
                        "reason": "position_open_or_candidate_mismatch",
                    },
                )
                continue
            intent = self._deserialize_intent(candidate["intent"])
            snapshot = snapshots[intent.symbol]
            filled, reasons, order = self._fill_open(
                self.master_ledger,
                MASTER_PORTFOLIO_ID,
                intent,
                snapshot,
                observed_at,
            )
            if filled and order is not None:
                self._register_demo_proposal(order, "sol_master_open")
            self.audit.append(
                "master_ai_open_result",
                {
                    "packet_id": decision.packet_id,
                    "candidate_id": decision.candidate_id,
                    "strategy_id": intent.strategy_id,
                    "filled": filled,
                    "risk_reasons": reasons,
                },
            )

    @staticmethod
    def _context(snapshot: MarketSnapshot, related: MarketSnapshot | None = None) -> StrategyContext:
        return StrategyContext(
            symbol=snapshot.symbol,
            closes=snapshot.closes,
            highs=tuple(candle.high for candle in snapshot.candles),
            lows=tuple(candle.low for candle in snapshot.candles),
            timestamps=tuple(candle.timestamp for candle in snapshot.candles),
            related_closes={} if related is None else {related.symbol: related.closes},
            bar_interval_seconds=(
                snapshot.quality.expected_interval_seconds
                if snapshot.quality is not None
                else int(INTERVAL_DURATIONS[snapshot.interval].total_seconds())
            ),
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
            "replay_market_batch",
            {
                "event_hash": event.event_hash,
                "timestamp_ns": event.timestamp_ns,
                "snapshot_hashes": {
                    symbol: snapshot.content_hash for symbol, snapshot in normalized.items()
                },
            },
        )
        self.ai.expire_pending()
        self._consume_ai_decisions(normalized, observed_at)
        results: list[dict[str, object]] = []
        master_candidates: list[dict[str, object]] = []
        for index, (strategy, symbol, portfolio_id) in enumerate(
            zip(self.strategies, STRATEGY_SYMBOLS, SHADOW_PORTFOLIO_IDS, strict=True)
        ):
            snapshot = normalized[symbol]
            related = normalized["NSDQ100"] if index == 10 else None
            position = self._position(portfolio_id)
            marks = {symbol: snapshot.bid}
            status = "hold"
            last_signal: str | None = None
            reasons: tuple[str, ...] = ()
            pending_key = f"shadow_pending_intent:{portfolio_id}"
            pending_raw = self.audit.state_get(pending_key, "")
            if pending_raw:
                pending = self._deserialize_intent(json.loads(pending_raw))
                if position is None:
                    filled, reasons, _ = self._fill_open(
                        self.ledger, portfolio_id, pending, snapshot, observed_at
                    )
                    status = "shadow_filled_next_quote" if filled else "risk_rejected"
                else:
                    _, units, _ = position
                    opposing = (units > 0 and pending.side is Side.SELL) or (
                        units < 0 and pending.side is Side.BUY
                    )
                    if opposing:
                        self._close_position(
                            self.ledger, portfolio_id, position, snapshot, observed_at
                        )
                        status = "shadow_closed_next_quote"
                self.audit.state_set(pending_key, "")
                position = self._position(portfolio_id)

            intent = strategy.decide_context(self._context(snapshot, related))
            if intent is not None:
                last_signal = intent.side.value
                self.audit.append("trade_intent", asdict(intent))
                should_queue = position is None
                if position is not None:
                    _, units, _ = position
                    should_queue = (units > 0 and intent.side is Side.SELL) or (
                        units < 0 and intent.side is Side.BUY
                    )
                if should_queue:
                    self.audit.state_set(
                        pending_key,
                        json.dumps(
                            self._serialize_intent(intent),
                            sort_keys=True,
                            separators=(",", ":"),
                            default=str,
                        ),
                    )
                    status = "queued_next_quote"
                elif status == "hold":
                    status = "position_open"
                if strategy.strategy_id in self.config.master_strategy_ids:
                    serialized = self._serialize_intent(intent)
                    candidate_id = canonical_hash(serialized)[:24]
                    master_candidates.append(
                        {
                            "candidate_id": candidate_id,
                            "strategy_id": strategy.strategy_id,
                            "symbol": intent.symbol,
                            "side": intent.side.value,
                            "confidence": str(intent.confidence),
                            "intent": serialized,
                        }
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

        master_position = self._position(MASTER_PORTFOLIO_ID)
        master_marks = (
            {}
            if master_position is None
            else {master_position[0]: normalized[master_position[0]].bid}
        )
        master_state = self.master_ledger.snapshot(
            MASTER_PORTFOLIO_ID, master_marks, as_of=observed_at
        )
        if self.config.ai_decision_enabled and (master_candidates or master_position):
            market_features: dict[str, object] = {}
            relevant_symbols = {str(item["symbol"]) for item in master_candidates}
            if master_position is not None:
                relevant_symbols.add(master_position[0])
            for feature_symbol in sorted(relevant_symbols):
                feature_snapshot = normalized[feature_symbol]
                closes = feature_snapshot.closes
                returns = {}
                for horizon in (1, 4, 16):
                    if len(closes) > horizon and closes[-horizon - 1] > 0:
                        returns[str(horizon)] = str(
                            closes[-1] / closes[-horizon - 1] - Decimal("1")
                        )
                mid = (feature_snapshot.bid + feature_snapshot.ask) / Decimal("2")
                market_features[feature_symbol] = {
                    "returns_by_bars": returns,
                    "spread_fraction": str(
                        (feature_snapshot.ask - feature_snapshot.bid) / mid
                    ),
                    "market_open": feature_snapshot.market_open,
                    "data_quality_ok": bool(
                        feature_snapshot.quality is None
                        or feature_snapshot.quality.is_valid
                    ),
                    "snapshot_hash": feature_snapshot.content_hash,
                }
            packet_payload = {
                "schema_version": 1,
                "mode": "POSITION_REVIEW" if master_position else "ENTRY_REVIEW",
                "account_mode": "PAPER_DEMO_ONLY",
                "real_money": False,
                "bar_observed_at": observed_at.isoformat(),
                "candidates": master_candidates,
                "position": (
                    None
                    if master_position is None
                    else {
                        "symbol": master_position[0],
                        "direction": "long" if master_position[1] > 0 else "short",
                    }
                ),
                "portfolio": {
                    "equity_usd": str(master_state.equity_usd),
                    "daily_pnl_usd": str(master_state.daily_pnl_usd),
                    "gross_exposure_usd": str(master_state.gross_exposure_usd),
                    "trades_today": master_state.trades_today,
                },
                "market_features": market_features,
                "allowed_actions": ["OPEN", "CLOSE", "HOLD"],
                "risk_config_hash": self.risk.risk_config_hash,
            }
            packet_id, packet_hash, created = self.ai.queue(
                packet_payload,
                int(datetime.now(timezone.utc).timestamp())
                + self.config.ai_decision_ttl_seconds,
            )
            self.audit.append(
                "master_ai_packet_status",
                {
                    "packet_id": packet_id,
                    "packet_hash": packet_hash,
                    "created": created,
                    "candidate_count": len(master_candidates),
                },
            )
        self.audit.heartbeat(
            "shadow-engine",
            "healthy",
            {
                "strategies": 12,
                "market_event_hash": event.event_hash,
                "master_portfolio": MASTER_PORTFOLIO_ID,
                "ai_pending": len(self.ai.pending()),
            },
        )
        return ShadowTickResult(
            observed_at.isoformat(), event.event_hash, tuple(results)
        )

    def collect_and_tick(self, collector: MarketDataCollector) -> ShadowTickResult:
        self.demo_client = collector.client
        snapshots: dict[str, MarketSnapshot] = {}
        for symbol, instrument_id in self.config.symbols.items():
            snapshots[symbol] = collector.collect(
                symbol,
                instrument_id,
                self.config.candle_interval,
                self.config.candle_count,
            )
        bar_identity = {
            symbol: {
                "timestamp": (
                    snapshot.candles[-1].timestamp.isoformat()
                    if snapshot.candles
                    else snapshot.content_hash
                ),
                "close": str(snapshot.closes[-1]),
            }
            for symbol, snapshot in sorted(snapshots.items())
        }
        bar_key = hashlib.sha256(
            json.dumps(bar_identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if self.audit.state_get("shadow_last_closed_bar_key", "") == bar_key:
            observed_at = max(
                snapshot.captured_at or datetime.now(timezone.utc)
                for snapshot in snapshots.values()
            )
            self.ai.expire_pending()
            self._consume_ai_decisions(snapshots, observed_at)
            self.audit.heartbeat(
                "shadow-engine",
                "healthy",
                {
                    "status": "waiting_for_closed_bar",
                    "bar_key": bar_key,
                    "ai_pending": len(self.ai.pending()),
                },
            )
            return ShadowTickResult(
                datetime.now(timezone.utc).isoformat(), bar_key, ()
            )
        result = self.tick(snapshots)
        self.audit.state_set("shadow_last_closed_bar_key", bar_key)
        return result

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
