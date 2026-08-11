from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from .domain_v2 import (
    ZERO,
    ExitReason,
    Fill,
    IntentEnvelope,
    PositionState,
    PositionStatus,
    QuoteProvenance,
    Side,
)
from .exits_v2 import BarObservation, ExitContext
from .kernel_v2 import UnifiedTradingKernel
from .risk_v2 import BrokerTruth, CapitalMandate, GlobalRiskKernel
from .runtime_store_v2 import RuntimeStoreV2

BPS = Decimal("10000")


@dataclass(frozen=True)
class HistoricalBar:
    event_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal = ZERO

    def __post_init__(self) -> None:
        if self.event_time.tzinfo is None:
            raise ValueError("historical bar timestamp must be timezone-aware")
        values = (self.open, self.high, self.low, self.close, self.volume)
        if not all(value.is_finite() for value in values):
            raise ValueError("historical bar values must be finite")
        if min(self.open, self.high, self.low, self.close) <= ZERO:
            raise ValueError("historical OHLC values must be positive")
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close):
            raise ValueError("historical bar range does not contain open/close")
        if self.high < self.low or self.volume < ZERO:
            raise ValueError("historical bar range/volume is invalid")


@dataclass(frozen=True)
class KernelBacktestResult:
    starting_equity: Decimal
    ending_equity: Decimal
    pnl: Decimal
    max_drawdown_fraction: Decimal
    closed_positions: int
    fills: int
    event_chain_valid: bool
    event_count: int


SignalFactory = Callable[
    [int, Sequence[HistoricalBar], Decimal, Decimal, str], IntentEnvelope | None
]


class KernelBacktester:
    """Historical adapter that drives the same kernel/OMS/ExitEvaluator used by shadow/broker."""

    def __init__(
        self,
        mandate: CapitalMandate,
        *,
        spread_bps: Decimal = Decimal("5"),
        slippage_bps: Decimal = Decimal("2"),
        fee_bps: Decimal = ZERO,
        financing_bps_per_day: Decimal = ZERO,
    ) -> None:
        costs = (spread_bps, slippage_bps, fee_bps, financing_bps_per_day)
        if not all(value.is_finite() for value in costs) or min(costs) < ZERO:
            raise ValueError("backtest costs cannot be negative")
        self.mandate = mandate
        self.spread_bps = spread_bps
        self.slippage_bps = slippage_bps
        self.fee_bps = fee_bps
        self.financing_bps_per_day = financing_bps_per_day

    def _exit_price(self, position: PositionState, raw_price: Decimal) -> Decimal:
        impact = self.slippage_bps / BPS
        return (
            raw_price * (Decimal("1") - impact)
            if position.side is Side.BUY
            else raw_price * (Decimal("1") + impact)
        )

    def _financing(self, position: PositionState, at: datetime) -> Decimal:
        elapsed_seconds = max(
            ZERO,
            Decimal(str((at - position.entry_event_time).total_seconds())),
        )
        days = elapsed_seconds / Decimal("86400")
        notional = position.quantity * position.entry_price
        return notional * self.financing_bps_per_day / BPS * days

    @staticmethod
    def _snapshot_hash(bar: HistoricalBar, index: int) -> str:
        raw = f"{index}:{bar.event_time.isoformat()}:{bar.open}:{bar.high}:{bar.low}:{bar.close}"
        return hashlib.sha256(raw.encode()).hexdigest()

    def _quote(
        self,
        symbol: str,
        mid: Decimal,
        observed_at: datetime,
        market_hash: str,
        broker_hash: str,
        sequence: int,
    ) -> QuoteProvenance:
        half = self.spread_bps / Decimal("2") / BPS
        return QuoteProvenance(
            symbol,
            mid * (Decimal("1") - half),
            mid * (Decimal("1") + half),
            observed_at,
            observed_at,
            "historical_adapter_v2",
            str(sequence),
            market_hash,
            broker_hash,
        )

    def run(
        self,
        symbol: str,
        bars: Sequence[HistoricalBar],
        starting_equity: Decimal,
        signal_factory: SignalFactory,
        *,
        runtime_path: str | Path | None = None,
    ) -> KernelBacktestResult:
        if starting_equity <= ZERO or len(bars) < 3:
            raise ValueError("backtest requires positive equity and at least three bars")
        if any(
            right.event_time <= left.event_time for left, right in zip(bars, bars[1:], strict=False)
        ):
            raise ValueError("historical bars must be strictly time ordered")
        temporary: TemporaryDirectory[str] | None = None
        if runtime_path is None:
            temporary = TemporaryDirectory(prefix="etoro-kernel-backtest-")
            runtime_path = Path(temporary.name) / "runtime.sqlite3"
        store = RuntimeStoreV2(runtime_path)
        kernel = UnifiedTradingKernel(store, GlobalRiskKernel(self.mandate))
        pending: IntentEnvelope | None = None
        peak = starting_equity
        max_drawdown = ZERO
        period_keys: dict[str, object] = {}
        period_baselines: dict[str, Decimal] = {}
        previous_equity = starting_equity

        def period_pnl(at: datetime, equity: Decimal) -> tuple[Decimal, Decimal, Decimal]:
            nonlocal previous_equity
            keys: dict[str, object] = {
                "daily": at.date(),
                "weekly": at.isocalendar()[:2],
                "monthly": (at.year, at.month),
            }
            values: list[Decimal] = []
            for name in ("daily", "weekly", "monthly"):
                if period_keys.get(name) != keys[name]:
                    period_keys[name] = keys[name]
                    period_baselines[name] = previous_equity
                values.append(equity - period_baselines[name])
            previous_equity = equity
            return values[0], values[1], values[2]

        def economic_state(mark: Decimal) -> tuple[Decimal, Decimal, int, Decimal]:
            positions = store.positions()
            realized = sum((position.realized_pnl for position in positions), ZERO)
            unrealized = sum(
                (
                    position.pnl_at(mark)
                    for position in positions
                    if position.status is PositionStatus.OPEN
                ),
                ZERO,
            )
            gross = sum(
                (
                    position.gross_exposure(mark)
                    for position in positions
                    if position.status is PositionStatus.OPEN
                ),
                ZERO,
            )
            equity = starting_equity + realized + unrealized
            open_count = sum(position.status is PositionStatus.OPEN for position in positions)
            return equity, gross, open_count, max(ZERO, equity - gross)

        for index, bar in enumerate(bars):
            market_hash = self._snapshot_hash(bar, index)
            equity, gross, open_count, cash = economic_state(bar.open)
            broker_hash = hashlib.sha256(
                f"{index}:{equity}:{gross}:{cash}:{open_count}".encode()
            ).hexdigest()
            quote_open = self._quote(
                symbol, bar.open, bar.event_time, market_hash, broker_hash, index
            )
            daily_pnl, weekly_pnl, monthly_pnl = period_pnl(bar.event_time, equity)
            broker = BrokerTruth(
                equity_usd=equity,
                peak_equity_usd=max(peak, equity),
                available_cash_usd=cash,
                gross_exposure_usd=gross,
                correlated_exposure_usd=gross,
                open_positions=open_count,
                pending_order_notional_usd=ZERO,
                daily_pnl_usd=daily_pnl,
                weekly_pnl_usd=weekly_pnl,
                monthly_pnl_usd=monthly_pnl,
                snapshot_hash=broker_hash,
                observed_at=bar.event_time,
                reconciliation_ok=True,
            )

            # Existing exits are evaluated before next-quote entries.
            open_positions = store.positions(open_only=True)
            for position in open_positions:
                decision = kernel.evaluate_exit(
                    position,
                    ExitContext(
                        now=bar.event_time,
                        quote=quote_open,
                        bar=BarObservation(bar.event_time, bar.open, bar.high, bar.low, bar.close),
                    ),
                )
                if decision.should_exit and decision.execution_price is not None:
                    command = kernel.create_close_command(
                        position,
                        now=bar.event_time,
                        reason=decision.reason or ExitReason.STRATEGY_INVALIDATION,
                        broker=broker,
                    )
                    kernel.begin_submit(command.order_command_id, bar.event_time)
                    kernel.acknowledge(
                        command.order_command_id,
                        at=bar.event_time,
                        broker_order_id=f"sim-close-{command.order_command_id}",
                        broker_position_id=position.broker_position_id or position.position_id,
                    )
                    price = self._exit_price(position, decision.execution_price)
                    fee = position.quantity * price * self.fee_bps / BPS
                    financing = self._financing(position, bar.event_time)
                    kernel.apply_fill(
                        Fill(
                            fill_id=f"fill-{command.order_command_id}",
                            order_command_id=command.order_command_id,
                            client_order_id=command.client_order_id,
                            broker_order_id=f"sim-close-{command.order_command_id}",
                            broker_position_id=position.broker_position_id or position.position_id,
                            symbol=position.symbol,
                            side=command.side,
                            quantity=position.quantity,
                            price=price,
                            fee_usd=fee,
                            financing_usd=financing,
                            event_time=bar.event_time,
                            processing_time=bar.event_time,
                            idempotency_key=f"sim-close-fill:{command.order_command_id}",
                        ),
                        final=True,
                        exit_reason=decision.reason,
                    )

            if pending is not None and not store.positions(pending.portfolio_id, open_only=True):
                equity, gross, open_count, cash = economic_state(bar.open)
                broker_hash = hashlib.sha256(
                    f"open:{index}:{equity}:{gross}:{cash}:{open_count}".encode()
                ).hexdigest()
                quote_open = self._quote(
                    symbol, bar.open, bar.event_time, market_hash, broker_hash, index
                )
                daily_pnl, weekly_pnl, monthly_pnl = period_pnl(bar.event_time, equity)
                broker = BrokerTruth(
                    equity,
                    max(peak, equity),
                    cash,
                    gross,
                    gross,
                    open_count,
                    ZERO,
                    daily_pnl,
                    weekly_pnl,
                    monthly_pnl,
                    broker_hash,
                    bar.event_time,
                )
                risk, command = kernel.submit_open_intent(
                    pending, quote_open, broker, now=bar.event_time
                )
                if risk.approved and command is not None:
                    kernel.begin_submit(command.order_command_id, bar.event_time)
                    broker_position_id = f"sim-pos-{pending.intent_id}"
                    kernel.acknowledge(
                        command.order_command_id,
                        at=bar.event_time,
                        broker_order_id=f"sim-open-{command.order_command_id}",
                        broker_position_id=broker_position_id,
                    )
                    impact = self.slippage_bps / BPS
                    price = (
                        quote_open.ask * (Decimal("1") + impact)
                        if pending.side is Side.BUY
                        else quote_open.bid * (Decimal("1") - impact)
                    )
                    quantity = pending.amount_usd / price
                    fee = pending.amount_usd * self.fee_bps / BPS
                    kernel.apply_fill(
                        Fill(
                            fill_id=f"fill-{command.order_command_id}",
                            order_command_id=command.order_command_id,
                            client_order_id=command.client_order_id,
                            broker_order_id=f"sim-open-{command.order_command_id}",
                            broker_position_id=broker_position_id,
                            symbol=pending.symbol,
                            side=pending.side,
                            quantity=quantity,
                            price=price,
                            fee_usd=fee,
                            financing_usd=ZERO,
                            event_time=bar.event_time,
                            processing_time=bar.event_time,
                            idempotency_key=f"sim-open-fill:{command.order_command_id}",
                        ),
                        final=True,
                    )
                pending = None

            # Signal on closed bar t; execution cannot happen earlier than bar t+1.
            close_broker_hash = hashlib.sha256(f"close:{index}".encode()).hexdigest()
            close_quote = self._quote(
                symbol, bar.close, bar.event_time, market_hash, close_broker_hash, index
            )
            candidate = signal_factory(
                index, bars[: index + 1], close_quote.bid, close_quote.ask, market_hash
            )
            if candidate is not None and not store.positions(open_only=True):
                pending = candidate

            equity, _, _, _ = economic_state(bar.close)
            peak = max(peak, equity)
            if peak > ZERO:
                max_drawdown = max(max_drawdown, (peak - equity) / peak)

        # Conservative forced close at end of test through the same exit/order/fill path.
        last = bars[-1]
        for position in store.positions(open_only=True):
            market_hash = self._snapshot_hash(last, len(bars) - 1)
            broker_hash = hashlib.sha256(b"end-of-test").hexdigest()
            quote = self._quote(
                symbol, last.close, last.event_time, market_hash, broker_hash, len(bars)
            )
            decision = kernel.evaluate_exit(
                position, ExitContext(now=last.event_time, quote=quote, end_of_test=True)
            )
            if decision.should_exit and decision.execution_price is not None:
                command = kernel.create_close_command(
                    position,
                    now=last.event_time,
                    reason=ExitReason.END_OF_TEST,
                    broker=broker,
                )
                kernel.begin_submit(command.order_command_id, last.event_time)
                kernel.acknowledge(
                    command.order_command_id,
                    at=last.event_time,
                    broker_order_id=f"sim-final-{command.order_command_id}",
                    broker_position_id=position.broker_position_id or position.position_id,
                )
                kernel.apply_fill(
                    Fill(
                        fill_id=f"fill-{command.order_command_id}",
                        order_command_id=command.order_command_id,
                        client_order_id=command.client_order_id,
                        broker_order_id=f"sim-final-{command.order_command_id}",
                        broker_position_id=position.broker_position_id or position.position_id,
                        symbol=position.symbol,
                        side=command.side,
                        quantity=position.quantity,
                        price=self._exit_price(position, decision.execution_price),
                        fee_usd=(
                            position.quantity
                            * self._exit_price(position, decision.execution_price)
                            * self.fee_bps
                            / BPS
                        ),
                        financing_usd=self._financing(position, last.event_time),
                        event_time=last.event_time,
                        processing_time=last.event_time,
                        idempotency_key=f"sim-final-fill:{command.order_command_id}",
                    ),
                    final=True,
                    exit_reason=ExitReason.END_OF_TEST,
                )

        ending, _, _, _ = economic_state(last.close)
        closed = sum(position.status is PositionStatus.CLOSED for position in store.positions())
        fills = int(store.db.execute("SELECT COUNT(*) FROM v2_fills").fetchone()[0])
        event_count = int(store.db.execute("SELECT COUNT(*) FROM v2_events").fetchone()[0])
        valid = store.verify_event_chain()
        store.close()
        if temporary is not None:
            temporary.cleanup()
        return KernelBacktestResult(
            starting_equity,
            ending,
            ending - starting_equity,
            max_drawdown,
            closed,
            fills,
            valid,
            event_count,
        )
