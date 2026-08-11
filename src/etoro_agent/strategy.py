from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, time
from decimal import Decimal
from typing import ClassVar
from zoneinfo import ZoneInfo

from .config import StrategyConfig
from .models import Side, TradeIntent
from .strategy_catalog import (
    COMMODITY_HYPOTHESES,
    COMMODITY_RISK_PROFILES,
    STRATEGY_PORTFOLIO_BY_ID,
    CommodityRiskProfile,
)

ZERO = Decimal("0")
ONE = Decimal("1")


@dataclass(frozen=True)
class StrategyMetadata:
    """Stable identity for reproducible experiments and audit records."""

    strategy_id: str
    parameter_version: str
    parameters: tuple[tuple[str, str], ...]

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(
            {
                "parameter_version": self.parameter_version,
                "parameters": self.parameters,
                "strategy_id": self.strategy_id,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()[:16]


@dataclass(frozen=True)
class StrategyContext:
    """Immutable market view supplied to a deterministic strategy.

    `timestamps` and `related_closes` are optional so the original
    `decide(symbol, closes)` API remains usable. Session strategies assume the
    first bar is session-aligned when timestamps are absent. Pair strategies
    fail closed when their reference series is absent.
    """

    symbol: str
    closes: tuple[Decimal, ...]
    highs: tuple[Decimal, ...] = ()
    lows: tuple[Decimal, ...] = ()
    timestamps: tuple[datetime, ...] = ()
    related_closes: Mapping[str, tuple[Decimal, ...]] = field(default_factory=dict)
    bar_interval_seconds: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", self.symbol.strip().upper())
        object.__setattr__(self, "closes", tuple(self.closes))
        object.__setattr__(self, "highs", tuple(self.highs))
        object.__setattr__(self, "lows", tuple(self.lows))
        object.__setattr__(self, "timestamps", tuple(self.timestamps))
        object.__setattr__(
            self,
            "related_closes",
            {key.strip().upper(): tuple(values) for key, values in self.related_closes.items()},
        )
        if self.bar_interval_seconds < 0:
            raise ValueError("bar_interval_seconds cannot be negative")
        if self.timestamps and len(self.timestamps) != len(self.closes):
            raise ValueError("timestamps must align with closes")


def _clamp(value: Decimal, lower: Decimal, upper: Decimal) -> Decimal:
    return min(upper, max(lower, value))


def _mean(values: tuple[Decimal, ...]) -> Decimal:
    return sum(values, ZERO) / Decimal(len(values))


def _stddev(values: tuple[Decimal, ...]) -> Decimal:
    if not values:
        return ZERO
    average = _mean(values)
    variance = sum(((value - average) ** 2 for value in values), ZERO) / Decimal(len(values))
    return variance.sqrt()


def _quantile(values: tuple[Decimal, ...], fraction: Decimal) -> Decimal:
    if not values:
        return ZERO
    ordered = tuple(sorted(values))
    position = int((Decimal(len(ordered) - 1) * fraction).to_integral_value())
    return ordered[max(0, min(position, len(ordered) - 1))]


def _ema(values: tuple[Decimal, ...], period: int) -> Decimal:
    seed = _mean(values[:period])
    alpha = Decimal(2) / Decimal(period + 1)
    result = seed
    for value in values[period:]:
        result = value * alpha + result * (ONE - alpha)
    return result


def _rsi(values: tuple[Decimal, ...], period: int) -> Decimal:
    changes = tuple(
        values[index] - values[index - 1] for index in range(len(values) - period, len(values))
    )
    gains = sum((max(change, ZERO) for change in changes), ZERO)
    losses = sum((max(-change, ZERO) for change in changes), ZERO)
    if losses == ZERO:
        return Decimal("100") if gains > ZERO else Decimal("50")
    relative_strength = gains / losses
    return Decimal("100") - Decimal("100") / (ONE + relative_strength)


def _mean_absolute_return(values: tuple[Decimal, ...], period: int) -> Decimal:
    returns: list[Decimal] = []
    for index in range(len(values) - period, len(values)):
        previous = values[index - 1]
        if previous > ZERO:
            returns.append(abs(values[index] / previous - ONE))
    return _mean(tuple(returns)) if returns else ZERO


def _trend_strength(values: tuple[Decimal, ...], period: int) -> Decimal:
    """Close-only, bounded directional-efficiency proxy for ADX."""

    changes = tuple(
        values[index] - values[index - 1] for index in range(len(values) - period, len(values))
    )
    total_move = sum((abs(change) for change in changes), ZERO)
    return ZERO if total_move == ZERO else abs(sum(changes, ZERO)) / total_move


def _bar_of_day(context: StrategyContext, bars_per_day: int) -> int:
    if context.timestamps and len(context.timestamps) == len(context.closes):
        timestamp = context.timestamps[-1]
        minutes = timestamp.hour * 60 + timestamp.minute
        return (minutes * bars_per_day) // (24 * 60)
    return (len(context.closes) - 1) % bars_per_day


def _session_values(context: StrategyContext, bars_per_day: int) -> tuple[Decimal, ...]:
    position = _bar_of_day(context, bars_per_day)
    start = len(context.closes) - position - 1
    return context.closes[start:]


def _clock_session(
    context: StrategyContext,
    timezone_name: str,
    session_open: time,
    session_close: time,
) -> tuple[tuple[datetime, Decimal], ...]:
    """Return today's timestamped session; empty means use legacy bar fallback."""

    if not context.timestamps:
        return ()
    zone = ZoneInfo(timezone_name)
    localized = tuple(timestamp.astimezone(zone) for timestamp in context.timestamps)
    session_day = localized[-1].date()
    return tuple(
        (timestamp, close)
        for timestamp, close in zip(localized, context.closes, strict=True)
        if timestamp.date() == session_day
        and session_open <= timestamp.timetz().replace(tzinfo=None) < session_close
    )


def _minutes_since_midnight(value: datetime) -> int:
    return value.hour * 60 + value.minute


class DeterministicStrategy:
    strategy_id: ClassVar[str]
    parameter_version: ClassVar[str] = "2.0.0"
    max_holding_seconds: ClassVar[int] = 86_400

    def __init__(
        self,
        order_amount_usd: Decimal = Decimal("100"),
        minimum_confidence: Decimal = Decimal("0.55"),
    ) -> None:
        if order_amount_usd <= ZERO:
            raise ValueError("order_amount_usd must be positive")
        if not ZERO <= minimum_confidence <= ONE:
            raise ValueError("minimum_confidence must be between zero and one")
        self.order_amount_usd = order_amount_usd
        self.minimum_confidence = minimum_confidence

    def parameter_items(self) -> tuple[tuple[str, str], ...]:
        return (
            ("minimum_confidence", str(self.minimum_confidence)),
            ("order_amount_usd", str(self.order_amount_usd)),
        )

    @property
    def metadata(self) -> StrategyMetadata:
        return StrategyMetadata(
            strategy_id=self.strategy_id,
            parameter_version=self.parameter_version,
            parameters=tuple(sorted(self.parameter_items())),
        )

    def decide(self, symbol: str, closes: tuple[Decimal, ...]) -> TradeIntent | None:
        return self.decide_context(StrategyContext(symbol=symbol.upper(), closes=tuple(closes)))

    def decide_context(self, context: StrategyContext) -> TradeIntent | None:
        raise NotImplementedError

    def _intent(
        self,
        context: StrategyContext,
        side: Side,
        raw_confidence: Decimal,
        stop_loss_fraction: Decimal,
        take_profit_fraction: Decimal,
        metrics: Mapping[str, Decimal | int | str],
    ) -> TradeIntent:
        confidence = _clamp(max(raw_confidence, self.minimum_confidence), ZERO, Decimal("0.99"))
        ordered_metrics = ";".join(f"{key}={metrics[key]}" for key in sorted(metrics))
        metadata = self.metadata
        rationale = (
            f"strategy={metadata.strategy_id};parameter_version={metadata.parameter_version};"
            f"parameter_fingerprint={metadata.fingerprint};{ordered_metrics}"
        )
        snapshot_payload = json.dumps(
            {
                "symbol": context.symbol,
                "closes": [str(value) for value in context.closes[-256:]],
                "timestamp": context.timestamps[-1].isoformat() if context.timestamps else None,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        signal_ts = (
            int(context.timestamps[-1].timestamp())
            if context.timestamps and context.timestamps[-1].tzinfo is not None
            else 0
        )
        return TradeIntent(
            symbol=context.symbol.upper(),
            side=side,
            amount_usd=self.order_amount_usd,
            confidence=confidence,
            rationale=rationale,
            stop_loss_fraction=stop_loss_fraction,
            take_profit_fraction=take_profit_fraction,
            leverage=1,
            strategy_id=metadata.strategy_id,
            strategy_version=metadata.parameter_version,
            portfolio_id=STRATEGY_PORTFOLIO_BY_ID.get(metadata.strategy_id, "shadow-legacy"),
            signal_ts=signal_ts,
            max_holding_seconds=self.max_holding_seconds,
            market_snapshot_hash=hashlib.sha256(snapshot_payload).hexdigest(),
        )


class MovingAverageStrategy(DeterministicStrategy):
    """Backward-compatible deterministic baseline used by the current agent."""

    strategy_id = "moving_average_baseline"

    def __init__(self, config: StrategyConfig) -> None:
        if config.fast_window <= 0 or config.slow_window <= config.fast_window:
            raise ValueError("invalid moving-average windows")
        self.config = config
        super().__init__(config.order_amount_usd, config.minimum_confidence)

    def parameter_items(self) -> tuple[tuple[str, str], ...]:
        return super().parameter_items() + (
            ("fast_window", str(self.config.fast_window)),
            ("slow_window", str(self.config.slow_window)),
        )

    def decide_context(self, context: StrategyContext) -> TradeIntent | None:
        closes = context.closes
        if len(closes) < self.config.slow_window:
            return None
        fast = _mean(closes[-self.config.fast_window :])
        slow = _mean(closes[-self.config.slow_window :])
        if slow <= ZERO:
            return None
        strength = abs(fast - slow) / slow
        confidence = min(ONE, Decimal("0.5") + strength * Decimal("10"))
        if confidence < self.minimum_confidence or fast <= slow:
            return None
        return self._intent(
            context,
            Side.BUY,
            confidence,
            Decimal("0.03"),
            Decimal("0.06"),
            {"fast": fast, "slow": slow},
        )


class OpeningRangeBreakoutStrategy(DeterministicStrategy):
    strategy_id = "orb_15m_immediate"

    def __init__(
        self,
        *args: object,
        opening_bars: int = 3,
        session_bars: int = 78,
        buffer: Decimal = Decimal("0.0005"),
        **kwargs: object,
    ) -> None:
        super().__init__(*args, **kwargs)
        if opening_bars <= 0 or session_bars <= opening_bars or buffer < ZERO:
            raise ValueError("invalid opening-range parameters")
        self.opening_bars = opening_bars
        self.session_bars = session_bars
        self.buffer = buffer

    def parameter_items(self) -> tuple[tuple[str, str], ...]:
        return super().parameter_items() + (
            ("buffer", str(self.buffer)),
            ("opening_bars", str(self.opening_bars)),
            ("session_bars", str(self.session_bars)),
        )

    def decide_context(self, context: StrategyContext) -> TradeIntent | None:
        if context.symbol not in {"NSDQ100", "SPX500"}:
            return None
        timed = _clock_session(context, "America/New_York", time(9, 30), time(16, 0))
        if timed:
            latest_minute = _minutes_since_midnight(timed[-1][0])
            opening = tuple(
                value for stamp, value in timed if _minutes_since_midnight(stamp) < 9 * 60 + 45
            )
            session = tuple(value for _, value in timed)
            if latest_minute < 9 * 60 + 45:
                return None
        else:
            session = _session_values(context, self.session_bars)
            opening = session[: self.opening_bars]
        if len(session) <= len(opening) or not opening:
            return None
        high, low, price = max(opening), min(opening), session[-1]
        if high <= ZERO or low <= ZERO:
            return None
        if price > high * (ONE + self.buffer):
            side, distance = Side.BUY, price / high - ONE
        elif price < low * (ONE - self.buffer):
            side, distance = Side.SELL, ONE - price / low
        else:
            return None
        return self._intent(
            context,
            side,
            Decimal("0.55") + distance * Decimal("20"),
            Decimal("0.015"),
            Decimal("0.03"),
            {"distance": distance, "opening_high": high, "opening_low": low},
        )


class OpeningRangeRetestStrategy(DeterministicStrategy):
    strategy_id = "orb_15m_retest"

    def __init__(
        self,
        *args: object,
        opening_bars: int = 3,
        session_bars: int = 78,
        breakout_buffer: Decimal = Decimal("0.001"),
        retest_tolerance: Decimal = Decimal("0.0015"),
        **kwargs: object,
    ) -> None:
        super().__init__(*args, **kwargs)
        if (
            opening_bars <= 0
            or session_bars <= opening_bars
            or min(breakout_buffer, retest_tolerance) < ZERO
        ):
            raise ValueError("invalid opening-range retest parameters")
        self.opening_bars = opening_bars
        self.session_bars = session_bars
        self.breakout_buffer = breakout_buffer
        self.retest_tolerance = retest_tolerance

    def parameter_items(self) -> tuple[tuple[str, str], ...]:
        return super().parameter_items() + (
            ("breakout_buffer", str(self.breakout_buffer)),
            ("opening_bars", str(self.opening_bars)),
            ("retest_tolerance", str(self.retest_tolerance)),
            ("session_bars", str(self.session_bars)),
        )

    def decide_context(self, context: StrategyContext) -> TradeIntent | None:
        if context.symbol not in {"NSDQ100", "SPX500"}:
            return None
        timed = _clock_session(context, "America/New_York", time(9, 30), time(16, 0))
        if timed:
            latest_minute = _minutes_since_midnight(timed[-1][0])
            opening_count = sum(
                1 for stamp, _ in timed if _minutes_since_midnight(stamp) < 9 * 60 + 45
            )
            session = tuple(value for _, value in timed)
            if latest_minute < 9 * 60 + 45:
                return None
        else:
            session = _session_values(context, self.session_bars)
            opening_count = self.opening_bars
        if len(session) <= opening_count + 2:
            return None
        opening = session[:opening_count]
        prior, price, previous = session[opening_count:-1], session[-1], session[-2]
        high, low = max(opening), min(opening)
        bullish_break = max(prior) > high * (ONE + self.breakout_buffer)
        bearish_break = min(prior) < low * (ONE - self.breakout_buffer)
        if (
            bullish_break
            and high <= price <= high * (ONE + self.retest_tolerance)
            and price > previous
        ):
            side, distance = Side.BUY, price / high - ONE
        elif (
            bearish_break
            and low * (ONE - self.retest_tolerance) <= price <= low
            and price < previous
        ):
            side, distance = Side.SELL, ONE - price / low
        else:
            return None
        return self._intent(
            context,
            side,
            Decimal("0.60") + abs(distance) * Decimal("20"),
            Decimal("0.012"),
            Decimal("0.03"),
            {"opening_high": high, "opening_low": low, "retest_distance": distance},
        )


class FirstLastHalfHourMomentumStrategy(DeterministicStrategy):
    strategy_id = "first_30m_last_30m_momentum"

    def __init__(
        self,
        *args: object,
        opening_bars: int = 6,
        closing_bars: int = 6,
        session_bars: int = 78,
        minimum_move: Decimal = Decimal("0.002"),
        **kwargs: object,
    ) -> None:
        super().__init__(*args, **kwargs)
        if (
            opening_bars <= 0
            or closing_bars <= 0
            or opening_bars + closing_bars > session_bars
            or minimum_move < ZERO
        ):
            raise ValueError("invalid first/last momentum parameters")
        self.opening_bars = opening_bars
        self.closing_bars = closing_bars
        self.session_bars = session_bars
        self.minimum_move = minimum_move

    def parameter_items(self) -> tuple[tuple[str, str], ...]:
        return super().parameter_items() + (
            ("closing_bars", str(self.closing_bars)),
            ("minimum_move", str(self.minimum_move)),
            ("opening_bars", str(self.opening_bars)),
            ("session_bars", str(self.session_bars)),
        )

    def decide_context(self, context: StrategyContext) -> TradeIntent | None:
        if context.symbol not in {"NSDQ100", "SPX500"}:
            return None
        timed = _clock_session(context, "America/New_York", time(9, 30), time(16, 0))
        if timed:
            position = _minutes_since_midnight(timed[-1][0]) - (9 * 60 + 30)
            opening = tuple(
                value for stamp, value in timed if _minutes_since_midnight(stamp) < 10 * 60
            )
            if not 360 <= position < 390 or not opening:
                return None
            opening_return = opening[-1] / opening[0] - ONE
        else:
            position = _bar_of_day(context, self.session_bars)
            session = _session_values(context, self.session_bars)
            if position < self.session_bars - self.closing_bars or len(session) < self.opening_bars:
                return None
            opening_return = session[self.opening_bars - 1] / session[0] - ONE
        if abs(opening_return) < self.minimum_move:
            return None
        side = Side.BUY if opening_return > ZERO else Side.SELL
        return self._intent(
            context,
            side,
            Decimal("0.55") + abs(opening_return) * Decimal("15"),
            Decimal("0.015"),
            Decimal("0.025"),
            {"opening_return": opening_return, "session_position": position},
        )


class DonchianAtrBreakoutStrategy(DeterministicStrategy):
    strategy_id = "donchian_atr_breakout"

    def __init__(
        self,
        *args: object,
        lookback: int = 20,
        breakout_buffer: Decimal = Decimal("0.0005"),
        **kwargs: object,
    ) -> None:
        super().__init__(*args, **kwargs)
        if lookback < 2 or breakout_buffer < ZERO:
            raise ValueError("invalid Donchian parameters")
        self.lookback = lookback
        self.breakout_buffer = breakout_buffer

    def parameter_items(self) -> tuple[tuple[str, str], ...]:
        return super().parameter_items() + (
            ("breakout_buffer", str(self.breakout_buffer)),
            ("lookback", str(self.lookback)),
        )

    def decide_context(self, context: StrategyContext) -> TradeIntent | None:
        closes = context.closes
        if len(closes) <= self.lookback:
            return None
        prior, price = closes[-self.lookback - 1 : -1], closes[-1]
        high, low = max(prior), min(prior)
        atr_fraction = _mean_absolute_return(closes[:-1], min(self.lookback, len(closes) - 2))
        if price > high * (ONE + self.breakout_buffer):
            side, distance = Side.BUY, price / high - ONE
        elif price < low * (ONE - self.breakout_buffer):
            side, distance = Side.SELL, ONE - price / low
        else:
            return None
        stop = _clamp(atr_fraction * Decimal("2"), Decimal("0.01"), Decimal("0.04"))
        return self._intent(
            context,
            side,
            Decimal("0.55") + distance * Decimal("15"),
            stop,
            stop * Decimal("2"),
            {"atr_fraction": atr_fraction, "channel_high": high, "channel_low": low},
        )


class EmaAdxStrategy(DeterministicStrategy):
    strategy_id = "ema_9_21_adx"

    def __init__(
        self,
        *args: object,
        fast_period: int = 9,
        slow_period: int = 21,
        trend_period: int = 14,
        minimum_trend_strength: Decimal = Decimal("0.30"),
        **kwargs: object,
    ) -> None:
        super().__init__(*args, **kwargs)
        if (
            fast_period <= 0
            or slow_period <= fast_period
            or trend_period <= 0
            or not ZERO <= minimum_trend_strength <= ONE
        ):
            raise ValueError("invalid EMA/ADX parameters")
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.trend_period = trend_period
        self.minimum_trend_strength = minimum_trend_strength

    def parameter_items(self) -> tuple[tuple[str, str], ...]:
        return super().parameter_items() + (
            ("fast_period", str(self.fast_period)),
            ("minimum_trend_strength", str(self.minimum_trend_strength)),
            ("slow_period", str(self.slow_period)),
            ("trend_period", str(self.trend_period)),
        )

    def decide_context(self, context: StrategyContext) -> TradeIntent | None:
        closes = context.closes
        required = max(self.slow_period, self.trend_period + 1)
        if len(closes) < required:
            return None
        fast, slow = _ema(closes, self.fast_period), _ema(closes, self.slow_period)
        trend = _trend_strength(closes, self.trend_period)
        if slow <= ZERO or trend < self.minimum_trend_strength or fast == slow:
            return None
        side = Side.BUY if fast > slow else Side.SELL
        gap = abs(fast - slow) / slow
        return self._intent(
            context,
            side,
            Decimal("0.50") + trend * Decimal("0.35") + gap * Decimal("10"),
            Decimal("0.02"),
            Decimal("0.04"),
            {"ema_fast": fast, "ema_slow": slow, "trend_strength": trend},
        )


class BollingerSqueezeBreakoutStrategy(DeterministicStrategy):
    strategy_id = "bollinger_squeeze_breakout"

    def __init__(
        self,
        *args: object,
        window: int = 20,
        deviations: Decimal = Decimal("2"),
        squeeze_quantile: Decimal = Decimal("0.30"),
        **kwargs: object,
    ) -> None:
        super().__init__(*args, **kwargs)
        if window < 2 or deviations <= ZERO or not ZERO <= squeeze_quantile <= ONE:
            raise ValueError("invalid Bollinger squeeze parameters")
        self.window = window
        self.deviations = deviations
        self.squeeze_quantile = squeeze_quantile

    def parameter_items(self) -> tuple[tuple[str, str], ...]:
        return super().parameter_items() + (
            ("deviations", str(self.deviations)),
            ("squeeze_quantile", str(self.squeeze_quantile)),
            ("window", str(self.window)),
        )

    def decide_context(self, context: StrategyContext) -> TradeIntent | None:
        closes = context.closes
        if len(closes) < self.window * 2 + 1:
            return None
        baseline = closes[-self.window - 1 : -1]
        average, deviation = _mean(baseline), _stddev(baseline)
        if average <= ZERO:
            return None
        bandwidths: list[Decimal] = []
        history = closes[-self.window * 2 - 1 : -1]
        for end in range(self.window, len(history) + 1):
            window = history[end - self.window : end]
            window_mean = _mean(window)
            if window_mean > ZERO:
                bandwidths.append(self.deviations * Decimal("2") * _stddev(window) / window_mean)
        if not bandwidths:
            return None
        ordered = sorted(bandwidths)
        quantile_index = min(
            len(ordered) - 1, int(Decimal(len(ordered) - 1) * self.squeeze_quantile)
        )
        prior_bandwidth = self.deviations * Decimal("2") * deviation / average
        if prior_bandwidth > ordered[quantile_index]:
            return None
        upper, lower, price = (
            average + self.deviations * deviation,
            average - self.deviations * deviation,
            closes[-1],
        )
        if price > upper:
            side, distance = Side.BUY, price / upper - ONE
        elif price < lower:
            side, distance = Side.SELL, ONE - price / lower
        else:
            return None
        return self._intent(
            context,
            side,
            Decimal("0.58") + distance * Decimal("20"),
            Decimal("0.015"),
            Decimal("0.035"),
            {"bandwidth": prior_bandwidth, "squeeze_threshold": ordered[quantile_index]},
        )


class BollingerRsiMeanReversionStrategy(DeterministicStrategy):
    strategy_id = "bollinger_rsi_mean_reversion"

    def __init__(
        self,
        *args: object,
        window: int = 20,
        rsi_period: int = 14,
        z_threshold: Decimal = Decimal("1.8"),
        maximum_trend_strength: Decimal = Decimal("0.35"),
        **kwargs: object,
    ) -> None:
        super().__init__(*args, **kwargs)
        if (
            window < 2
            or rsi_period < 2
            or z_threshold <= ZERO
            or not ZERO <= maximum_trend_strength <= ONE
        ):
            raise ValueError("invalid Bollinger/RSI parameters")
        self.window = window
        self.rsi_period = rsi_period
        self.z_threshold = z_threshold
        self.maximum_trend_strength = maximum_trend_strength

    def parameter_items(self) -> tuple[tuple[str, str], ...]:
        return super().parameter_items() + (
            ("maximum_trend_strength", str(self.maximum_trend_strength)),
            ("rsi_period", str(self.rsi_period)),
            ("window", str(self.window)),
            ("z_threshold", str(self.z_threshold)),
        )

    def decide_context(self, context: StrategyContext) -> TradeIntent | None:
        closes = context.closes
        required = max(self.window, self.rsi_period + 1)
        if len(closes) < required:
            return None
        window = closes[-self.window :]
        average, deviation = _mean(window), _stddev(window)
        if deviation == ZERO:
            return None
        zscore = (closes[-1] - average) / deviation
        rsi = _rsi(closes, self.rsi_period)
        trend = _trend_strength(closes, self.rsi_period)
        if trend > self.maximum_trend_strength:
            return None
        if zscore <= -self.z_threshold and rsi <= Decimal("35"):
            side = Side.BUY
        elif zscore >= self.z_threshold and rsi >= Decimal("65"):
            side = Side.SELL
        else:
            return None
        return self._intent(
            context,
            side,
            Decimal("0.55") + min(abs(zscore), Decimal("4")) * Decimal("0.08"),
            Decimal("0.015"),
            Decimal("0.025"),
            {"rsi": rsi, "trend_strength": trend, "zscore": zscore},
        )


class AtrShockFadeStrategy(DeterministicStrategy):
    strategy_id = "atr_shock_fade"

    def __init__(
        self,
        *args: object,
        lookback: int = 20,
        shock_multiple: Decimal = Decimal("2.5"),
        **kwargs: object,
    ) -> None:
        super().__init__(*args, **kwargs)
        if lookback < 2 or shock_multiple <= ONE:
            raise ValueError("invalid ATR shock parameters")
        self.lookback = lookback
        self.shock_multiple = shock_multiple

    def parameter_items(self) -> tuple[tuple[str, str], ...]:
        return super().parameter_items() + (
            ("lookback", str(self.lookback)),
            ("shock_multiple", str(self.shock_multiple)),
        )

    def decide_context(self, context: StrategyContext) -> TradeIntent | None:
        closes = context.closes
        if len(closes) < self.lookback + 2 or closes[-2] <= ZERO:
            return None
        baseline = _mean_absolute_return(closes[:-1], self.lookback)
        shock = closes[-1] / closes[-2] - ONE
        if baseline == ZERO or abs(shock) < baseline * self.shock_multiple:
            return None
        side = Side.SELL if shock > ZERO else Side.BUY
        severity = abs(shock) / baseline
        return self._intent(
            context,
            side,
            Decimal("0.55") + min(severity, Decimal("6")) * Decimal("0.05"),
            Decimal("0.02"),
            Decimal("0.025"),
            {"baseline_atr": baseline, "shock": shock, "shock_multiple": severity},
        )


class LondonBreakoutStrategy(DeterministicStrategy):
    strategy_id = "london_breakout_eurusd"

    def __init__(
        self,
        *args: object,
        bars_per_day: int = 288,
        range_end_bar: int = 84,
        trade_end_bar: int = 120,
        buffer: Decimal = Decimal("0.0004"),
        **kwargs: object,
    ) -> None:
        super().__init__(*args, **kwargs)
        if (
            bars_per_day <= 0
            or not 0 < range_end_bar < trade_end_bar <= bars_per_day
            or buffer < ZERO
        ):
            raise ValueError("invalid London breakout parameters")
        self.bars_per_day = bars_per_day
        self.range_end_bar = range_end_bar
        self.trade_end_bar = trade_end_bar
        self.buffer = buffer

    def parameter_items(self) -> tuple[tuple[str, str], ...]:
        return super().parameter_items() + (
            ("bars_per_day", str(self.bars_per_day)),
            ("buffer", str(self.buffer)),
            ("range_end_bar", str(self.range_end_bar)),
            ("trade_end_bar", str(self.trade_end_bar)),
        )

    def decide_context(self, context: StrategyContext) -> TradeIntent | None:
        if context.symbol != "EURUSD":
            return None
        timed = _clock_session(context, "Europe/London", time(0, 0), time(10, 0))
        if timed:
            minute = _minutes_since_midnight(timed[-1][0])
            range_values = tuple(
                value for stamp, value in timed if _minutes_since_midnight(stamp) < 7 * 60
            )
            if not 7 * 60 <= minute < 10 * 60 or not range_values:
                return None
            price = timed[-1][1]
        else:
            position = _bar_of_day(context, self.bars_per_day)
            session = _session_values(context, self.bars_per_day)
            if (
                not self.range_end_bar <= position < self.trade_end_bar
                or len(session) <= self.range_end_bar
            ):
                return None
            range_values, price = session[: self.range_end_bar], session[-1]
        high, low = max(range_values), min(range_values)
        if price > high * (ONE + self.buffer):
            side, distance = Side.BUY, price / high - ONE
        elif price < low * (ONE - self.buffer):
            side, distance = Side.SELL, ONE - price / low
        else:
            return None
        return self._intent(
            context,
            side,
            Decimal("0.58") + distance * Decimal("25"),
            Decimal("0.01"),
            Decimal("0.02"),
            {"asian_high": high, "asian_low": low, "breakout": distance},
        )


class NyLondonOverlapMomentumStrategy(DeterministicStrategy):
    strategy_id = "ny_london_overlap_momentum_eurusd"

    def __init__(
        self,
        *args: object,
        bars_per_day: int = 288,
        overlap_start_bar: int = 156,
        overlap_end_bar: int = 192,
        minimum_move: Decimal = Decimal("0.001"),
        **kwargs: object,
    ) -> None:
        super().__init__(*args, **kwargs)
        if (
            bars_per_day <= 0
            or not 0 <= overlap_start_bar < overlap_end_bar <= bars_per_day
            or minimum_move < ZERO
        ):
            raise ValueError("invalid overlap momentum parameters")
        self.bars_per_day = bars_per_day
        self.overlap_start_bar = overlap_start_bar
        self.overlap_end_bar = overlap_end_bar
        self.minimum_move = minimum_move

    def parameter_items(self) -> tuple[tuple[str, str], ...]:
        return super().parameter_items() + (
            ("bars_per_day", str(self.bars_per_day)),
            ("minimum_move", str(self.minimum_move)),
            ("overlap_end_bar", str(self.overlap_end_bar)),
            ("overlap_start_bar", str(self.overlap_start_bar)),
        )

    def decide_context(self, context: StrategyContext) -> TradeIntent | None:
        if context.symbol != "EURUSD":
            return None
        timed = _clock_session(context, "UTC", time(13, 0), time(16, 0))
        if timed:
            minute = _minutes_since_midnight(timed[-1][0])
            if not 13 * 60 < minute < 16 * 60 or len(timed) < 2:
                return None
            move = timed[-1][1] / timed[0][1] - ONE
            position = minute
        else:
            position = _bar_of_day(context, self.bars_per_day)
            session = _session_values(context, self.bars_per_day)
            if (
                not self.overlap_start_bar < position < self.overlap_end_bar
                or len(session) <= self.overlap_start_bar
            ):
                return None
            move = session[-1] / session[self.overlap_start_bar] - ONE
        if abs(move) < self.minimum_move:
            return None
        side = Side.BUY if move > ZERO else Side.SELL
        return self._intent(
            context,
            side,
            Decimal("0.55") + abs(move) * Decimal("20"),
            Decimal("0.012"),
            Decimal("0.025"),
            {"overlap_move": move, "session_position": position},
        )


class SpxNasdaqPairsMeanReversionStrategy(DeterministicStrategy):
    strategy_id = "spx_nasdaq_pairs_mean_reversion"
    max_holding_seconds = 259_200

    def __init__(
        self,
        *args: object,
        return_window: int = 6,
        z_window: int = 30,
        z_threshold: Decimal = Decimal("1.75"),
        hedge_ratio: Decimal = ONE,
        **kwargs: object,
    ) -> None:
        super().__init__(*args, **kwargs)
        if return_window <= 0 or z_window < 3 or z_threshold <= ZERO or hedge_ratio <= ZERO:
            raise ValueError("invalid pairs parameters")
        self.return_window = return_window
        self.z_window = z_window
        self.z_threshold = z_threshold
        self.hedge_ratio = hedge_ratio

    def parameter_items(self) -> tuple[tuple[str, str], ...]:
        return super().parameter_items() + (
            ("hedge_ratio", str(self.hedge_ratio)),
            ("return_window", str(self.return_window)),
            ("z_threshold", str(self.z_threshold)),
            ("z_window", str(self.z_window)),
        )

    def decide_context(self, context: StrategyContext) -> TradeIntent | None:
        references = {key.upper(): tuple(value) for key, value in context.related_closes.items()}
        reference_symbol = (
            "NSDQ100"
            if context.symbol == "SPX500"
            else "SPX500"
            if context.symbol == "NSDQ100"
            else ""
        )
        reference = references.get(reference_symbol)
        if reference is None:
            return None
        size = min(len(context.closes), len(reference))
        required = self.return_window + self.z_window
        if size < required:
            return None
        target = context.closes[-size:]
        reference = reference[-size:]
        spreads: list[Decimal] = []
        for index in range(size - self.z_window, size):
            target_base, reference_base = (
                target[index - self.return_window],
                reference[index - self.return_window],
            )
            if target_base <= ZERO or reference_base <= ZERO:
                return None
            target_return = target[index] / target_base - ONE
            reference_return = reference[index] / reference_base - ONE
            spreads.append(target_return - self.hedge_ratio * reference_return)
        history = tuple(spreads)
        deviation = _stddev(history)
        if deviation == ZERO:
            return None
        zscore = (history[-1] - _mean(history)) / deviation
        if abs(zscore) < self.z_threshold:
            return None
        side = Side.SELL if zscore > ZERO else Side.BUY
        return self._intent(
            context,
            side,
            Decimal("0.55") + min(abs(zscore), Decimal("4")) * Decimal("0.08"),
            Decimal("0.015"),
            Decimal("0.025"),
            {"reference": reference_symbol, "spread_zscore": zscore},
        )


class EurUsdFourHourTimeSeriesMomentumStrategy(DeterministicStrategy):
    strategy_id = "eurusd_4h_time_series_momentum"
    max_holding_seconds = 259_200

    def __init__(
        self,
        *args: object,
        bars_per_four_hours: int = 48,
        lookback_periods: int = 6,
        minimum_move: Decimal = Decimal("0.002"),
        **kwargs: object,
    ) -> None:
        super().__init__(*args, **kwargs)
        if bars_per_four_hours <= 0 or lookback_periods <= 0 or minimum_move < ZERO:
            raise ValueError("invalid time-series momentum parameters")
        self.bars_per_four_hours = bars_per_four_hours
        self.lookback_periods = lookback_periods
        self.minimum_move = minimum_move

    def parameter_items(self) -> tuple[tuple[str, str], ...]:
        return super().parameter_items() + (
            ("bars_per_four_hours", str(self.bars_per_four_hours)),
            ("lookback_periods", str(self.lookback_periods)),
            ("minimum_move", str(self.minimum_move)),
        )

    def decide_context(self, context: StrategyContext) -> TradeIntent | None:
        if context.symbol != "EURUSD":
            return None
        horizon = (
            max(
                1,
                (4 * 60 * 60 * self.lookback_periods + context.bar_interval_seconds - 1)
                // context.bar_interval_seconds,
            )
            if context.bar_interval_seconds
            else self.bars_per_four_hours * self.lookback_periods
        )
        if len(context.closes) <= horizon or context.closes[-horizon - 1] <= ZERO:
            return None
        move = context.closes[-1] / context.closes[-horizon - 1] - ONE
        if abs(move) < self.minimum_move:
            return None
        volatility = _mean_absolute_return(context.closes, min(horizon, len(context.closes) - 1))
        side = Side.BUY if move > ZERO else Side.SELL
        normalized = abs(move) / max(volatility, Decimal("0.000001"))
        return self._intent(
            context,
            side,
            Decimal("0.52") + min(normalized, Decimal("8")) * Decimal("0.05"),
            Decimal("0.02"),
            Decimal("0.04"),
            {"horizon_move": move, "mean_absolute_return": volatility},
        )


class CommodityHypothesisStrategy(DeterministicStrategy):
    """One commodity hypothesis under one immutable risk profile.

    Profiles are separate ledgers and strategy identities. They may vary signal
    selectivity, notional and exits, but never the global deterministic risk
    policy or the fixed DEMO execution route.
    """

    parameter_version = "3.0.0"

    def __init__(
        self,
        *,
        strategy_id: str,
        symbol: str,
        hypothesis: str,
        risk_profile: CommodityRiskProfile,
    ) -> None:
        super().__init__(
            order_amount_usd=risk_profile.order_amount_usd,
            minimum_confidence=risk_profile.minimum_confidence,
        )
        if symbol not in {"OIL", "NATGAS"}:
            raise ValueError("commodity strategy symbol is unsupported")
        if hypothesis not in {
            "adaptive_range",
            "donchian_breakout",
            "ema_trend",
            "shock_fade",
            "positive_spike_fade",
            "squeeze_breakout",
        }:
            raise ValueError("commodity hypothesis is unsupported")
        self.strategy_id = strategy_id
        self.symbol = symbol
        self.hypothesis = hypothesis
        self.risk_profile = risk_profile
        self.max_holding_seconds = risk_profile.max_holding_seconds

    def parameter_items(self) -> tuple[tuple[str, str], ...]:
        stop, take = self._exit_fractions()
        return super().parameter_items() + (
            ("hypothesis", self.hypothesis),
            ("max_holding_seconds", str(self.max_holding_seconds)),
            ("risk_profile", self.risk_profile.profile_id),
            ("stop_loss_fraction", str(stop)),
            ("symbol", self.symbol),
            ("take_profit_fraction", str(take)),
            ("threshold_multiplier", str(self.risk_profile.threshold_multiplier)),
        )

    def _exit_fractions(self) -> tuple[Decimal, Decimal]:
        if self.symbol == "OIL":
            return (
                self.risk_profile.oil_stop_fraction,
                self.risk_profile.oil_take_fraction,
            )
        return (
            self.risk_profile.gas_stop_fraction,
            self.risk_profile.gas_take_fraction,
        )

    def _emit(
        self,
        context: StrategyContext,
        side: Side,
        raw_confidence: Decimal,
        metrics: Mapping[str, Decimal | int | str],
    ) -> TradeIntent:
        stop, take = self._exit_fractions()
        return self._intent(context, side, raw_confidence, stop, take, metrics)

    def _adaptive_range(self, context: StrategyContext) -> TradeIntent | None:
        profile_quantiles = {
            "prudent": (Decimal("0.10"), Decimal("0.90")),
            "balanced": (Decimal("0.20"), Decimal("0.80")),
            "aggressive": (Decimal("0.30"), Decimal("0.70")),
        }
        if len(context.closes) < 193:
            return None
        history = context.closes[-193:-1]
        lower_q, upper_q = profile_quantiles[self.risk_profile.profile_id]
        lower, upper, price = (
            _quantile(history, lower_q),
            _quantile(history, upper_q),
            context.closes[-1],
        )
        scale = max(_mean_absolute_return(context.closes, 96), Decimal("0.000001"))
        if price < lower and context.closes[-1] >= context.closes[-2]:
            side, distance = Side.BUY, (lower - price) / max(price, Decimal("0.000001"))
        elif price > upper and context.closes[-1] <= context.closes[-2]:
            side, distance = Side.SELL, (price - upper) / max(price, Decimal("0.000001"))
        else:
            return None
        normalized = distance / scale
        return self._emit(
            context,
            side,
            Decimal("0.54") + min(normalized, Decimal("5")) * Decimal("0.08"),
            {"distance": distance, "lower_quantile": lower, "upper_quantile": upper},
        )

    def _donchian_breakout(self, context: StrategyContext) -> TradeIntent | None:
        lookbacks = {"prudent": 96, "balanced": 72, "aggressive": 48}
        lookback = lookbacks[self.risk_profile.profile_id]
        if len(context.closes) <= lookback:
            return None
        history, price = context.closes[-lookback - 1 : -1], context.closes[-1]
        high, low = max(history), min(history)
        volatility = _mean_absolute_return(context.closes, lookback)
        buffer = volatility * self.risk_profile.threshold_multiplier
        if price > high * (ONE + buffer):
            side, distance = Side.BUY, price / high - ONE
        elif price < low * (ONE - buffer):
            side, distance = Side.SELL, ONE - price / low
        else:
            return None
        normalized = distance / max(volatility, Decimal("0.000001"))
        return self._emit(
            context,
            side,
            Decimal("0.54") + min(normalized, Decimal("5")) * Decimal("0.08"),
            {"breakout": distance, "channel_high": high, "channel_low": low, "lookback": lookback},
        )

    def _ema_trend(self, context: StrategyContext) -> TradeIntent | None:
        periods = {
            "prudent": (24, 96),
            "balanced": (16, 64),
            "aggressive": (8, 32),
        }
        fast_period, slow_period = periods[self.risk_profile.profile_id]
        if len(context.closes) < slow_period:
            return None
        fast = _ema(context.closes, fast_period)
        slow = _ema(context.closes, slow_period)
        if slow <= ZERO:
            return None
        spread = abs(fast / slow - ONE)
        volatility = _mean_absolute_return(context.closes, slow_period)
        required = volatility * Decimal("1.5") * self.risk_profile.threshold_multiplier
        if spread <= required:
            return None
        side = Side.BUY if fast > slow else Side.SELL
        normalized = spread / max(volatility, Decimal("0.000001"))
        return self._emit(
            context,
            side,
            Decimal("0.53") + min(normalized, Decimal("5")) * Decimal("0.08"),
            {"ema_fast": fast, "ema_slow": slow, "spread": spread},
        )

    def _shock_fade(self, context: StrategyContext) -> TradeIntent | None:
        if len(context.closes) < 98 or context.closes[-2] <= ZERO:
            return None
        latest_return = context.closes[-1] / context.closes[-2] - ONE
        baseline = _mean_absolute_return(context.closes[:-1], 96)
        shock_multipliers = {
            "prudent": Decimal("5"),
            "balanced": Decimal("4"),
            "aggressive": Decimal("3"),
        }
        required = baseline * shock_multipliers[self.risk_profile.profile_id]
        if baseline <= ZERO or abs(latest_return) < required:
            return None
        if self.hypothesis == "positive_spike_fade" and latest_return <= ZERO:
            return None
        side = Side.SELL if latest_return > ZERO else Side.BUY
        normalized = abs(latest_return) / baseline
        return self._emit(
            context,
            side,
            Decimal("0.54") + min(normalized, Decimal("7")) * Decimal("0.06"),
            {
                "baseline_abs_return": baseline,
                "shock_multiple": normalized,
                "shock_return": latest_return,
            },
        )

    def _squeeze_breakout(self, context: StrategyContext) -> TradeIntent | None:
        if len(context.closes) < 98 or context.closes[-2] <= ZERO:
            return None
        long_vol = _mean_absolute_return(context.closes[:-1], 96)
        short_vol = _mean_absolute_return(context.closes[:-1], 16)
        latest_return = context.closes[-1] / context.closes[-2] - ONE
        squeeze_limits = {
            "prudent": Decimal("0.45"),
            "balanced": Decimal("0.60"),
            "aggressive": Decimal("0.75"),
        }
        breakout_multipliers = {
            "prudent": Decimal("3"),
            "balanced": Decimal("2.4"),
            "aggressive": Decimal("1.8"),
        }
        if (
            long_vol <= ZERO
            or short_vol > long_vol * squeeze_limits[self.risk_profile.profile_id]
            or abs(latest_return) < long_vol * breakout_multipliers[self.risk_profile.profile_id]
        ):
            return None
        side = Side.BUY if latest_return > ZERO else Side.SELL
        normalized = abs(latest_return) / long_vol
        return self._emit(
            context,
            side,
            Decimal("0.54") + min(normalized, Decimal("6")) * Decimal("0.07"),
            {
                "breakout_return": latest_return,
                "long_volatility": long_vol,
                "short_volatility": short_vol,
            },
        )

    def decide_context(self, context: StrategyContext) -> TradeIntent | None:
        if context.symbol != self.symbol:
            return None
        if self.hypothesis == "adaptive_range":
            return self._adaptive_range(context)
        if self.hypothesis == "donchian_breakout":
            return self._donchian_breakout(context)
        if self.hypothesis == "ema_trend":
            return self._ema_trend(context)
        if self.hypothesis in {"shock_fade", "positive_spike_fade"}:
            return self._shock_fade(context)
        return self._squeeze_breakout(context)


def build_strategy_suite(config: StrategyConfig) -> tuple[DeterministicStrategy, ...]:
    """Return the versioned core plus commodity risk-profile experiments."""

    common = {
        "order_amount_usd": config.order_amount_usd,
        "minimum_confidence": config.minimum_confidence,
    }
    core = (
        OpeningRangeBreakoutStrategy(**common),
        OpeningRangeRetestStrategy(**common),
        FirstLastHalfHourMomentumStrategy(**common),
        DonchianAtrBreakoutStrategy(**common),
        EmaAdxStrategy(**common),
        BollingerSqueezeBreakoutStrategy(**common),
        BollingerRsiMeanReversionStrategy(**common),
        AtrShockFadeStrategy(**common),
        LondonBreakoutStrategy(**common),
        NyLondonOverlapMomentumStrategy(**common),
        SpxNasdaqPairsMeanReversionStrategy(**common),
        EurUsdFourHourTimeSeriesMomentumStrategy(**common),
    )
    profiles = {profile.profile_id: profile for profile in COMMODITY_RISK_PROFILES}
    commodity = tuple(
        CommodityHypothesisStrategy(
            strategy_id=f"{hypothesis['base_id']}__{profile.profile_id}",
            symbol=hypothesis["symbol"],
            hypothesis=hypothesis["hypothesis"],
            risk_profile=profiles[profile.profile_id],
        )
        for hypothesis in COMMODITY_HYPOTHESES
        for profile in COMMODITY_RISK_PROFILES
    )
    return core + commodity
