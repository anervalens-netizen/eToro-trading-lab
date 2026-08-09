from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Mapping, Protocol, Sequence

from .models import Side, TradeIntent
from .strategy import StrategyContext, StrategyMetadata


ZERO = Decimal("0")
ONE = Decimal("1")
BPS = Decimal("10000")


class StrategyLike(Protocol):
    def decide(self, symbol: str, closes: tuple[Decimal, ...]) -> TradeIntent | None: ...


@dataclass(frozen=True)
class ExecutionCosts:
    """Deterministic one-way execution assumptions in basis points."""

    commission_bps: Decimal = Decimal("1")
    spread_bps: Decimal = Decimal("2")
    slippage_bps: Decimal = Decimal("1")
    overnight_bps_per_day: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        if min(
            self.commission_bps,
            self.spread_bps,
            self.slippage_bps,
            self.overnight_bps_per_day,
        ) < ZERO:
            raise ValueError("execution costs cannot be negative")

    @property
    def price_impact_fraction(self) -> Decimal:
        return (self.spread_bps / Decimal("2") + self.slippage_bps) / BPS

    @property
    def commission_fraction(self) -> Decimal:
        return self.commission_bps / BPS


INSTRUMENT_COSTS: dict[str, ExecutionCosts] = {
    "EURUSD": ExecutionCosts(ZERO, Decimal("14"), Decimal("2"), Decimal("1")),
    "SPX500": ExecutionCosts(ZERO, Decimal("6"), Decimal("2"), Decimal("2")),
    "NSDQ100": ExecutionCosts(ZERO, Decimal("8"), Decimal("3"), Decimal("2")),
    "AAPL": ExecutionCosts(ZERO, Decimal("20"), Decimal("5"), Decimal("3")),
    "TSLA": ExecutionCosts(ZERO, Decimal("30"), Decimal("8"), Decimal("3")),
    "BTC": ExecutionCosts(Decimal("100"), Decimal("20"), Decimal("10"), ZERO),
    "ETH": ExecutionCosts(Decimal("100"), Decimal("25"), Decimal("12"), ZERO),
}


def costs_for_symbol(symbol: str) -> ExecutionCosts:
    try:
        return INSTRUMENT_COSTS[symbol.strip().upper()]
    except KeyError as exc:
        raise ValueError(f"no versioned execution-cost profile for {symbol}") from exc


@dataclass(frozen=True)
class BacktestResult:
    starting_equity: Decimal
    ending_equity: Decimal
    pnl: Decimal
    trades: int
    max_drawdown_fraction: Decimal
    fees_paid: Decimal = ZERO
    gross_pnl: Decimal = ZERO
    orders: int = 0
    winning_trades: int = 0
    strategy_id: str = "unknown"
    parameter_version: str = "unversioned"
    parameter_fingerprint: str = "unversioned"

    @property
    def return_fraction(self) -> Decimal:
        return ZERO if self.starting_equity == ZERO else self.pnl / self.starting_equity

    @property
    def win_rate(self) -> Decimal:
        return ZERO if self.trades == 0 else Decimal(self.winning_trades) / Decimal(self.trades)


@dataclass(frozen=True)
class WalkForwardConfig:
    train_bars: int
    test_bars: int
    step_bars: int | None = None

    def __post_init__(self) -> None:
        if self.train_bars <= 0 or self.test_bars <= 0:
            raise ValueError("train_bars and test_bars must be positive")
        step = self.test_bars if self.step_bars is None else self.step_bars
        if step < self.test_bars:
            raise ValueError("overlapping test windows are forbidden")

    @property
    def effective_step_bars(self) -> int:
        return self.test_bars if self.step_bars is None else self.step_bars


@dataclass(frozen=True)
class WalkForwardFold:
    fold_index: int
    train_start: int
    train_end: int
    test_start: int
    test_end: int
    train_result: BacktestResult
    test_result: BacktestResult


@dataclass(frozen=True)
class StrategyRanking:
    rank: int
    strategy_id: str
    parameter_version: str
    parameter_fingerprint: str
    score: Decimal
    starting_equity: Decimal
    ending_equity: Decimal
    pnl: Decimal
    trades: int
    max_drawdown_fraction: Decimal
    fees_paid: Decimal
    folds: tuple[WalkForwardFold, ...]


@dataclass(frozen=True)
class WalkForwardReport:
    rankings: tuple[StrategyRanking, ...]

    @property
    def top_three(self) -> tuple[StrategyRanking, ...]:
        return tuple(item for item in self.rankings if item.trades > 0)[:3]


@dataclass
class _Position:
    side: Side
    notional: Decimal
    entry_price: Decimal
    stop_loss_fraction: Decimal
    take_profit_fraction: Decimal


def load_closes(path: str | Path) -> list[Decimal]:
    with Path(path).open(newline="", encoding="utf-8") as handle:
        rows = csv.DictReader(handle)
        return [Decimal(row["close"]) for row in rows]


def _metadata(strategy: StrategyLike) -> StrategyMetadata:
    metadata = getattr(strategy, "metadata", None)
    if isinstance(metadata, StrategyMetadata):
        return metadata
    strategy_id = str(getattr(strategy, "strategy_id", type(strategy).__name__))
    parameter_version = str(getattr(strategy, "parameter_version", "unversioned"))
    return StrategyMetadata(strategy_id, parameter_version, ())


def _validate_series(closes: Sequence[Decimal], related_closes: Mapping[str, Sequence[Decimal]]) -> None:
    if any(price <= ZERO for price in closes):
        raise ValueError("close prices must be positive")
    if any(len(values) < len(closes) for values in related_closes.values()):
        raise ValueError("related close series must cover the primary series")
    if any(price <= ZERO for values in related_closes.values() for price in values[: len(closes)]):
        raise ValueError("related close prices must be positive")


def _decision(
    strategy: StrategyLike,
    symbol: str,
    closes: Sequence[Decimal],
    index: int,
    related_closes: Mapping[str, Sequence[Decimal]],
    timestamps: Sequence[datetime],
    bar_interval_seconds: int,
) -> TradeIntent | None:
    context_method = getattr(strategy, "decide_context", None)
    if callable(context_method):
        context = StrategyContext(
            symbol=symbol.upper(),
            closes=tuple(closes[: index + 1]),
            timestamps=tuple(timestamps[: index + 1]) if timestamps else (),
            related_closes={key.upper(): tuple(values[: index + 1]) for key, values in related_closes.items()},
            bar_interval_seconds=bar_interval_seconds,
        )
        return context_method(context)
    return strategy.decide(symbol, tuple(closes[: index + 1]))


def run_backtest(
    strategy: StrategyLike,
    symbol: str,
    closes: list[Decimal],
    starting_equity: Decimal,
    *,
    costs: ExecutionCosts | None = None,
    evaluation_start_index: int = 0,
    related_closes: Mapping[str, Sequence[Decimal]] | None = None,
    timestamps: Sequence[datetime] | None = None,
    opens: Sequence[Decimal] | None = None,
    highs: Sequence[Decimal] | None = None,
    lows: Sequence[Decimal] | None = None,
    bar_interval_seconds: int = 0,
) -> BacktestResult:
    """Run a deterministic next-quote backtest.

    Indicator history before `evaluation_start_index` is visible to a strategy,
    but orders cannot be placed there. This is the anti-leakage seam used by
    walk-forward tests. A signal observed on bar ``t`` can execute no earlier
    than the first supplied quote for bar ``t+1``. When OHLC is supplied, stop
    loss wins ties against take profit to avoid optimistic intrabar ordering.
    """

    if starting_equity <= ZERO:
        raise ValueError("starting_equity must be positive")
    if evaluation_start_index < 0 or evaluation_start_index > len(closes):
        raise ValueError("invalid evaluation_start_index")
    execution_costs = costs or costs_for_symbol(symbol)
    references = related_closes or {}
    time_values = timestamps or ()
    if time_values and len(time_values) < len(closes):
        raise ValueError("timestamps must cover the primary series")
    open_values = opens or ()
    high_values = highs or ()
    low_values = lows or ()
    for name, values in (("opens", open_values), ("highs", high_values), ("lows", low_values)):
        if values and len(values) < len(closes):
            raise ValueError(f"{name} must cover the primary series")
        if any(value <= ZERO for value in values[: len(closes)]):
            raise ValueError(f"{name} prices must be positive")
    if bar_interval_seconds < 0:
        raise ValueError("bar_interval_seconds cannot be negative")
    _validate_series(closes, references)
    metadata = _metadata(strategy)

    equity = starting_equity
    peak = equity
    max_drawdown = ZERO
    fees_paid = ZERO
    gross_pnl = ZERO
    orders = 0
    closed_trades = 0
    winning_trades = 0
    position: _Position | None = None
    pending_intent: TradeIntent | None = None

    def entry_price(price: Decimal, side: Side) -> Decimal:
        impact = execution_costs.price_impact_fraction
        return price * (ONE + impact if side is Side.BUY else ONE - impact)

    def exit_price(price: Decimal, side: Side) -> Decimal:
        impact = execution_costs.price_impact_fraction
        return price * (ONE - impact if side is Side.BUY else ONE + impact)

    def open_position(intent: TradeIntent, price: Decimal) -> None:
        nonlocal equity, fees_paid, orders, position
        notional = min(intent.amount_usd, max(equity, ZERO))
        if notional <= ZERO:
            return
        fee = notional * execution_costs.commission_fraction
        if fee >= equity:
            return
        equity -= fee
        fees_paid += fee
        orders += 1
        position = _Position(
            side=intent.side,
            notional=notional,
            entry_price=entry_price(price, intent.side),
            stop_loss_fraction=intent.stop_loss_fraction,
            take_profit_fraction=intent.take_profit_fraction,
        )

    def close_position(price: Decimal) -> None:
        nonlocal equity, fees_paid, gross_pnl, orders, closed_trades, winning_trades, position
        if position is None:
            return
        executed = exit_price(price, position.side)
        direction = ONE if position.side is Side.BUY else Decimal("-1")
        trade_gross = position.notional * direction * (executed / position.entry_price - ONE)
        fee = position.notional * execution_costs.commission_fraction
        equity += trade_gross - fee
        gross_pnl += trade_gross
        fees_paid += fee
        orders += 1
        closed_trades += 1
        if trade_gross - fee > ZERO:
            winning_trades += 1
        position = None

    def marked_equity(price: Decimal) -> Decimal:
        if position is None:
            return equity
        executed = exit_price(price, position.side)
        direction = ONE if position.side is Side.BUY else Decimal("-1")
        unrealized = position.notional * direction * (executed / position.entry_price - ONE)
        estimated_fee = position.notional * execution_costs.commission_fraction
        return equity + unrealized - estimated_fee

    for index, price in enumerate(closes):
        if index < evaluation_start_index:
            continue

        closed_by_limit = False
        if position is not None:
            limit_price: Decimal | None = None
            if high_values and low_values:
                if position.side is Side.BUY:
                    stop = position.entry_price * (ONE - position.stop_loss_fraction)
                    target = position.entry_price * (ONE + position.take_profit_fraction)
                    if low_values[index] <= stop:
                        limit_price = stop
                    elif high_values[index] >= target:
                        limit_price = target
                else:
                    stop = position.entry_price * (ONE + position.stop_loss_fraction)
                    target = position.entry_price * (ONE - position.take_profit_fraction)
                    if high_values[index] >= stop:
                        limit_price = stop
                    elif low_values[index] <= target:
                        limit_price = target
            else:
                direction = ONE if position.side is Side.BUY else Decimal("-1")
                raw_return = direction * (price / position.entry_price - ONE)
                if raw_return <= -position.stop_loss_fraction or raw_return >= position.take_profit_fraction:
                    limit_price = price
            if limit_price is not None:
                close_position(limit_price)
                closed_by_limit = True

        execution_quote = open_values[index] if open_values else price
        if not closed_by_limit and pending_intent is not None:
            if position is None:
                open_position(pending_intent, execution_quote)
            elif pending_intent.side is not position.side:
                close_position(execution_quote)

        pending_intent = _decision(
            strategy,
            symbol,
            closes,
            index,
            references,
            time_values,
            bar_interval_seconds,
        )

        marked = marked_equity(price)
        peak = max(peak, marked)
        if peak > ZERO:
            max_drawdown = max(max_drawdown, (peak - marked) / peak)

    if position is not None and closes and evaluation_start_index < len(closes):
        close_position(closes[-1])
        marked = equity
        peak = max(peak, marked)
        if peak > ZERO:
            max_drawdown = max(max_drawdown, (peak - marked) / peak)

    return BacktestResult(
        starting_equity=starting_equity,
        ending_equity=equity,
        pnl=equity - starting_equity,
        trades=closed_trades,
        max_drawdown_fraction=max_drawdown,
        fees_paid=fees_paid,
        gross_pnl=gross_pnl,
        orders=orders,
        winning_trades=winning_trades,
        strategy_id=metadata.strategy_id,
        parameter_version=metadata.parameter_version,
        parameter_fingerprint=metadata.fingerprint,
    )


def run_walk_forward(
    strategies: Sequence[StrategyLike],
    symbol: str,
    closes: list[Decimal],
    starting_equity: Decimal,
    config: WalkForwardConfig,
    *,
    costs: ExecutionCosts | None = None,
    related_closes: Mapping[str, Sequence[Decimal]] | None = None,
    timestamps: Sequence[datetime] | None = None,
) -> WalkForwardReport:
    """Evaluate fixed parameter versions on chronological, non-overlapping OOS folds."""

    if len(closes) < config.train_bars + config.test_bars:
        raise ValueError("not enough bars for one walk-forward fold")
    identities = [(_metadata(strategy).strategy_id, _metadata(strategy).parameter_version) for strategy in strategies]
    if len(set(identities)) != len(identities):
        raise ValueError("duplicate strategy id and parameter version")

    references = related_closes or {}
    time_values = timestamps or ()
    unranked: list[StrategyRanking] = []
    for strategy in strategies:
        metadata = _metadata(strategy)
        equity = starting_equity
        folds: list[WalkForwardFold] = []
        fold_index = 0
        test_start = config.train_bars
        while test_start + config.test_bars <= len(closes):
            train_start = test_start - config.train_bars
            train_end = test_start
            test_end = test_start + config.test_bars
            fold_closes = closes[train_start:test_end]
            fold_references = {key: values[train_start:test_end] for key, values in references.items()}
            fold_timestamps = time_values[train_start:test_end] if time_values else ()
            train_result = run_backtest(
                strategy,
                symbol,
                closes[train_start:train_end],
                starting_equity,
                costs=costs,
                related_closes={key: values[train_start:train_end] for key, values in references.items()},
                timestamps=time_values[train_start:train_end] if time_values else (),
            )
            test_result = run_backtest(
                strategy,
                symbol,
                list(fold_closes),
                equity,
                costs=costs,
                evaluation_start_index=config.train_bars,
                related_closes=fold_references,
                timestamps=fold_timestamps,
            )
            equity = test_result.ending_equity
            folds.append(
                WalkForwardFold(
                    fold_index=fold_index,
                    train_start=train_start,
                    train_end=train_end,
                    test_start=test_start,
                    test_end=test_end,
                    train_result=train_result,
                    test_result=test_result,
                )
            )
            fold_index += 1
            test_start += config.effective_step_bars

        pnl = equity - starting_equity
        max_drawdown = ZERO
        aggregate_peak = starting_equity
        for fold in folds:
            max_drawdown = max(max_drawdown, fold.test_result.max_drawdown_fraction)
            aggregate_peak = max(aggregate_peak, fold.test_result.ending_equity)
            if aggregate_peak > ZERO:
                max_drawdown = max(
                    max_drawdown,
                    (aggregate_peak - fold.test_result.ending_equity) / aggregate_peak,
                )
        total_fees = sum((fold.test_result.fees_paid for fold in folds), ZERO)
        total_trades = sum(fold.test_result.trades for fold in folds)
        return_fraction = pnl / starting_equity
        score = return_fraction - max_drawdown
        unranked.append(
            StrategyRanking(
                rank=0,
                strategy_id=metadata.strategy_id,
                parameter_version=metadata.parameter_version,
                parameter_fingerprint=metadata.fingerprint,
                score=score,
                starting_equity=starting_equity,
                ending_equity=equity,
                pnl=pnl,
                trades=total_trades,
                max_drawdown_fraction=max_drawdown,
                fees_paid=total_fees,
                folds=tuple(folds),
            )
        )

    ordered = sorted(
        unranked,
        key=lambda item: (
            item.trades == 0,
            -item.score,
            -item.pnl,
            item.max_drawdown_fraction,
            item.strategy_id,
            item.parameter_version,
        ),
    )
    rankings = tuple(
        StrategyRanking(
            rank=index,
            strategy_id=item.strategy_id,
            parameter_version=item.parameter_version,
            parameter_fingerprint=item.parameter_fingerprint,
            score=item.score,
            starting_equity=item.starting_equity,
            ending_equity=item.ending_equity,
            pnl=item.pnl,
            trades=item.trades,
            max_drawdown_fraction=item.max_drawdown_fraction,
            fees_paid=item.fees_paid,
            folds=item.folds,
        )
        for index, item in enumerate(ordered, start=1)
    )
    return WalkForwardReport(rankings)


def rank_top_strategies(
    strategies: Sequence[StrategyLike],
    symbol: str,
    closes: list[Decimal],
    starting_equity: Decimal,
    config: WalkForwardConfig,
    *,
    costs: ExecutionCosts | None = None,
    related_closes: Mapping[str, Sequence[Decimal]] | None = None,
    timestamps: Sequence[datetime] | None = None,
) -> tuple[StrategyRanking, ...]:
    """Convenience API returning only the deterministic top-three ranking."""

    return run_walk_forward(
        strategies,
        symbol,
        closes,
        starting_equity,
        config,
        costs=costs,
        related_closes=related_closes,
        timestamps=timestamps,
    ).top_three
