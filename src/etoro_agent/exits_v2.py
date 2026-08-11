from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from .domain_v2 import ExitReason, PositionState, QuoteProvenance, Side, utc

ZERO = Decimal("0")


@dataclass(frozen=True)
class BarObservation:
    event_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_time", utc(self.event_time))
        if min(self.open, self.high, self.low, self.close) <= ZERO:
            raise ValueError("bar OHLC must be positive")
        if self.high < max(self.open, self.low, self.close):
            raise ValueError("bar high is invalid")
        if self.low > min(self.open, self.high, self.close):
            raise ValueError("bar low is invalid")


@dataclass(frozen=True)
class ExitContext:
    now: datetime
    quote: QuoteProvenance
    bar: BarObservation | None = None
    agent_close: bool = False
    reduce_only_forced: bool = False
    data_valid: bool = True
    strategy_invalidated: bool = False
    overnight_exit_due: bool = False
    end_of_test: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "now", utc(self.now))


@dataclass(frozen=True)
class ExitDecision:
    should_exit: bool
    reason: ExitReason | None = None
    execution_price: Decimal | None = None
    triggered_price: Decimal | None = None
    conservative_intrabar: bool = False


class ExitEvaluator:
    """Versioned deterministic exit semantics shared by replay/shadow/broker adapters."""

    version = "exit-v2.0"

    def evaluate(self, position: PositionState, context: ExitContext) -> ExitDecision:
        if position.symbol != context.quote.symbol:
            raise ValueError("position/quote symbol mismatch")
        executable = context.quote.bid if position.side is Side.BUY else context.quote.ask

        # 1. Explicit agent/manual close command.
        if context.agent_close:
            return ExitDecision(True, ExitReason.AGENT_CLOSE, executable)
        # 2. Kill/reduce-only forced exit.
        if context.reduce_only_forced:
            return ExitDecision(True, ExitReason.REDUCE_ONLY, executable)
        # 3. Data/broker invalidation policy.
        if not context.data_valid:
            return ExitDecision(True, ExitReason.DATA_INVALIDATION, executable)

        bar = context.bar
        if bar is not None:
            # 4. Gap-through-stop. Use first observable bar open, never the ideal stop.
            if position.side is Side.BUY and bar.open <= position.stop_price:
                return ExitDecision(
                    True, ExitReason.GAP_STOP, min(bar.open, executable), position.stop_price
                )
            if position.side is Side.SELL and bar.open >= position.stop_price:
                return ExitDecision(
                    True, ExitReason.GAP_STOP, max(bar.open, executable), position.stop_price
                )

            # 5/6. Stop wins when stop and target are both touched and path is unknown.
            if position.side is Side.BUY:
                stop_hit = bar.low <= position.stop_price
                target_hit = bar.high >= position.take_profit_price
            else:
                stop_hit = bar.high >= position.stop_price
                target_hit = bar.low <= position.take_profit_price
            if stop_hit:
                return ExitDecision(
                    True,
                    ExitReason.STOP_LOSS,
                    position.stop_price,
                    position.stop_price,
                    conservative_intrabar=target_hit,
                )
            if target_hit:
                return ExitDecision(
                    True,
                    ExitReason.TAKE_PROFIT,
                    position.take_profit_price,
                    position.take_profit_price,
                )
        else:
            # Tick/BBO path: trigger at the currently observable executable side.
            if position.side is Side.BUY:
                if executable <= position.stop_price:
                    return ExitDecision(True, ExitReason.STOP_LOSS, executable, position.stop_price)
                if executable >= position.take_profit_price:
                    return ExitDecision(
                        True, ExitReason.TAKE_PROFIT, executable, position.take_profit_price
                    )
            else:
                if executable >= position.stop_price:
                    return ExitDecision(True, ExitReason.STOP_LOSS, executable, position.stop_price)
                if executable <= position.take_profit_price:
                    return ExitDecision(
                        True, ExitReason.TAKE_PROFIT, executable, position.take_profit_price
                    )

        # 7. Time stop.
        if context.now >= position.expires_at:
            return ExitDecision(True, ExitReason.TIME_STOP, executable)
        # 8. Strategy invalidation/opposite signal.
        if context.strategy_invalidated:
            return ExitDecision(True, ExitReason.STRATEGY_INVALIDATION, executable)
        # 9. Financing/overnight policy.
        if context.overnight_exit_due:
            return ExitDecision(True, ExitReason.OVERNIGHT_POLICY, executable)
        # 10. End-of-test handling.
        if context.end_of_test:
            return ExitDecision(True, ExitReason.END_OF_TEST, executable)
        return ExitDecision(False)
