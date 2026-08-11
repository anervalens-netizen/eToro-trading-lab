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
from .data_quality import INTERVAL_DURATIONS, MarketDataQualityError
from .execution import select_broker_eligibility
from .market import MarketDataCollector, MarketSnapshot
from .models import ApprovedOrder, CloseIntent, KillState, RiskContext, Side, TradeIntent
from .news import CommodityNewsStore
from .nautilus_runtime import ReplayClock
from .portfolio import MASTER_PORTFOLIO_ID, SHADOW_PORTFOLIO_IDS, ShadowPortfolioLedger
from .risk import DeterministicRiskEngine, canonical_hash, load_private_signing_key
from .strategy import StrategyContext, build_strategy_suite
from .strategy_catalog import STRATEGY_COUNT, STRATEGY_SYMBOLS


RESEARCH_EPOCH = "commodity-risk-grid-v5-20260810"
ENGINE_ERROR_AUDIT_INTERVAL_SECONDS = 15 * 60


class MarketCollectionFailure(RuntimeError):
    """Bind a safe symbol identifier to a collector failure without logging raw data."""

    def __init__(self, symbol: str, cause: Exception) -> None:
        super().__init__(f"market collection failed for {symbol}")
        self.symbol = symbol
        self.cause = cause


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
        if len(self.strategies) != STRATEGY_COUNT or len(STRATEGY_SYMBOLS) != STRATEGY_COUNT:
            raise RuntimeError("shadow engine strategy catalog is inconsistent")
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
        self.news = CommodityNewsStore(audit)
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
        invalidated_ai_packets = self.ai.invalidate_active(
            "research epoch and broker compatibility policy changed"
        )
        invalidated: list[str] = []
        carried: list[str] = []
        for portfolio_id in SHADOW_PORTFOLIO_IDS:
            self.audit.state_set(f"shadow_last_evaluated_bar:{portfolio_id}", "")
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
                "reason": "closed_bar_quote_time_and_broker_compatibility_hardening",
                "invalidated_pending_portfolios": invalidated,
                "invalidated_ai_packets": invalidated_ai_packets,
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
                """
                SELECT COUNT(*) FROM shadow_fills AS f
                WHERE f.portfolio_id=? AND f.ts>=?
                  AND NOT EXISTS(
                      SELECT 1 FROM shadow_fill_quarantine AS q WHERE q.fill_id=f.id
                  )
                """,
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
        if (
            risk_result.approved
            and risk_result.order is not None
            and self.config.etoro_demo_execution_enabled
        ):
            if self.demo_client is None:
                return False, ("broker_eligibility_unavailable",), None
            body = json.loads(risk_result.order.body_json)
            eligibility = self.demo_client.execute_read(
                "/api/v2/trading/info/demo/eligibility",
                body=json.dumps({"symbols": [intent.symbol], "currency": "USD"}),
            )
            if not eligibility.is_success:
                return False, ("broker_eligibility_unavailable",), None
            try:
                select_broker_eligibility(body, eligibility.body)
            except PermissionError:
                return False, ("broker_eligibility_rejected",), None
            costs = self.demo_client.execute_read(
                "/api/v2/trading/info/demo/costs",
                body=risk_result.order.body_json,
            )
            if not costs.is_success:
                return False, ("broker_cost_preview_unavailable",), None
        return risk_result.approved, risk_result.reasons, risk_result.order

    def _broker_symbol_positions(self, symbol: str) -> tuple[dict[str, object], ...]:
        if self.demo_client is None:
            raise RuntimeError("DEMO master reconciliation requires broker read access")
        result = self.demo_client.execute_read("/api/v1/trading/info/demo/portfolio")
        if not result.is_success or not isinstance(result.body, dict):
            raise RuntimeError("DEMO master broker reconciliation failed")
        portfolio = result.body.get("clientPortfolio", result.body)
        positions = portfolio.get("positions", []) if isinstance(portfolio, dict) else []
        if not isinstance(positions, list):
            raise RuntimeError("DEMO master broker position shape is invalid")
        instrument_id = self.config.symbols[symbol]
        matches: list[dict[str, object]] = []
        for position in positions:
            if not isinstance(position, dict):
                continue
            try:
                candidate_instrument = int(
                    position.get("instrumentID", position.get("instrumentId", -1))
                )
                candidate_position = int(
                    position.get("positionID", position.get("positionId", 0))
                )
            except (TypeError, ValueError):
                continue
            if candidate_instrument == instrument_id and candidate_position > 0:
                matches.append(dict(position))
        return tuple(matches)

    def _broker_symbol_position_state(self, symbol: str) -> tuple[int, ...]:
        return tuple(
            int(position.get("positionID", position.get("positionId", 0)))
            for position in self._broker_symbol_positions(symbol)
        )

    def _expected_master_broker_position_id(self, symbol: str) -> int | None:
        current = self.audit.state_get("master_broker_position_id", "")
        if current:
            try:
                position_id = int(current)
            except ValueError as exc:
                raise RuntimeError("stored master broker position identity is invalid") from exc
            if position_id <= 0:
                raise RuntimeError("stored master broker position identity is invalid")
            return position_id
        for row in self.audit.db.execute(
            """
            SELECT payload FROM events WHERE event_type='master_execution_reconciled'
            ORDER BY id DESC LIMIT 200
            """
        ):
            payload = json.loads(str(row[0]))
            position_ids = payload.get("broker_position_ids", [])
            if (
                payload.get("action") == "OPEN"
                and str(payload.get("symbol", "")).upper() == symbol.upper()
                and isinstance(position_ids, list)
                and len(position_ids) == 1
            ):
                position_id = int(position_ids[0])
                if position_id > 0:
                    return position_id
        return None

    def _broker_closed_trade(
        self, symbol: str, position_id: int
    ) -> dict[str, object] | None:
        if self.demo_client is None:
            raise RuntimeError("DEMO close-history reconciliation requires broker read access")
        opened = self.audit.db.execute(
            """
            SELECT f.ts FROM shadow_fills AS f
            WHERE f.portfolio_id=? AND f.symbol=?
              AND NOT EXISTS(
                  SELECT 1 FROM shadow_fill_quarantine AS q WHERE q.fill_id=f.id
              )
            ORDER BY f.id DESC LIMIT 1
            """,
            (MASTER_PORTFOLIO_ID, symbol.upper()),
        ).fetchone()
        if opened is None:
            raise RuntimeError("local master position has no immutable opening fill")
        opened_at = datetime.fromisoformat(str(opened[0]).replace("Z", "+00:00"))
        if opened_at.tzinfo is None:
            raise RuntimeError("local master opening fill timestamp is invalid")
        query = {
            "minDate": opened_at.astimezone(timezone.utc).date().isoformat(),
            "page": "1",
            "pageSize": "100",
        }
        result = self.demo_client.execute_read(
            "/api/v1/trading/info/trade/demo/history", query=query
        )
        if not result.is_success or not isinstance(result.body, list):
            raise RuntimeError("DEMO close history reconciliation failed")
        matches: list[dict[str, object]] = []
        for item in result.body:
            if not isinstance(item, dict):
                continue
            try:
                candidate_id = int(
                    item.get("positionId", item.get("positionID", 0))
                )
            except (TypeError, ValueError):
                continue
            if candidate_id == position_id:
                matches.append(item)
        if len(matches) > 1:
            raise RuntimeError("DEMO close history returned duplicate position identity")
        if not matches:
            return None
        match = dict(matches[0])
        if "positionId" not in match and "positionID" in match:
            match["positionId"] = match.pop("positionID")
        if "instrumentId" not in match and "instrumentID" in match:
            match["instrumentId"] = match.pop("instrumentID")
        return match

    def reconcile_master_broker_close(
        self,
        symbol: str,
        position_id: int,
        *,
        clear_pending_execution: bool = False,
    ) -> bool:
        """Import a closed DEMO position using broker history only; never write."""

        symbol = symbol.upper()
        trade = self._broker_closed_trade(symbol, position_id)
        if trade is None:
            return False
        return self.master_ledger.reconcile_broker_close(
            MASTER_PORTFOLIO_ID,
            symbol,
            self.config.symbols[symbol],
            trade,
            clear_pending_execution=clear_pending_execution,
        )

    def reconcile_master_broker_open(
        self,
        symbol: str,
        position_id: int,
        *,
        replace_local_projection: bool = False,
    ) -> bool:
        """Import a current DEMO position using broker truth only; never write."""

        symbol = symbol.upper()
        matches = [
            item
            for item in self._broker_symbol_positions(symbol)
            if int(item.get("positionID", item.get("positionId", 0))) == position_id
        ]
        if len(matches) != 1:
            raise RuntimeError("DEMO open reconciliation requires one exact broker position")
        return self.master_ledger.reconcile_broker_open(
            MASTER_PORTFOLIO_ID,
            symbol,
            self.config.symbols[symbol],
            matches[0],
            replace_local_projection=replace_local_projection,
        )

    def _reconcile_master_external_close(self, observed_at: datetime) -> None:
        if self.audit.state_get("master_pending_execution", ""):
            return
        position = self._position(MASTER_PORTFOLIO_ID)
        if position is None:
            return
        symbol = position[0]
        broker_position_rows = self._broker_symbol_positions(symbol)
        broker_positions = tuple(
            int(item.get("positionID", item.get("positionId", 0)))
            for item in broker_position_rows
        )
        expected_position_id = self._expected_master_broker_position_id(symbol)
        if len(broker_positions) == 1:
            if (
                expected_position_id is not None
                and broker_positions[0] != expected_position_id
            ):
                self._mark_master_reconciliation_drift(
                    {
                        "proposal_id": None,
                        "action": "BROKER_IDENTITY",
                        "symbol": symbol,
                    },
                    observed_at,
                    broker_positions,
                    "current DEMO position identity differs from the local master binding",
                )
                return
            try:
                self.master_ledger.validate_broker_open(
                    MASTER_PORTFOLIO_ID,
                    symbol,
                    self.config.symbols[symbol],
                    broker_position_rows[0],
                )
            except ValueError:
                self._mark_master_reconciliation_drift(
                    {
                        "proposal_id": None,
                        "action": "BROKER_PROJECTION",
                        "symbol": symbol,
                    },
                    observed_at,
                    broker_positions,
                    "current DEMO position fields differ from the local master projection",
                )
                return
            self.audit.state_set("master_broker_position_id", str(broker_positions[0]))
            return
        if not broker_positions and expected_position_id is not None:
            if self.reconcile_master_broker_close(symbol, expected_position_id):
                self.master_ledger.snapshot(MASTER_PORTFOLIO_ID, as_of=observed_at)
                self.audit.append(
                    "master_external_broker_close_projected",
                    {
                        "symbol": symbol,
                        "broker_position_id": expected_position_id,
                        "network_write_attempted": False,
                        "real_money": False,
                    },
                )
                return
        self._mark_master_reconciliation_drift(
            {
                "proposal_id": None,
                "action": "BROKER_CLOSE",
                "symbol": symbol,
            },
            observed_at,
            broker_positions,
            "local master position differs from current DEMO broker/history truth",
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
        if state in {"AWAITING_APPROVAL", "APPROVED"}:
            self.audit.reject_expired_before_send(
                proposal_id, now=int(observed_at.timestamp())
            )
            proposal = self.audit.proposal(proposal_id)
            if proposal is None:
                raise RuntimeError("master pending execution lost its immutable proposal")
            state = str(proposal["state"])
        if state in {"REJECTED", "CANCELLED"}:
            if state == "REJECTED" and proposal.get("consumed_at") is None:
                symbol = str(pending["symbol"])
                action = str(pending["action"])
                broker_positions = self._broker_symbol_position_state(symbol)
                local_position = self._position(MASTER_PORTFOLIO_ID)
                broker_truth_matches = (
                    action == "OPEN"
                    and local_position is None
                    and not broker_positions
                ) or (
                    action == "CLOSE"
                    and local_position is not None
                    and local_position[0] == symbol
                    and len(broker_positions) == 1
                )
                if not broker_truth_matches:
                    self._mark_master_reconciliation_drift(
                        pending,
                        observed_at,
                        broker_positions,
                        "rejected proposal broker truth differs from the local master ledger",
                    )
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
        broker_position_rows = self._broker_symbol_positions(symbol)
        broker_positions = tuple(
            int(item.get("positionID", item.get("positionId", 0)))
            for item in broker_position_rows
        )
        action = str(pending["action"])
        if action == "OPEN":
            if len(broker_positions) != 1:
                self._lock_stale_master_execution(pending, observed_at)
                return
            intent_raw = pending.get("intent")
            try:
                if not isinstance(intent_raw, dict):
                    raise ValueError("missing immutable intent")
                intent = self._deserialize_intent(intent_raw)
                broker_position = broker_position_rows[0]
                response = json.loads(str(proposal.get("response_json") or "{}"))
                response_body = response.get("body", response)
                if (
                    intent.symbol != symbol
                    or not isinstance(broker_position.get("isBuy"), bool)
                    or broker_position["isBuy"] != (intent.side is Side.BUY)
                    or abs(
                        Decimal(str(broker_position["initialAmountInDollars"]))
                        - intent.amount_usd
                    )
                    > Decimal("0.02")
                    or int(broker_position["orderID"])
                    != int(response_body["orderId"])
                ):
                    raise ValueError("broker open does not match ACK/intent identity")
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                self._mark_master_reconciliation_drift(
                    pending,
                    observed_at,
                    broker_positions,
                    "DEMO open broker identity differs from the sealed intent/ACK",
                )
                return
            if self._position(MASTER_PORTFOLIO_ID) is None:
                try:
                    self.master_ledger.reconcile_broker_open(
                        MASTER_PORTFOLIO_ID,
                        symbol,
                        self.config.symbols[symbol],
                        broker_position_rows[0],
                    )
                except ValueError:
                    self._mark_master_reconciliation_drift(
                        pending,
                        observed_at,
                        broker_positions,
                        "DEMO open fields cannot form an exact local projection",
                    )
                    return
            self.audit.state_set("master_broker_position_id", str(broker_positions[0]))
        elif action == "CLOSE":
            if broker_positions:
                self._lock_stale_master_execution(pending, observed_at)
                return
            position = self._position(MASTER_PORTFOLIO_ID)
            if position is not None:
                order = self.audit.load_order(proposal_id)
                try:
                    position_id = int(order.route.rsplit("/", 1)[-1])
                except ValueError as exc:
                    raise RuntimeError("master close proposal lost broker identity") from exc
                try:
                    reconciled = self.reconcile_master_broker_close(
                        symbol, position_id, clear_pending_execution=True
                    )
                except ValueError:
                    self._mark_master_reconciliation_drift(
                        pending,
                        observed_at,
                        broker_positions,
                        "DEMO close history cannot form an exact local projection",
                    )
                    return
                if not reconciled:
                    self._lock_stale_master_execution(pending, observed_at)
                    return
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

    def _mark_master_reconciliation_drift(
        self,
        pending: Mapping[str, object],
        observed_at: datetime,
        broker_positions: tuple[int, ...],
        reason: str,
    ) -> None:
        payload = {
            "proposal_id": pending.get("proposal_id"),
            "action": pending.get("action"),
            "symbol": pending.get("symbol"),
            "broker_position_ids": broker_positions,
            "detected_at": observed_at.astimezone(timezone.utc).isoformat(),
            "reason": reason,
        }
        self.audit.state_set(
            "master_reconciliation_drift",
            json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str),
        )
        if self.audit.kill_state() is not KillState.LOCKED:
            self.audit.set_kill_state(
                KillState.LOCKED,
                "shadow-engine",
                "DEMO broker truth differs from the local master ledger",
            )
        self.audit.append("master_reconciliation_drift", payload)

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
            self._mark_master_reconciliation_drift(
                {
                    "proposal_id": None,
                    "action": "CLOSE",
                    "symbol": symbol,
                },
                observed_at,
                tuple(
                    int(position.get("positionID", position.get("positionId", 0)))
                    for position in matches
                    if int(
                        position.get("positionID", position.get("positionId", 0))
                    )
                    > 0
                ),
                "DEMO close requires exactly one broker position matching the local ledger",
            )
            return None
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
            self.audit.append(
                "master_close_risk_rejected",
                {
                    "symbol": symbol,
                    "reasons": close_result.reasons,
                    "network_write_attempted": False,
                },
            )
            return None
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
        self._reconcile_master_external_close(observed_at)
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
                        self.audit.append(
                            "master_ai_noop",
                            {
                                "packet_id": decision.packet_id,
                                "reason": "demo_close_not_sealed",
                            },
                        )
                        continue
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
            if position is not None or (candidate is None and decision.intent is None):
                self.audit.append(
                    "master_ai_noop",
                    {
                        "packet_id": decision.packet_id,
                        "reason": "position_open_or_candidate_mismatch",
                    },
                )
                continue
            if candidate is not None:
                intent = self._deserialize_intent(candidate["intent"])
            else:
                direct = decision.intent or {}
                symbol = str(direct.get("symbol", "")).upper()
                if symbol not in snapshots:
                    self.audit.append(
                        "master_ai_noop",
                        {"packet_id": decision.packet_id, "reason": "direct_symbol_unavailable"},
                    )
                    continue
                intent = TradeIntent(
                    symbol=symbol,
                    side=Side(str(direct["side"])),
                    amount_usd=Decimal(str(direct["amount_usd"])),
                    confidence=decision.confidence,
                    rationale=decision.rationale,
                    stop_loss_fraction=Decimal(str(direct["stop_loss_fraction"])),
                    take_profit_fraction=Decimal(str(direct["take_profit_fraction"])),
                    leverage=1,
                    strategy_id="sol_direct",
                    strategy_version=decision.model,
                    portfolio_id=MASTER_PORTFOLIO_ID,
                    signal_ts=int(observed_at.timestamp()),
                    max_holding_seconds=int(direct["max_holding_seconds"]),
                    market_snapshot_hash=snapshots[symbol].content_hash,
                )
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
                    "decision_source": "strategy_candidate" if candidate is not None else "sol_direct",
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
            related = (
                normalized["NSDQ100"]
                if strategy.strategy_id == "spx_nasdaq_pairs_mean_reversion"
                else None
            )
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
                if not snapshot.market_open:
                    self.audit.state_set(pending_key, "")
                    self.audit.append(
                        "shadow_pending_expired",
                        {
                            "portfolio_id": portfolio_id,
                            "symbol": symbol,
                            "reason": "market_closed",
                            "research_epoch": RESEARCH_EPOCH,
                        },
                    )
                    status = "pending_expired_market_closed"
                else:
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
                        status = (
                            "shadow_filled_next_quote" if filled else "risk_rejected"
                        )
                    else:
                        _, units, _ = position
                        opposing = (units > 0 and pending.side is Side.SELL) or (
                            units < 0 and pending.side is Side.BUY
                        )
                        if opposing:
                            self._close_position(
                                self.ledger,
                                portfolio_id,
                                position,
                                snapshot,
                                observed_at,
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
                signal_tradable = bool(
                    snapshot.market_open
                    and (snapshot.quality is None or snapshot.quality.is_valid)
                )
                self.audit.append(
                    "trade_intent",
                    {
                        **asdict(intent),
                        "research_epoch": RESEARCH_EPOCH,
                        "accepted_for_execution": signal_tradable,
                    },
                )
                if not signal_tradable:
                    status = "signal_observed_market_closed"
                else:
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
                        execution_costs = costs_for_symbol(intent.symbol)
                        master_candidates.append(
                            {
                                "candidate_id": candidate_id,
                                "strategy_id": strategy.strategy_id,
                                "symbol": intent.symbol,
                                "side": intent.side.value,
                                "confidence": str(intent.confidence),
                                "estimated_round_trip_cost_bps": str(
                                    execution_costs.commission_bps * Decimal("2")
                                    + execution_costs.spread_bps
                                    + execution_costs.slippage_bps * Decimal("2")
                                ),
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
        market_events = self.news.active_events(observed_at)
        market_event_hashes = [str(item["event_hash"]) for item in market_events]
        position_review_due = False
        entry_review_fingerprint = ""
        entry_review_due = False
        if master_position is not None:
            position_snapshot = normalized[master_position[0]]
            relevant_event_hashes = [
                str(item["event_hash"])
                for item in market_events
                if master_position[0] in item.get("symbols", [])
            ]
            position_bar = canonical_hash(
                {
                    "bar": self._bar_fingerprint(position_snapshot),
                    "market_events": relevant_event_hashes,
                }
            )
            position_review_key = f"master_position_review_bar:{master_position[0]}"
            position_review_due = bool(
                position_snapshot.market_open
                and (
                    position_snapshot.quality is None
                    or position_snapshot.quality.is_valid
                )
                and self.audit.state_get(position_review_key, "") != position_bar
            )
        else:
            eligible_bars = {
                symbol: self._bar_fingerprint(snapshot)
                for symbol, snapshot in normalized.items()
                if snapshot.market_open
                and (snapshot.quality is None or snapshot.quality.is_valid)
            }
            if eligible_bars:
                entry_review_fingerprint = canonical_hash(
                    {"bars": eligible_bars, "market_events": market_event_hashes}
                )
                entry_review_due = (
                    self.audit.state_get("master_entry_review_fingerprint", "")
                    != entry_review_fingerprint
                )
        master_execution_pending = bool(
            self.audit.state_get("master_pending_execution", "")
        )
        if self.config.ai_decision_enabled and not master_execution_pending and (
            (master_position and position_review_due)
            or (master_position is None and entry_review_due)
        ):
            market_features: dict[str, object] = {}
            relevant_symbols = {
                symbol
                for symbol, snapshot in normalized.items()
                if snapshot.market_open
                and (snapshot.quality is None or snapshot.quality.is_valid)
            }
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
                "allowed_symbols": sorted(relevant_symbols),
                "intent_constraints": {
                    "max_order_notional_usd": str(self.config.risk.max_order_notional_usd),
                    "max_trade_risk_usd": str(self.config.risk.max_trade_risk_usd),
                    "min_stop_loss_fraction": str(self.config.risk.min_stop_loss_fraction),
                    "max_stop_loss_fraction": str(self.config.risk.max_stop_loss_fraction),
                    "max_leverage": self.config.risk.max_leverage,
                    "minimum_amount_usd_by_symbol": {
                        symbol: str(amount)
                        for symbol, amount in (
                            self.config.broker_minimum_amounts_usd or {}
                        ).items()
                    },
                    "minimum_stop_loss_fraction_by_symbol": {
                        symbol: str(value)
                        for symbol, value in (
                            self.config.broker_minimum_stop_fractions or {}
                        ).items()
                    },
                },
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
                "market_events": market_events,
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
                    "market_event_count": len(market_events),
                },
            )
            if master_position is not None and position_review_due:
                self.audit.state_set(position_review_key, position_bar)
            elif master_position is None and entry_review_due:
                self.audit.state_set(
                    "master_entry_review_fingerprint", entry_review_fingerprint
                )
        kill_state = self.audit.kill_state()
        self.audit.heartbeat(
            "shadow-engine",
            "healthy" if kill_state is KillState.ACTIVE else "halted",
            {
                "strategies": STRATEGY_COUNT,
                "market_event_hash": event.event_hash,
                "master_portfolio": MASTER_PORTFOLIO_ID,
                "ai_pending": len(self.ai.pending()),
                "evaluated_strategies": len(results),
                "research_epoch": RESEARCH_EPOCH,
                "kill_state": kill_state.value,
                "master_reconciliation_drift": bool(
                    self.audit.state_get("master_reconciliation_drift", "")
                ),
            },
        )
        return ShadowTickResult(
            observed_at.isoformat(), event.event_hash, tuple(results)
        )

    def collect_and_tick(self, collector: MarketDataCollector) -> ShadowTickResult:
        self.demo_client = collector.client
        snapshots: dict[str, MarketSnapshot] = {}
        for symbol, instrument_id in self.config.symbols.items():
            try:
                snapshots[symbol] = collector.collect(
                    symbol,
                    instrument_id,
                    self.config.candle_interval,
                    self.config.candle_count,
                    close_grace_seconds=self.config.candle_close_grace_seconds,
                )
            except Exception as exc:
                raise MarketCollectionFailure(symbol, exc) from exc
        fingerprints: dict[str, str] = {}
        for index, (symbol, portfolio_id) in enumerate(
            zip(STRATEGY_SYMBOLS, SHADOW_PORTFOLIO_IDS, strict=True)
        ):
            related = (
                snapshots["NSDQ100"]
                if self.strategies[index].strategy_id
                == "spx_nasdaq_pairs_mean_reversion"
                else None
            )
            fingerprint = self._bar_fingerprint(snapshots[symbol], related)
            if (
                self.audit.state_get(
                    f"shadow_last_evaluated_bar:{portfolio_id}", ""
                )
                != fingerprint
            ):
                fingerprints[portfolio_id] = fingerprint
        return self.tick(snapshots, fingerprints)

    @staticmethod
    def _engine_error_details(exc: Exception) -> dict[str, object]:
        cause = exc.cause if isinstance(exc, MarketCollectionFailure) else exc
        details: dict[str, object] = {"error_type": type(cause).__name__}
        if isinstance(exc, MarketCollectionFailure):
            details["symbol"] = exc.symbol
        if isinstance(cause, MarketDataQualityError):
            details["issue_codes"] = sorted(
                {issue.code for issue in cause.report.issues}
            )
            details["freshness_seconds"] = cause.report.freshness_seconds
        return details

    def _record_engine_failure(self, exc: Exception) -> None:
        now = datetime.now(timezone.utc)
        details = self._engine_error_details(exc)
        signature = hashlib.sha256(
            json.dumps(details, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        active_signature = self.audit.state_get("shadow_engine_error_signature", "")
        repeat_count = int(
            self.audit.state_get("shadow_engine_error_repeat_count", "0") or "0"
        )
        first_at = self.audit.state_get("shadow_engine_error_first_at", "")
        if signature != active_signature:
            repeat_count = 0
            first_at = now.isoformat()
            self.audit.state_set("shadow_engine_error_signature", signature)
            self.audit.state_set("shadow_engine_error_first_at", first_at)
        else:
            repeat_count += 1
        self.audit.state_set(
            "shadow_engine_error_repeat_count", str(repeat_count)
        )
        last_audited_raw = self.audit.state_get(
            "shadow_engine_error_last_audited_at", ""
        )
        last_audited = (
            datetime.fromisoformat(last_audited_raw).astimezone(timezone.utc)
            if last_audited_raw
            else None
        )
        should_audit = (
            signature != active_signature
            or last_audited is None
            or (now - last_audited).total_seconds()
            >= ENGINE_ERROR_AUDIT_INTERVAL_SECONDS
        )
        heartbeat_details = {
            **details,
            "signature": signature,
            "first_at": first_at,
            "repeat_count": repeat_count,
        }
        self.audit.heartbeat("shadow-engine", "error", heartbeat_details)
        if should_audit:
            self.audit.append("shadow_engine_error", heartbeat_details)
            self.audit.state_set(
                "shadow_engine_error_last_audited_at", now.isoformat()
            )

    def _record_engine_recovery(self) -> None:
        signature = self.audit.state_get("shadow_engine_error_signature", "")
        if not signature:
            return
        first_at = self.audit.state_get("shadow_engine_error_first_at", "")
        repeat_count = int(
            self.audit.state_get("shadow_engine_error_repeat_count", "0") or "0"
        )
        now = datetime.now(timezone.utc)
        duration_seconds = 0
        if first_at:
            duration_seconds = max(
                0,
                int(
                    (
                        now
                        - datetime.fromisoformat(first_at).astimezone(timezone.utc)
                    ).total_seconds()
                ),
            )
        self.audit.append(
            "shadow_engine_recovered",
            {
                "previous_signature": signature,
                "repeat_count": repeat_count,
                "duration_seconds": duration_seconds,
            },
        )
        for key in (
            "shadow_engine_error_signature",
            "shadow_engine_error_repeat_count",
            "shadow_engine_error_first_at",
            "shadow_engine_error_last_audited_at",
        ):
            self.audit.state_set(key, "")

    def run_forever(self, collector: MarketDataCollector, interval_seconds: int = 60) -> None:
        if interval_seconds < 15:
            raise ValueError("collector loop interval must be at least 15 seconds")
        while True:
            try:
                self.collect_and_tick(collector)
                self._record_engine_recovery()
            except Exception as exc:
                self._record_engine_failure(exc)
            time.sleep(interval_seconds)
