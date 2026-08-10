from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Mapping, Sequence

from .domain_v2 import IntentEnvelope, Side, ZERO, ONE, utc


class StrategyFamily(str, Enum):
    TREND_BREAKOUT = "trend_breakout_multi_horizon"
    SESSION_MOMENTUM = "session_momentum"
    REGIME_MEAN_REVERSION = "regime_mean_reversion"
    TRUE_RELATIVE_VALUE = "true_relative_value_spread"
    COMMODITY_EVENT_CARRY = "commodity_event_carry"
    STATISTICAL_BASELINE = "simple_statistical_baseline"
    SOL_DIRECT = "sol_direct_intent"


@dataclass(frozen=True)
class FamilySignal:
    family: StrategyFamily
    strategy_version: str
    symbol: str
    side: Side
    raw_confidence: Decimal
    confidence_threshold: Decimal
    stop_fraction: Decimal
    take_fraction: Decimal
    max_holding_seconds: int
    rationale: str
    evidence_refs: tuple[str, ...] = ()

    @property
    def actionable(self) -> bool:
        return self.raw_confidence >= self.confidence_threshold

    def to_intent(
        self,
        *,
        portfolio_id: str,
        lane_id: str,
        amount_usd: Decimal,
        created_at: datetime,
        reference_bid: Decimal,
        reference_ask: Decimal,
        snapshot_hash: str,
        max_price_drift_bps: Decimal = Decimal("25"),
        max_slippage_bps: Decimal = Decimal("15"),
    ) -> IntentEnvelope | None:
        if not self.actionable:
            return None
        created = utc(created_at)
        seed = (
            f"{portfolio_id}:{lane_id}:{self.family.value}:{self.strategy_version}:"
            f"{self.symbol}:{self.side.value}:{snapshot_hash}:{created.isoformat()}"
        )
        intent_id = f"intent-{hashlib.sha256(seed.encode()).hexdigest()[:24]}"
        return IntentEnvelope(
            intent_id=intent_id,
            portfolio_id=portfolio_id,
            lane_id=lane_id,
            strategy_id=self.family.value,
            strategy_version=self.strategy_version,
            symbol=self.symbol,
            side=self.side,
            amount_usd=amount_usd,
            raw_confidence=self.raw_confidence,
            confidence_threshold=self.confidence_threshold,
            stop_loss_fraction=self.stop_fraction,
            take_profit_fraction=self.take_fraction,
            max_holding_seconds=self.max_holding_seconds,
            created_at=created,
            valid_after=created,
            expires_at=created + timedelta(minutes=20),
            reference_bid=reference_bid,
            reference_ask=reference_ask,
            max_price_drift_bps=max_price_drift_bps,
            max_slippage_bps=max_slippage_bps,
            snapshot_hash=snapshot_hash,
            rationale=self.rationale,
            evidence_refs=self.evidence_refs,
            correlation_id=intent_id,
        )


@dataclass(frozen=True)
class SpreadSignal:
    strategy_version: str
    long_symbol: str
    short_symbol: str
    hedge_ratio: Decimal
    zscore: Decimal
    raw_confidence: Decimal
    confidence_threshold: Decimal
    max_unhedged_seconds: int = 5

    @property
    def actionable(self) -> bool:
        return self.raw_confidence >= self.confidence_threshold


def mean(values: Sequence[Decimal]) -> Decimal:
    return sum(values, ZERO) / Decimal(len(values))


def stddev(values: Sequence[Decimal]) -> Decimal:
    if len(values) < 2:
        return ZERO
    avg = mean(values)
    return (sum(((value - avg) ** 2 for value in values), ZERO) / Decimal(len(values))).sqrt()


def ema(values: Sequence[Decimal], period: int) -> Decimal:
    if period < 1 or len(values) < period:
        raise ValueError("insufficient values for EMA")
    alpha = Decimal("2") / Decimal(period + 1)
    result = mean(values[:period])
    for value in values[period:]:
        result = alpha * value + (ONE - alpha) * result
    return result


def rsi(values: Sequence[Decimal], period: int = 14) -> Decimal:
    if len(values) < period + 1:
        raise ValueError("insufficient values for RSI")
    changes = [values[i] - values[i - 1] for i in range(len(values) - period, len(values))]
    avg_gain = sum((max(change, ZERO) for change in changes), ZERO) / Decimal(period)
    avg_loss = sum((max(-change, ZERO) for change in changes), ZERO) / Decimal(period)
    if avg_loss == ZERO:
        return Decimal("100") if avg_gain > ZERO else Decimal("50")
    rs = avg_gain / avg_loss
    return Decimal("100") - Decimal("100") / (ONE + rs)


def wilder_adx(
    highs: Sequence[Decimal],
    lows: Sequence[Decimal],
    closes: Sequence[Decimal],
    period: int = 14,
) -> Decimal:
    """Wilder ADX with Wilder smoothing; returns the latest ADX."""
    if period < 2 or not (len(highs) == len(lows) == len(closes)):
        raise ValueError("ADX series are invalid")
    if len(closes) < period * 2 + 1:
        raise ValueError("ADX requires at least 2*period+1 bars")
    trs: list[Decimal] = []
    plus_dm: list[Decimal] = []
    minus_dm: list[Decimal] = []
    for i in range(1, len(closes)):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm.append(up if up > down and up > ZERO else ZERO)
        minus_dm.append(down if down > up and down > ZERO else ZERO)
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))

    atr = sum(trs[:period], ZERO)
    plus = sum(plus_dm[:period], ZERO)
    minus = sum(minus_dm[:period], ZERO)
    dxs: list[Decimal] = []
    for index in range(period - 1, len(trs)):
        if index >= period:
            atr = atr - atr / Decimal(period) + trs[index]
            plus = plus - plus / Decimal(period) + plus_dm[index]
            minus = minus - minus / Decimal(period) + minus_dm[index]
        if atr <= ZERO:
            dxs.append(ZERO)
            continue
        plus_di = Decimal("100") * plus / atr
        minus_di = Decimal("100") * minus / atr
        denom = plus_di + minus_di
        dxs.append(ZERO if denom == ZERO else Decimal("100") * abs(plus_di - minus_di) / denom)
    if len(dxs) < period:
        raise ValueError("ADX smoothing window unavailable")
    adx = mean(dxs[:period])
    for value in dxs[period:]:
        adx = ((Decimal(period - 1) * adx) + value) / Decimal(period)
    return adx


class StrategyFamilyEngine:
    version = "families-v2.0"

    def trend_breakout(
        self,
        symbol: str,
        highs: Sequence[Decimal],
        lows: Sequence[Decimal],
        closes: Sequence[Decimal],
        *,
        threshold: Decimal = Decimal("0.60"),
    ) -> FamilySignal | None:
        if len(closes) < 40:
            return None
        fast = ema(closes, 9)
        slow = ema(closes, 21)
        adx = wilder_adx(highs, lows, closes, 14)
        channel_high = max(highs[-21:-1])
        channel_low = min(lows[-21:-1])
        price = closes[-1]
        side: Side | None = None
        breakout = ZERO
        if price > channel_high and fast > slow:
            side = Side.BUY
            breakout = price / channel_high - ONE
        elif price < channel_low and fast < slow:
            side = Side.SELL
            breakout = ONE - price / channel_low
        if side is None:
            return None
        raw = min(Decimal("0.99"), Decimal("0.45") + adx / Decimal("200") + breakout * Decimal("20"))
        return FamilySignal(
            StrategyFamily.TREND_BREAKOUT,
            self.version,
            symbol.upper(),
            side,
            raw,
            threshold,
            Decimal("0.02"),
            Decimal("0.04"),
            24 * 3600,
            f"adx={adx};breakout={breakout};ema_fast={fast};ema_slow={slow}",
        )

    def mean_reversion(
        self,
        symbol: str,
        closes: Sequence[Decimal],
        *,
        threshold: Decimal = Decimal("0.62"),
    ) -> FamilySignal | None:
        if len(closes) < 30:
            return None
        window = closes[-20:]
        avg = mean(window)
        dev = stddev(window)
        if dev == ZERO:
            return None
        z = (closes[-1] - avg) / dev
        value_rsi = rsi(closes, 14)
        if z <= Decimal("-1.8") and value_rsi <= Decimal("35"):
            side = Side.BUY
        elif z >= Decimal("1.8") and value_rsi >= Decimal("65"):
            side = Side.SELL
        else:
            return None
        raw = min(Decimal("0.99"), Decimal("0.50") + min(abs(z), Decimal("4")) * Decimal("0.08"))
        return FamilySignal(
            StrategyFamily.REGIME_MEAN_REVERSION,
            self.version,
            symbol.upper(),
            side,
            raw,
            threshold,
            Decimal("0.015"),
            Decimal("0.025"),
            12 * 3600,
            f"z={z};rsi={value_rsi}",
        )

    def statistical_baseline(
        self,
        symbol: str,
        closes: Sequence[Decimal],
        *,
        threshold: Decimal = Decimal("0.60"),
    ) -> FamilySignal | None:
        if len(closes) < 33 or closes[-33] <= ZERO:
            return None
        move = closes[-1] / closes[-33] - ONE
        vol = mean([abs(closes[i] / closes[i - 1] - ONE) for i in range(len(closes) - 32, len(closes))])
        if vol == ZERO or abs(move) < vol * Decimal("2"):
            return None
        normalized = abs(move) / vol
        raw = min(Decimal("0.99"), Decimal("0.50") + min(normalized, Decimal("8")) * Decimal("0.05"))
        return FamilySignal(
            StrategyFamily.STATISTICAL_BASELINE,
            self.version,
            symbol.upper(),
            Side.BUY if move > ZERO else Side.SELL,
            raw,
            threshold,
            Decimal("0.02"),
            Decimal("0.04"),
            48 * 3600,
            f"move={move};mean_abs_return={vol}",
        )

    def relative_value(
        self,
        symbol_a: str,
        closes_a: Sequence[Decimal],
        symbol_b: str,
        closes_b: Sequence[Decimal],
        *,
        hedge_ratio: Decimal = ONE,
        threshold: Decimal = Decimal("0.65"),
    ) -> SpreadSignal | None:
        size = min(len(closes_a), len(closes_b))
        if size < 64:
            return None
        spreads = []
        for i in range(size - 32, size):
            if closes_a[i - 16] <= ZERO or closes_b[i - 16] <= ZERO:
                return None
            a_return = closes_a[i] / closes_a[i - 16] - ONE
            b_return = closes_b[i] / closes_b[i - 16] - ONE
            spreads.append(a_return - hedge_ratio * b_return)
        dev = stddev(spreads)
        if dev == ZERO:
            return None
        z = (spreads[-1] - mean(spreads)) / dev
        if abs(z) < Decimal("2"):
            return None
        # If A is rich, short A / long B; if cheap, long A / short B.
        raw = min(Decimal("0.99"), Decimal("0.52") + min(abs(z), Decimal("5")) * Decimal("0.07"))
        return SpreadSignal(
            self.version,
            long_symbol=(symbol_b if z > ZERO else symbol_a).upper(),
            short_symbol=(symbol_a if z > ZERO else symbol_b).upper(),
            hedge_ratio=hedge_ratio,
            zscore=z,
            raw_confidence=raw,
            confidence_threshold=threshold,
        )

    def commodity_event(
        self,
        symbol: str,
        *,
        surprise_zscore: Decimal,
        price_confirmation_return: Decimal,
        event_id: str,
        threshold: Decimal = Decimal("0.65"),
    ) -> FamilySignal | None:
        symbol = symbol.upper()
        if symbol not in {"OIL", "NATGAS"} or abs(surprise_zscore) < Decimal("1"):
            return None
        if surprise_zscore * price_confirmation_return <= ZERO:
            return None
        side = Side.BUY if surprise_zscore > ZERO else Side.SELL
        raw = min(Decimal("0.99"), Decimal("0.50") + min(abs(surprise_zscore), Decimal("4")) * Decimal("0.10"))
        return FamilySignal(
            StrategyFamily.COMMODITY_EVENT_CARRY,
            self.version,
            symbol,
            side,
            raw,
            threshold,
            Decimal("0.02"),
            Decimal("0.04"),
            8 * 3600,
            f"surprise_zscore={surprise_zscore};price_confirmation={price_confirmation_return}",
            evidence_refs=(event_id,),
        )
