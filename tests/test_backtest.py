from __future__ import annotations

import unittest
from decimal import Decimal

from etoro_agent.backtest import (
    ExecutionCosts,
    WalkForwardConfig,
    rank_top_strategies,
    run_backtest,
    run_walk_forward,
)
from etoro_agent.models import Side, TradeIntent
from etoro_agent.strategy import StrategyMetadata


class AlwaysSideStrategy:
    parameter_version = "1.0.0"

    def __init__(self, strategy_id: str, side: Side, amount: Decimal) -> None:
        self.strategy_id = strategy_id
        self.side = side
        self.amount = amount

    @property
    def metadata(self) -> StrategyMetadata:
        return StrategyMetadata(
            self.strategy_id,
            self.parameter_version,
            (("amount", str(self.amount)), ("side", self.side.value)),
        )

    def decide(self, symbol: str, closes: tuple[Decimal, ...]) -> TradeIntent:
        return TradeIntent(
            symbol=symbol.upper(),
            side=self.side,
            amount_usd=self.amount,
            confidence=Decimal("0.8"),
            rationale=f"strategy={self.strategy_id}",
            stop_loss_fraction=Decimal("0.50"),
            take_profit_fraction=Decimal("0.50"),
            leverage=1,
        )


class BacktestTests(unittest.TestCase):
    def test_costs_are_applied_deterministically(self) -> None:
        strategy = AlwaysSideStrategy("always_buy", Side.BUY, Decimal("100"))
        closes = [Decimal(value) for value in range(100, 111)]
        free = run_backtest(
            strategy, "AAPL", closes, Decimal("1000"), costs=ExecutionCosts(ZERO, ZERO, ZERO)
        )
        costly = run_backtest(
            strategy,
            "AAPL",
            closes,
            Decimal("1000"),
            costs=ExecutionCosts(Decimal("10"), Decimal("10"), Decimal("5")),
        )
        self.assertEqual(free.trades, 1)
        self.assertEqual(free.orders, 2)
        self.assertEqual(free.fees_paid, ZERO)
        self.assertGreater(free.ending_equity, costly.ending_equity)
        self.assertGreater(costly.fees_paid, ZERO)
        self.assertEqual(
            costly,
            run_backtest(
                strategy,
                "AAPL",
                closes,
                Decimal("1000"),
                costs=ExecutionCosts(Decimal("10"), Decimal("10"), Decimal("5")),
            ),
        )

    def test_walk_forward_is_chronological_deterministic_and_ranks_top_three(self) -> None:
        strategies = (
            AlwaysSideStrategy("buy_100", Side.BUY, Decimal("100")),
            AlwaysSideStrategy("buy_75", Side.BUY, Decimal("75")),
            AlwaysSideStrategy("buy_50", Side.BUY, Decimal("50")),
            AlwaysSideStrategy("short_100", Side.SELL, Decimal("100")),
        )
        closes = [Decimal(100 + index) for index in range(70)]
        config = WalkForwardConfig(train_bars=20, test_bars=10)
        costs = ExecutionCosts(ZERO, ZERO, ZERO)
        first = run_walk_forward(strategies, "AAPL", closes, Decimal("1000"), config, costs=costs)
        second = run_walk_forward(strategies, "AAPL", closes, Decimal("1000"), config, costs=costs)
        self.assertEqual(first, second)
        self.assertEqual(
            tuple(item.strategy_id for item in first.top_three), ("buy_100", "buy_75", "buy_50")
        )
        self.assertEqual(tuple(item.rank for item in first.rankings), (1, 2, 3, 4))
        self.assertTrue(all(len(item.folds) == 5 for item in first.rankings))
        for fold in first.rankings[0].folds:
            self.assertEqual(fold.train_end, fold.test_start)
            self.assertEqual(fold.test_end - fold.test_start, 10)
            self.assertEqual(fold.test_result.trades, 1)
        self.assertEqual(
            rank_top_strategies(strategies, "AAPL", closes, Decimal("1000"), config, costs=costs),
            first.top_three,
        )

    def test_invalid_or_overlapping_walk_forward_windows_fail_closed(self) -> None:
        with self.assertRaises(ValueError):
            WalkForwardConfig(train_bars=20, test_bars=10, step_bars=5)
        with self.assertRaises(ValueError):
            run_walk_forward(
                (AlwaysSideStrategy("buy", Side.BUY, Decimal("100")),),
                "AAPL",
                [Decimal(100)] * 20,
                Decimal("1000"),
                WalkForwardConfig(train_bars=15, test_bars=10),
            )

    def test_signal_executes_only_on_the_next_quote(self) -> None:
        strategy = AlwaysSideStrategy("next_quote", Side.BUY, Decimal("100"))
        result = run_backtest(
            strategy,
            "AAPL",
            [Decimal("100"), Decimal("200"), Decimal("200")],
            Decimal("1000"),
            costs=ExecutionCosts(ZERO, ZERO, ZERO),
        )
        self.assertEqual(result.trades, 1)
        self.assertEqual(result.ending_equity, Decimal("1000"))


ZERO = Decimal("0")


if __name__ == "__main__":
    unittest.main()
