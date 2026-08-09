from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo

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
RESEARCH_EPOCH = "closed-bars-v2-20260810"


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
        self._activate_research_epoch()

    def _activate_research_epoch(self) -> None:
        previous = self.audit.state_get("research_epoch", "")
        if previous == RESEARCH_EPOCH:
            return
        started_at = datetime.now(timezone.utc).isoformat()
        invalidated: list[str] = []
        carried: list[str] = []
        for portfolio_id in SHADOW_PORTFOLIO_IDS:
            pending_key = f"shadow_pending_intent:{portfolio_id}"
            if self.audit.state_get(pending_key, ""):
                self.audit.state_set(pending_key, "")
                invalidated.append(portfolio_id)
            has_position = self._position(portfolio_id) is not None
            self.audit.state_set(
                f"research_epoch_carried:{RESEARCH_EPOCH}:{portfolio_id}",
                "1" if has_position else "0",
            )
            if has_position:
                carried.append(portfolio_id)
        self.audit.state_set("research_epoch", RESEARCH_EPOCH)
        self.audit.state_set("research_epoch_started_at", started_at)
        self.audit.append(
            "research_epoch_started",
            {
                "research_epoch": RESEARCH_EPOCH,
                "previous_epoch": previous or None,
                "reason": "closed_bar_finalization_and_quote_time_hardening",
                "invalidated_pending_portfolios": invalidated,
                "carried_position_portfolios": carried,
            },
        )

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
    def _position_mark(
        snapshot: MarketSnapshot,
        position: tuple[str, Decimal, Decimal] | None,
    ) -> Decimal:
        return snapshot.ask if position is not None and position[1] < 0 else snapshot.bid

    @staticmethod
    def _bar_fingerprint(
        snapshot: MarketSnapshot, related: MarketSnapshot | None = None
    ) -> str:
        payload: dict[str, object] = {"symbol": snapshot.symbol}
        if snapshot.candles:
            payload.update(
                {
                    "timestamp": snapshot.candles[-1].timestamp.isoformat(),
                    "close": str(snapshot.candles[-1].close),
                }
            )
        else:
            payload["content_hash"] = snapshot.content_hash
        if related is not None:
            payload["related"] = (
                {
                    "symbol": related.symbol,
                    "timestamp": related.candles[-1].timestamp.isoformat(),
                    "close": str(related.candles[-1].close),
                }
                if related.candles
                else {"symbol": related.symbol, "content_hash": related.content_hash}
            )
        return canonical_hash(payload)

    def _epoch_metrics(
        self,
        portfolio_id: str,
        equity: Decimal,
        ledger_daily_pnl: Decimal,
        observed_at: datetime,
    ) -> tuple[Decimal, Decimal, int, bool]:
        baseline_key = f"research_epoch_baseline:{RESEARCH_EPOCH}:{portfolio_id}"
        baseline_raw = self.audit.state_get(baseline_key, "")
        if not baseline_raw:
            baseline_raw = str(equity)
            self.audit.state_set(baseline_key, baseline_raw)
            self.audit.append(
                "research_epoch_baseline",
                {
                    "research_epoch": RESEARCH_EPOCH,
                    "portfolio_id": portfolio_id,
                    "equity_usd": equity,
                },
            )
        epoch_pnl = equity - Decimal(baseline_raw)
        started_at = datetime.fromisoformat(
            self.audit.state_get("research_epoch_started_at", observed_at.isoformat())
        )
        reporting_tz = ZoneInfo(self.config.report_timezone)
        daily_pnl = (
            epoch_pnl
            if started_at.astimezone(reporting_tz).date()
            == observed_at.astimezone(reporting_tz).date()
            else ledger_daily_pnl
        )
        trades = int(
            self.audit.db.execute(
                "SELECT COUNT(*) FROM shadow_fills WHERE portfolio_id=? AND ts>=?",
                (portfolio_id, started_at.astimezone(timezone.utc).isoformat()),
            ).fetchone()[0]
        )
        carry_key = f"research_epoch_carried:{RESEARCH_EPOCH}:{portfolio_id}"
        carried = self.audit.state_get(carry_key, "0") == "1"
        if carried and self._position(portfolio_id) is None:
            carried = False
            self.audit.state_set(carry_key, "0")
            self.audit.append(
                "research_epoch_carry_resolved",
                {"research_epoch": RESEARCH_EPOCH, "portfolio_id": portfolio_id},
            )
        return epoch_pnl, daily_pnl, trades, carried

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
            quote_observed_at=int(
                (snapshot.quote_observed_at or observed_at).timestamp()
            ),
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
        self._record_open_fill(ledger, portfolio_id, intent, snapshot, observed_at)
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

    @staticmethod
    def _record_open_fill(
        ledger: ShadowPortfolioLedger,
        portfolio_id: str,
        intent: TradeIntent,
        snapshot: MarketSnapshot,
        observed_at: datetime,
    ) -> None:
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

    def _prepare_master_open(
        self,
        intent: TradeIntent,
        snapshot: MarketSnapshot,
        observed_at: datetime,
    ) -> tuple[bool, tuple[str, ...], ApprovedOrder | None]:
        state = self.master_ledger.snapshot(
            MASTER_PORTFOLIO_ID,
            {intent.symbol: snapshot.bid},
            as_of=observed_at,
        )
        risk_result = self.risk.evaluate(
            intent,
            self._risk_context(
                MASTER_PORTFOLIO_ID,
                state,
                snapshot,
                observed_at,
                open_positions=0,
            ),
        )
        return risk_result.approved, risk_result.reasons, risk_result.order

    def _broker_symbol_position_state(self, symbol: str) -> tuple[int, ...]:
        if self.demo_client is None:
            raise RuntimeError("DEMO master reconciliation requires broker read access")
        result = self.demo_client.execute_read("/api/v1/trading/info/demo/portfolio")
        if not result.is_success or not isinstance(result.body, dict):
            raise RuntimeError("DEMO master broker reconciliation failed")
        portfolio = result.body.get("clientPortfolio", result.body)
        positions = portfolio.get("positions", []) if isinstance(portfolio, dict) else []
        instrument_id = self.config.symbols[symbol]
        return tuple(
            int(position.get("positionID", position.get("positionId", 0)))
            for position in positions
            if isinstance(position, dict)
            and int(position.get("instrumentID", position.get("instrumentId", -1)))
            == instrument_id
            and int(position.get("positionID", position.get("positionId", 0))) > 0
        )

    def _set_master_pending_execution(
        self,
        action: str,
        order: ApprovedOrder,
        *,
        intent: TradeIntent | None = None,
        symbol: str,
    ) -> None:
        if self.audit.state_get("master_pending_execution", ""):
            raise RuntimeError("master execution is already pending")
        payload: dict[str, object] = {
            "action": action,
            "proposal_id": order.proposal_id,
            "symbol": symbol,
            "research_epoch": RESEARCH_EPOCH,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        if intent is not None:
            payload["intent"] = self._serialize_intent(intent)
        self.audit.state_set(
            "master_pending_execution",
            json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str),
        )
        self.audit.append("master_execution_pending", payload)

    def _reconcile_master_pending_execution(
        self,
        snapshots: Mapping[str, MarketSnapshot],
        observed_at: datetime,
    ) -> None:
        raw = self.audit.state_get("master_pending_execution", "")
        if not raw:
            return
        pending = json.loads(raw)
        proposal_id = str(pending["proposal_id"])
        proposal = self.audit.proposal(proposal_id)
        if proposal is None:
            raise RuntimeError("master pending execution lost its immutable proposal")
        state = str(proposal["state"])
        if state in {"REJECTED", "CANCELLED"}:
            self.audit.state_set("master_pending_execution", "")
            self.audit.append(
                "master_execution_rejected",
                {"proposal_id": proposal_id, "state": state},
            )
            return
        if state not in {"ACKNOWLEDGED", "FILLED", "PARTIAL", "RECONCILED"}:
            return
        symbol = str(pending["symbol"])
        if symbol not in snapshots:
            raise RuntimeError("master pending execution symbol has no market snapshot")
        broker_positions = self._broker_symbol_position_state(symbol)
        action = str(pending["action"])
        if action == "OPEN":
            if len(broker_positions) != 1:
                self._lock_stale_master_execution(pending, observed_at)
                return
            intent_raw = pending.get("intent")
            if not isinstance(intent_raw, dict):
                raise RuntimeError("master pending open lost its immutable intent")
            intent = self._deserialize_intent(intent_raw)
            if self._position(MASTER_PORTFOLIO_ID) is None:
                self._record_open_fill(
                    self.master_ledger,
                    MASTER_PORTFOLIO_ID,
                    intent,
                    snapshots[symbol],
                    observed_at,
                )
        elif action == "CLOSE":
            if broker_positions:
                self._lock_stale_master_execution(pending, observed_at)
                return
            position = self._position(MASTER_PORTFOLIO_ID)
            if position is not None:
                self._close_position(
                    self.master_ledger,
                    MASTER_PORTFOLIO_ID,
                    position,
                    snapshots[symbol],
                    observed_at,
                )
        else:
            raise RuntimeError("master pending execution action is invalid")
        self.audit.state_set("master_pending_execution", "")
        self.audit.append(
            "master_execution_reconciled",
            {
                "proposal_id": proposal_id,
                "action": action,
                "symbol": symbol,
                "broker_position_ids": broker_positions,
            },
        )

    def _lock_stale_master_execution(
        self, pending: dict[str, object], observed_at: datetime
    ) -> None:
        if pending.get("timeout_locked"):
            return
        try:
            created_at = datetime.fromisoformat(str(pending["created_at"]))
            if created_at.tzinfo is None:
                raise ValueError("pending execution timestamp is not timezone-aware")
        except (KeyError, ValueError) as exc:
            raise RuntimeError("master pending execution timestamp is invalid") from exc
        if observed_at.astimezone(timezone.utc) - created_at.astimezone(timezone.utc) <= timedelta(
            seconds=120
        ):
            return
        pending["timeout_locked"] = True
        self.audit.state_set(
            "master_pending_execution",
            json.dumps(pending, sort_keys=True, separators=(",", ":"), default=str),
        )
        self.audit.set_kill_state(
            KillState.LOCKED,
            "shadow-engine",
            "broker ACK was not reconciled to broker truth within 120 seconds",
        )
        self.audit.append(
            "master_execution_reconciliation_timeout",
            {
                "proposal_id": pending.get("proposal_id"),
                "action": pending.get("action"),
                "symbol": pending.get("symbol"),
            },
        )

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
            order.proposal_id, request, order, source=source
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
    ) -> ApprovedOrder | None:
        if self.config.account_mode != "demo" or not self.config.etoro_demo_execution_enabled:
            return None
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
        return close_result.order

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
        self._reconcile_master_pending_execution(snapshots, observed_at)
        for decision in self.ai.consume_ready():
            if decision.payload.get("research_epoch") != RESEARCH_EPOCH:
                self.audit.append(
                    "master_ai_noop",
                    {
                        "packet_id": decision.packet_id,
                        "reason": "stale_research_epoch",
                    },
                )
                continue
            position = self._position(MASTER_PORTFOLIO_ID)
            if self.audit.state_get("master_pending_execution", ""):
                self.audit.append(
                    "master_ai_noop",
                    {"packet_id": decision.packet_id, "reason": "execution_pending"},
                )
                continue
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
                close_order = self._register_demo_close_proposal(
                    position[0], snapshots[position[0]], observed_at
                )
                if self.config.etoro_demo_execution_enabled:
                    if close_order is None:
                        raise RuntimeError("DEMO close did not create a sealed proposal")
                    self._set_master_pending_execution(
                        "CLOSE", close_order, symbol=position[0]
                    )
                else:
                    self._close_position(
                        self.master_ledger,
                        MASTER_PORTFOLIO_ID,
                        position,
                        snapshots[position[0]],
                        observed_at,
                    )
                self.audit.append(
                    (
                        "master_ai_close_requested"
                        if self.config.etoro_demo_execution_enabled
                        else "master_ai_closed"
                    ),
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
            if self.config.etoro_demo_execution_enabled:
                filled, reasons, order = self._prepare_master_open(
                    intent, snapshot, observed_at
                )
                if filled and order is not None:
                    self._register_demo_proposal(order, "sol_master_open")
                    self._set_master_pending_execution(
                        "OPEN", order, intent=intent, symbol=intent.symbol
                    )
            else:
                filled, reasons, order = self._fill_open(
                    self.master_ledger,
                    MASTER_PORTFOLIO_ID,
                    intent,
                    snapshot,
                    observed_at,
                )
            self.audit.append(
                "master_ai_open_result",
                {
                    "packet_id": decision.packet_id,
                    "candidate_id": decision.candidate_id,
                    "strategy_id": intent.strategy_id,
                    "accepted_by_risk": filled,
                    "execution_pending": bool(
                        filled and self.config.etoro_demo_execution_enabled
                    ),
                    "locally_filled": bool(
                        filled and not self.config.etoro_demo_execution_enabled
                    ),
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

    def tick(
        self,
        snapshots: Mapping[str, MarketSnapshot],
        bar_fingerprints: Mapping[str, str] | None = None,
    ) -> ShadowTickResult:
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
            marks = {symbol: self._position_mark(snapshot, position)}
            status = "hold"
            last_signal: str | None = None
            reasons: tuple[str, ...] = ()
            evaluated = bar_fingerprints is None or portfolio_id in bar_fingerprints
            had_activity = False
            pending_key = f"shadow_pending_intent:{portfolio_id}"
            pending_raw = self.audit.state_get(pending_key, "")
            if pending_raw:
                had_activity = True
                pending_payload = json.loads(pending_raw)
                if "intent" in pending_payload:
                    pending = self._deserialize_intent(pending_payload["intent"])
                    queued_quote_at = datetime.fromisoformat(
                        str(pending_payload["queued_quote_observed_at"])
                    ).astimezone(timezone.utc)
                else:
                    pending = self._deserialize_intent(pending_payload)
                    queued_quote_at = datetime.min.replace(tzinfo=timezone.utc)
                quote_is_newer = bool(
                    snapshot.quote_observed_at
                    and snapshot.quote_observed_at > queued_quote_at
                )
                if not quote_is_newer:
                    status = "waiting_for_next_quote"
                elif position is None:
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
                if quote_is_newer:
                    self.audit.state_set(pending_key, "")
                position = self._position(portfolio_id)
                marks = {symbol: self._position_mark(snapshot, position)}

            intent = (
                strategy.decide_context(self._context(snapshot, related))
                if evaluated and not had_activity
                else None
            )
            if intent is not None:
                last_signal = intent.side.value
                self.audit.append(
                    "trade_intent",
                    {**asdict(intent), "research_epoch": RESEARCH_EPOCH},
                )
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
                            {
                                "intent": self._serialize_intent(intent),
                                "queued_quote_observed_at": (
                                    snapshot.quote_observed_at or observed_at
                                ).isoformat(),
                            },
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
            if evaluated and bar_fingerprints is not None:
                self.audit.state_set(
                    f"shadow_last_evaluated_bar:{portfolio_id}",
                    str(bar_fingerprints[portfolio_id]),
                )
            if not evaluated and not had_activity:
                continue
            refreshed = self.ledger.snapshot(portfolio_id, marks, as_of=observed_at)
            epoch_pnl, daily_pnl, epoch_trades, carried = self._epoch_metrics(
                portfolio_id,
                refreshed.equity_usd,
                refreshed.daily_pnl_usd,
                observed_at,
            )
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
                "daily_pnl_usd": daily_pnl,
                "total_pnl_usd": epoch_pnl,
                "drawdown_fraction": drawdown,
                "trades": epoch_trades,
                "last_signal": last_signal,
                "reasons": reasons,
                "research_epoch": RESEARCH_EPOCH,
                "carried_position": carried,
                "eligible_for_promotion": not carried,
            }
            self.audit.append("strategy_snapshot", strategy_snapshot)
            results.append(strategy_snapshot)

        master_position = self._position(MASTER_PORTFOLIO_ID)
        master_marks = (
            {}
            if master_position is None
            else {
                master_position[0]: self._position_mark(
                    normalized[master_position[0]], master_position
                )
            }
        )
        master_state = self.master_ledger.snapshot(
            MASTER_PORTFOLIO_ID, master_marks, as_of=observed_at
        )
        position_review_due = False
        if master_position is not None:
            position_snapshot = normalized[master_position[0]]
            position_bar = self._bar_fingerprint(position_snapshot)
            position_review_key = f"master_position_review_bar:{master_position[0]}"
            position_review_due = (
                self.audit.state_get(position_review_key, "") != position_bar
            )
        master_execution_pending = bool(
            self.audit.state_get("master_pending_execution", "")
        )
        if self.config.ai_decision_enabled and not master_execution_pending and (
            master_candidates or (master_position and position_review_due)
        ):
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
                "research_epoch": RESEARCH_EPOCH,
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
            if master_position is not None and position_review_due:
                self.audit.state_set(position_review_key, position_bar)
        self.audit.heartbeat(
            "shadow-engine",
            "healthy",
            {
                "strategies": 12,
                "market_event_hash": event.event_hash,
                "master_portfolio": MASTER_PORTFOLIO_ID,
                "ai_pending": len(self.ai.pending()),
                "evaluated_strategies": len(results),
                "research_epoch": RESEARCH_EPOCH,
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
                close_grace_seconds=self.config.candle_close_grace_seconds,
            )
        fingerprints: dict[str, str] = {}
        for index, (symbol, portfolio_id) in enumerate(
            zip(STRATEGY_SYMBOLS, SHADOW_PORTFOLIO_IDS, strict=True)
        ):
            related = snapshots["NSDQ100"] if index == 10 else None
            fingerprint = self._bar_fingerprint(snapshots[symbol], related)
            if (
                self.audit.state_get(
                    f"shadow_last_evaluated_bar:{portfolio_id}", ""
                )
                != fingerprint
            ):
                fingerprints[portfolio_id] = fingerprint
        return self.tick(snapshots, fingerprints)

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
