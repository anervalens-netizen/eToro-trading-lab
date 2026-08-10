from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from etoro_agent.config import StrategyConfig
from etoro_agent.models import Side, TradeIntent
from etoro_agent.strategy import (
    AtrShockFadeStrategy,
    BollingerRsiMeanReversionStrategy,
    BollingerSqueezeBreakoutStrategy,
    CommodityHypothesisStrategy,
    DonchianAtrBreakoutStrategy,
    EmaAdxStrategy,
    EurUsdFourHourTimeSeriesMomentumStrategy,
    FirstLastHalfHourMomentumStrategy,
    LondonBreakoutStrategy,
    MovingAverageStrategy,
    NyLondonOverlapMomentumStrategy,
    OpeningRangeBreakoutStrategy,
    OpeningRangeRetestStrategy,
    SpxNasdaqPairsMeanReversionStrategy,
    StrategyContext,
    build_strategy_suite,
)
from etoro_agent.strategy_catalog import COMMODITY_RISK_PROFILES, STRATEGY_COUNT


def decimals(*values: int | float | str) -> tuple[Decimal, ...]:
    return tuple(Decimal(str(value)) for value in values)


class StrategySuiteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = StrategyConfig(
            fast_window=3,
            slow_window=5,
            minimum_confidence=Decimal("0.55"),
            order_amount_usd=Decimal("100"),
        )

    def test_suite_has_stable_core_and_thirty_commodity_variants(self) -> None:
        suite = build_strategy_suite(self.config)
        expected = (
            "orb_15m_immediate",
            "orb_15m_retest",
            "first_30m_last_30m_momentum",
            "donchian_atr_breakout",
            "ema_9_21_adx",
            "bollinger_squeeze_breakout",
            "bollinger_rsi_mean_reversion",
            "atr_shock_fade",
            "london_breakout_eurusd",
            "ny_london_overlap_momentum_eurusd",
            "spx_nasdaq_pairs_mean_reversion",
            "eurusd_4h_time_series_momentum",
        )
        self.assertEqual(tuple(item.strategy_id for item in suite[:12]), expected)
        self.assertEqual(len(suite), STRATEGY_COUNT)
        self.assertEqual({item.parameter_version for item in suite[:12]}, {"2.0.0"})
        self.assertEqual({item.parameter_version for item in suite[12:]}, {"3.0.0"})
        self.assertEqual(len({item.metadata.fingerprint for item in suite}), STRATEGY_COUNT)
        self.assertEqual(
            tuple(item.metadata.fingerprint for item in suite),
            tuple(item.metadata.fingerprint for item in build_strategy_suite(self.config)),
        )

    def test_commodity_profiles_are_separate_deterministic_experiments(self) -> None:
        suite = build_strategy_suite(self.config)[12:]
        self.assertEqual(len(suite), 30)
        grouped: dict[str, list[CommodityHypothesisStrategy]] = {}
        for strategy in suite:
            assert isinstance(strategy, CommodityHypothesisStrategy)
            grouped.setdefault(strategy.strategy_id.rsplit("__", 1)[0], []).append(strategy)
        self.assertEqual(len(grouped), 10)
        for variants in grouped.values():
            self.assertEqual(
                {item.risk_profile.profile_id for item in variants},
                {"prudent", "balanced", "aggressive"},
            )
            self.assertEqual(
                {item.order_amount_usd for item in variants},
                {Decimal("50"), Decimal("100"), Decimal("150")},
            )

    def test_each_commodity_hypothesis_can_emit_a_bounded_intent(self) -> None:
        profile = COMMODITY_RISK_PROFILES[1]
        cases = {
            "adaptive_range": tuple([Decimal("100")] * 191 + [Decimal("90"), Decimal("91")]),
            "donchian_breakout": tuple([Decimal("100")] * 73 + [Decimal("105")]),
            "ema_trend": tuple(Decimal("100") + Decimal(index) for index in range(70)),
            "shock_fade": tuple(Decimal("100") + Decimal(index % 2) / Decimal("10") for index in range(97)) + (Decimal("110"),),
            "positive_spike_fade": tuple(Decimal("100") + Decimal(index % 2) / Decimal("10") for index in range(97)) + (Decimal("110"),),
            "squeeze_breakout": tuple(Decimal("100") + Decimal(index % 2) for index in range(80)) + tuple([Decimal("100")] * 17) + (Decimal("105"),),
        }
        for hypothesis, closes in cases.items():
            symbol = "NATGAS" if hypothesis == "positive_spike_fade" else "OIL"
            strategy = CommodityHypothesisStrategy(
                strategy_id=f"test_{hypothesis}__balanced",
                symbol=symbol,
                hypothesis=hypothesis,
                risk_profile=profile,
            )
            with self.subTest(hypothesis=hypothesis):
                intent = strategy.decide_context(StrategyContext(symbol, closes))
                self.assertIsNotNone(intent)
                assert intent is not None
                self.assertEqual(intent.amount_usd, Decimal("100"))
                self.assertLessEqual(intent.stop_loss_fraction, Decimal("0.1"))

    def test_original_moving_average_api_remains_trade_intent_compatible(self) -> None:
        strategy = MovingAverageStrategy(self.config)
        intent = strategy.decide("aapl", decimals(1, 2, 3, 4, 5))
        self.assertIsInstance(intent, TradeIntent)
        assert intent is not None
        self.assertEqual(intent.symbol, "AAPL")
        self.assertEqual(intent.side, Side.BUY)
        self.assertIn("strategy=moving_average_baseline", intent.rationale)
        self.assertIn("parameter_version=2.0.0", intent.rationale)

    def test_all_twelve_strategies_emit_deterministic_trade_intents(self) -> None:
        cases = (
            (
                OpeningRangeBreakoutStrategy(opening_bars=3, session_bars=12),
                StrategyContext("SPX500", decimals(100, 101, 99, 103)),
            ),
            (
                OpeningRangeRetestStrategy(opening_bars=3, session_bars=12),
                StrategyContext("NSDQ100", decimals(100, 101, 99, 103, "100.9", "101.05")),
            ),
            (
                FirstLastHalfHourMomentumStrategy(opening_bars=2, closing_bars=2, session_bars=12),
                StrategyContext("SPX500", decimals(100, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111)),
            ),
            (
                DonchianAtrBreakoutStrategy(lookback=5),
                StrategyContext("AAPL", decimals(100, 101, 102, 103, 104, 106)),
            ),
            (
                EmaAdxStrategy(fast_period=3, slow_period=5, trend_period=4),
                StrategyContext("AAPL", decimals(100, 101, 102, 103, 104, 105, 106)),
            ),
            (
                BollingerSqueezeBreakoutStrategy(window=20),
                StrategyContext("AAPL", tuple([Decimal("100")] * 40 + [Decimal("105")])),
            ),
            (
                BollingerRsiMeanReversionStrategy(window=20, rsi_period=14, maximum_trend_strength=Decimal("0.60")),
                StrategyContext(
                    "AAPL",
                    tuple(Decimal("100") + Decimal(index % 2) for index in range(20)) + decimals(94),
                ),
            ),
            (
                AtrShockFadeStrategy(lookback=6),
                StrategyContext("AAPL", decimals(100, "100.1", 100, "100.1", 100, "100.1", 100, 105)),
            ),
            (
                LondonBreakoutStrategy(bars_per_day=12, range_end_bar=3, trade_end_bar=6),
                StrategyContext("EURUSD", decimals("1.1000", "1.1005", "1.0995", "1.1020")),
            ),
            (
                NyLondonOverlapMomentumStrategy(bars_per_day=12, overlap_start_bar=4, overlap_end_bar=8),
                StrategyContext("EURUSD", decimals("1.1000", "1.1001", "1.1002", "1.1003", "1.1000", "1.1020")),
            ),
            (
                SpxNasdaqPairsMeanReversionStrategy(return_window=2, z_window=6, z_threshold=Decimal("1.5")),
                StrategyContext(
                    "SPX500",
                    decimals(100, 100, 100, 100, 100, 100, 100, 100, 110),
                    related_closes={"NSDQ100": decimals(100, 100, 100, 100, 100, 100, 100, 100, 100)},
                ),
            ),
            (
                EurUsdFourHourTimeSeriesMomentumStrategy(bars_per_four_hours=2, lookback_periods=2),
                StrategyContext("EURUSD", decimals("1.1000", "1.1010", "1.1020", "1.1030", "1.1040", "1.1100")),
            ),
        )
        self.assertEqual(len(cases), 12)
        for strategy, context in cases:
            with self.subTest(strategy=strategy.strategy_id):
                first = strategy.decide_context(context)
                second = strategy.decide_context(context)
                self.assertIsInstance(first, TradeIntent)
                self.assertEqual(first, second)
                assert first is not None
                self.assertEqual(first.amount_usd, Decimal("100"))
                self.assertIn(f"strategy={strategy.strategy_id}", first.rationale)
                self.assertIn(f"parameter_fingerprint={strategy.metadata.fingerprint}", first.rationale)

    def test_pair_strategy_fails_closed_without_reference_series(self) -> None:
        strategy = SpxNasdaqPairsMeanReversionStrategy(return_window=2, z_window=6)
        self.assertIsNone(strategy.decide("SPX500", decimals(100, 101, 102, 103, 104, 105, 106, 107)))

    def test_invalid_strategy_parameters_fail_closed(self) -> None:
        with self.assertRaises(ValueError):
            OpeningRangeBreakoutStrategy(opening_bars=3, session_bars=3)
        with self.assertRaises(ValueError):
            EmaAdxStrategy(fast_period=21, slow_period=9)
        with self.assertRaises(ValueError):
            SpxNasdaqPairsMeanReversionStrategy(z_window=2)

    def test_four_hour_horizon_derives_from_actual_bar_interval(self) -> None:
        start = datetime(2026, 8, 3, 0, tzinfo=timezone.utc)
        closes = tuple(Decimal("1.10") + Decimal(index) / Decimal("10000") for index in range(97))
        context = StrategyContext(
            "EURUSD",
            closes,
            timestamps=tuple(start + timedelta(minutes=15 * index) for index in range(97)),
            bar_interval_seconds=900,
        )
        intent = EurUsdFourHourTimeSeriesMomentumStrategy().decide_context(context)
        self.assertIsNotNone(intent)


if __name__ == "__main__":
    unittest.main()
