from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from .domain_v2 import ONE, ZERO, Side
from .strategy_v2 import FamilySignal, StrategyFamily

PROVISIONAL_ROUND_TRIP_COST_BPS: dict[str, Decimal] = {
    "EURUSD": Decimal("18"),
    "SPX500": Decimal("10"),
    "NSDQ100": Decimal("14"),
    "AAPL": Decimal("35"),
    "TSLA": Decimal("50"),
    "BTC": Decimal("45"),
    "ETH": Decimal("55"),
    "OIL": Decimal("40"),
    "NATGAS": Decimal("75"),
}


def _mean(values: Sequence[Decimal]) -> Decimal:
    return sum(values, ZERO) / Decimal(len(values))


def _std(values: Sequence[Decimal]) -> Decimal:
    average = _mean(values)
    return (sum(((value - average) ** 2 for value in values), ZERO) / Decimal(len(values))).sqrt()


def trend_breakout_signal(
    symbol: str, closes: Sequence[Decimal], highs: Sequence[Decimal], lows: Sequence[Decimal]
) -> FamilySignal | None:
    if len(closes) < 22 or len(highs) < len(closes) or len(lows) < len(closes):
        return None
    prior_high = max(highs[-21:-1])
    prior_low = min(lows[-21:-1])
    price = closes[-1]
    if price > prior_high:
        side = Side.BUY
        distance = price / prior_high - ONE
    elif price < prior_low:
        side = Side.SELL
        distance = ONE - price / prior_low
    else:
        return None
    raw = min(Decimal("0.95"), Decimal("0.55") + distance * Decimal("30"))
    return FamilySignal(
        StrategyFamily.TREND_BREAKOUT,
        "trend-breakout-v2.1",
        symbol.upper(),
        side,
        raw,
        Decimal("0.60"),
        Decimal("0.015"),
        Decimal("0.03"),
        8 * 3600,
        f"donchian20_distance={distance}",
    )


def session_momentum_signal(symbol: str, closes: Sequence[Decimal]) -> FamilySignal | None:
    if len(closes) < 8 or closes[-5] <= ZERO:
        return None
    move = closes[-1] / closes[-5] - ONE
    if abs(move) < Decimal("0.002"):
        return None
    raw = min(Decimal("0.90"), Decimal("0.52") + abs(move) * Decimal("35"))
    return FamilySignal(
        StrategyFamily.SESSION_MOMENTUM,
        "session-momentum-v2.1",
        symbol.upper(),
        Side.BUY if move > ZERO else Side.SELL,
        raw,
        Decimal("0.60"),
        Decimal("0.012"),
        Decimal("0.025"),
        4 * 3600,
        f"four_bar_move={move}",
    )


def mean_reversion_signal(symbol: str, closes: Sequence[Decimal]) -> FamilySignal | None:
    if len(closes) < 22:
        return None
    window = closes[-20:]
    deviation = _std(window)
    if deviation == ZERO:
        return None
    z = (closes[-1] - _mean(window)) / deviation
    latest = closes[-1] / closes[-2] - ONE if closes[-2] > ZERO else ZERO
    if z <= Decimal("-1.8") and latest >= ZERO:
        side = Side.BUY
    elif z >= Decimal("1.8") and latest <= ZERO:
        side = Side.SELL
    else:
        return None
    raw = min(Decimal("0.92"), Decimal("0.52") + min(abs(z), Decimal("4")) * Decimal("0.09"))
    return FamilySignal(
        StrategyFamily.REGIME_MEAN_REVERSION,
        "regime-mean-reversion-v2.1",
        symbol.upper(),
        side,
        raw,
        Decimal("0.62"),
        Decimal("0.012"),
        Decimal("0.022"),
        6 * 3600,
        f"zscore={z};latest_return={latest}",
    )


def statistical_baseline_signal(symbol: str, closes: Sequence[Decimal]) -> FamilySignal | None:
    if len(closes) < 33 or closes[-33] <= ZERO:
        return None
    move = closes[-1] / closes[-33] - ONE
    mean_absolute_return = _mean(
        [
            abs(closes[index] / closes[index - 1] - ONE)
            for index in range(len(closes) - 32, len(closes))
        ]
    )
    if mean_absolute_return == ZERO or abs(move) < mean_absolute_return * Decimal("2"):
        return None
    normalized = abs(move) / mean_absolute_return
    raw = min(
        Decimal("0.99"),
        Decimal("0.50") + min(normalized, Decimal("8")) * Decimal("0.05"),
    )
    return FamilySignal(
        StrategyFamily.STATISTICAL_BASELINE,
        "statistical-baseline-v2.1",
        symbol.upper(),
        Side.BUY if move > ZERO else Side.SELL,
        raw,
        Decimal("0.60"),
        Decimal("0.02"),
        Decimal("0.04"),
        48 * 3600,
        (
            f"lookback_return={move};lookback_return_bps={move * Decimal('10000')};"
            f"mean_abs_bar_return={mean_absolute_return};"
            f"mean_abs_bar_return_bps={mean_absolute_return * Decimal('10000')}"
        ),
    )


def payoff_proxy_bps(signal: FamilySignal) -> Decimal:
    """Non-probabilistic tradability proxy; raw_confidence is not a calibrated probability."""

    p = signal.raw_confidence
    return (p * signal.take_fraction - (ONE - p) * signal.stop_fraction) * Decimal("10000")


def viable_net_of_cost(signal: FamilySignal, *, stress_multiple: Decimal = Decimal("1.5")) -> bool:
    if not signal.actionable:
        return False
    cost = PROVISIONAL_ROUND_TRIP_COST_BPS.get(signal.symbol.upper(), Decimal("100"))
    return payoff_proxy_bps(signal) > cost * stress_multiple


def generate_core_signals(
    symbol: str,
    closes: Sequence[Decimal],
    highs: Sequence[Decimal],
    lows: Sequence[Decimal],
) -> tuple[FamilySignal, ...]:
    candidates = (
        trend_breakout_signal(symbol, closes, highs, lows),
        session_momentum_signal(symbol, closes),
        mean_reversion_signal(symbol, closes),
        statistical_baseline_signal(symbol, closes),
    )
    return tuple(
        signal for signal in candidates if signal is not None and viable_net_of_cost(signal)
    )
