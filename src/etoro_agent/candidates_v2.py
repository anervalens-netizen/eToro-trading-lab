from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path

from .domain_v2 import ONE, ZERO, Side
from .strategy_v2 import FamilySignal, StrategyFamily

_ENGINE_SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

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


def _canonical_hash(value: object) -> str:
    body = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode()).hexdigest()


def _mean(values: Sequence[Decimal]) -> Decimal:
    return sum(values, ZERO) / Decimal(len(values))


def _std(values: Sequence[Decimal]) -> Decimal:
    average = _mean(values)
    return (sum(((value - average) ** 2 for value in values), ZERO) / Decimal(len(values))).sqrt()


@dataclass(frozen=True)
class CandidateEngineParametersV2:
    trend_lookback: int = 20
    trend_threshold: Decimal = Decimal("0.60")
    trend_stop_fraction: Decimal = Decimal("0.015")
    trend_take_fraction: Decimal = Decimal("0.03")
    trend_holding_seconds: int = 8 * 3600
    session_lookback: int = 4
    session_minimum_move: Decimal = Decimal("0.002")
    session_threshold: Decimal = Decimal("0.60")
    session_stop_fraction: Decimal = Decimal("0.012")
    session_take_fraction: Decimal = Decimal("0.025")
    session_holding_seconds: int = 4 * 3600
    mean_reversion_lookback: int = 20
    mean_reversion_zscore: Decimal = Decimal("1.8")
    mean_reversion_threshold: Decimal = Decimal("0.62")
    mean_reversion_stop_fraction: Decimal = Decimal("0.012")
    mean_reversion_take_fraction: Decimal = Decimal("0.022")
    mean_reversion_holding_seconds: int = 6 * 3600
    baseline_lookback: int = 32
    baseline_move_multiple: Decimal = Decimal("2")
    baseline_threshold: Decimal = Decimal("0.60")
    baseline_stop_fraction: Decimal = Decimal("0.02")
    baseline_take_fraction: Decimal = Decimal("0.04")
    baseline_holding_seconds: int = 48 * 3600
    cost_stress_multiple: Decimal = Decimal("1.5")


@dataclass(frozen=True)
class CandidateBatchV2:
    engine_version: str
    engine_hash: str
    parameters_hash: str
    feature_schema_hash: str
    cost_model_hash: str
    symbol: str
    input_snapshot_hash: str
    signals: tuple[FamilySignal, ...]

    @property
    def batch_hash(self) -> str:
        signals = [
            {
                "family": signal.family.value,
                "strategy_version": signal.strategy_version,
                "symbol": signal.symbol,
                "side": signal.side.value,
                "raw_confidence": str(signal.raw_confidence),
                "confidence_threshold": str(signal.confidence_threshold),
                "stop_fraction": str(signal.stop_fraction),
                "take_fraction": str(signal.take_fraction),
                "max_holding_seconds": signal.max_holding_seconds,
                "rationale": signal.rationale,
                "evidence_refs": list(signal.evidence_refs),
            }
            for signal in self.signals
        ]
        return _canonical_hash(
            {
                "engine_version": self.engine_version,
                "engine_hash": self.engine_hash,
                "parameters_hash": self.parameters_hash,
                "feature_schema_hash": self.feature_schema_hash,
                "cost_model_hash": self.cost_model_hash,
                "symbol": self.symbol,
                "input_snapshot_hash": self.input_snapshot_hash,
                "signals": signals,
            }
        )


class CandidateEngineV2:
    """The single deterministic candidate implementation for every runtime adapter."""

    version = "candidate-engine-v2.2"
    _feature_schema = {
        "schema": "closed-ohlc-series-v1",
        "fields": ("symbol", "closes", "highs", "lows"),
        "number": "finite-decimal",
        "ordering": "oldest-to-newest",
    }

    def __init__(
        self,
        parameters: CandidateEngineParametersV2 | None = None,
        *,
        round_trip_cost_bps: Mapping[str, Decimal] | None = None,
    ) -> None:
        self.parameters = parameters or CandidateEngineParametersV2()
        self._validate_parameters(self.parameters)
        costs = (
            PROVISIONAL_ROUND_TRIP_COST_BPS if round_trip_cost_bps is None else round_trip_cost_bps
        )
        self.round_trip_cost_bps = {
            str(symbol).upper(): Decimal(str(value)) for symbol, value in costs.items()
        }
        if not self.round_trip_cost_bps or any(
            not value.is_finite() or value < ZERO for value in self.round_trip_cost_bps.values()
        ):
            raise ValueError("candidate cost model is invalid")

    @staticmethod
    def _validate_parameters(parameters: CandidateEngineParametersV2) -> None:
        value = asdict(parameters)
        decimal_values = [item for item in value.values() if isinstance(item, Decimal)]
        integer_values = [item for item in value.values() if isinstance(item, int)]
        if any(not item.is_finite() or item <= ZERO for item in decimal_values):
            raise ValueError("candidate engine decimal parameter is invalid")
        if any(item < 1 for item in integer_values):
            raise ValueError("candidate engine integer parameter is invalid")
        for name in (
            "trend_threshold",
            "session_threshold",
            "mean_reversion_threshold",
            "baseline_threshold",
        ):
            if Decimal(str(value[name])) > ONE:
                raise ValueError("candidate engine threshold is invalid")

    @property
    def parameters_hash(self) -> str:
        return _canonical_hash(asdict(self.parameters))

    @property
    def feature_schema_hash(self) -> str:
        return _canonical_hash(self._feature_schema)

    @property
    def cost_model_hash(self) -> str:
        return _canonical_hash(
            {symbol: str(value) for symbol, value in sorted(self.round_trip_cost_bps.items())}
        )

    @property
    def engine_hash(self) -> str:
        return _canonical_hash(
            {
                "version": self.version,
                "source_sha256": _ENGINE_SOURCE_SHA256,
                "algorithms": (
                    "donchian20_distance",
                    "four_bar_momentum",
                    "zscore20_with_reversal",
                    "return32_over_mean_abs_return",
                ),
                "parameters_hash": self.parameters_hash,
                "feature_schema_hash": self.feature_schema_hash,
                "cost_model_hash": self.cost_model_hash,
            }
        )

    @staticmethod
    def _validate_series(
        closes: Sequence[Decimal], highs: Sequence[Decimal], lows: Sequence[Decimal]
    ) -> None:
        if not (len(closes) == len(highs) == len(lows)):
            raise ValueError("candidate OHLC series lengths do not match")
        if any(not value.is_finite() for series in (closes, highs, lows) for value in series):
            raise ValueError("candidate OHLC series contains non-finite values")
        if any(value <= ZERO for series in (closes, highs, lows) for value in series):
            raise ValueError("candidate OHLC series must be positive")
        if any(high < low for high, low in zip(highs, lows, strict=True)):
            raise ValueError("candidate OHLC high is below low")

    def trend_breakout(
        self,
        symbol: str,
        closes: Sequence[Decimal],
        highs: Sequence[Decimal],
        lows: Sequence[Decimal],
        *,
        threshold: Decimal | None = None,
    ) -> FamilySignal | None:
        p = self.parameters
        if len(closes) < p.trend_lookback + 2:
            return None
        prior_high = max(highs[-p.trend_lookback - 1 : -1])
        prior_low = min(lows[-p.trend_lookback - 1 : -1])
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
            self.version,
            symbol.upper(),
            side,
            raw,
            p.trend_threshold if threshold is None else threshold,
            p.trend_stop_fraction,
            p.trend_take_fraction,
            p.trend_holding_seconds,
            f"donchian20_distance={distance}",
        )

    def session_momentum(
        self,
        symbol: str,
        closes: Sequence[Decimal],
        *,
        threshold: Decimal | None = None,
    ) -> FamilySignal | None:
        p = self.parameters
        if len(closes) < p.session_lookback + 4 or closes[-p.session_lookback - 1] <= ZERO:
            return None
        move = closes[-1] / closes[-p.session_lookback - 1] - ONE
        if abs(move) < p.session_minimum_move:
            return None
        raw = min(Decimal("0.90"), Decimal("0.52") + abs(move) * Decimal("35"))
        return FamilySignal(
            StrategyFamily.SESSION_MOMENTUM,
            self.version,
            symbol.upper(),
            Side.BUY if move > ZERO else Side.SELL,
            raw,
            p.session_threshold if threshold is None else threshold,
            p.session_stop_fraction,
            p.session_take_fraction,
            p.session_holding_seconds,
            f"four_bar_move={move}",
        )

    def mean_reversion(
        self,
        symbol: str,
        closes: Sequence[Decimal],
        *,
        threshold: Decimal | None = None,
    ) -> FamilySignal | None:
        p = self.parameters
        if len(closes) < p.mean_reversion_lookback + 2:
            return None
        window = closes[-p.mean_reversion_lookback :]
        deviation = _std(window)
        if deviation == ZERO:
            return None
        z = (closes[-1] - _mean(window)) / deviation
        latest = closes[-1] / closes[-2] - ONE if closes[-2] > ZERO else ZERO
        if z <= -p.mean_reversion_zscore and latest >= ZERO:
            side = Side.BUY
        elif z >= p.mean_reversion_zscore and latest <= ZERO:
            side = Side.SELL
        else:
            return None
        raw = min(Decimal("0.92"), Decimal("0.52") + min(abs(z), Decimal("4")) * Decimal("0.09"))
        return FamilySignal(
            StrategyFamily.REGIME_MEAN_REVERSION,
            self.version,
            symbol.upper(),
            side,
            raw,
            p.mean_reversion_threshold if threshold is None else threshold,
            p.mean_reversion_stop_fraction,
            p.mean_reversion_take_fraction,
            p.mean_reversion_holding_seconds,
            f"zscore={z};latest_return={latest}",
        )

    def statistical_baseline(
        self,
        symbol: str,
        closes: Sequence[Decimal],
        *,
        threshold: Decimal | None = None,
    ) -> FamilySignal | None:
        p = self.parameters
        if len(closes) < p.baseline_lookback + 1 or closes[-p.baseline_lookback - 1] <= ZERO:
            return None
        move = closes[-1] / closes[-p.baseline_lookback - 1] - ONE
        mean_absolute_return = _mean(
            [
                abs(closes[index] / closes[index - 1] - ONE)
                for index in range(len(closes) - p.baseline_lookback, len(closes))
            ]
        )
        if (
            mean_absolute_return == ZERO
            or abs(move) < mean_absolute_return * p.baseline_move_multiple
        ):
            return None
        normalized = abs(move) / mean_absolute_return
        raw = min(
            Decimal("0.99"),
            Decimal("0.50") + min(normalized, Decimal("8")) * Decimal("0.05"),
        )
        return FamilySignal(
            StrategyFamily.STATISTICAL_BASELINE,
            self.version,
            symbol.upper(),
            Side.BUY if move > ZERO else Side.SELL,
            raw,
            p.baseline_threshold if threshold is None else threshold,
            p.baseline_stop_fraction,
            p.baseline_take_fraction,
            p.baseline_holding_seconds,
            (
                f"lookback_return={move};lookback_return_bps={move * Decimal('10000')};"
                f"mean_abs_bar_return={mean_absolute_return};"
                f"mean_abs_bar_return_bps={mean_absolute_return * Decimal('10000')}"
            ),
        )

    def payoff_proxy_bps(self, signal: FamilySignal) -> Decimal:
        p = signal.raw_confidence
        return (p * signal.take_fraction - (ONE - p) * signal.stop_fraction) * Decimal("10000")

    def viable_net_of_cost(self, signal: FamilySignal) -> bool:
        if not signal.actionable:
            return False
        cost = self.round_trip_cost_bps.get(signal.symbol.upper(), Decimal("100"))
        return self.payoff_proxy_bps(signal) > cost * self.parameters.cost_stress_multiple

    def evaluate(
        self,
        symbol: str,
        closes: Sequence[Decimal],
        highs: Sequence[Decimal],
        lows: Sequence[Decimal],
        *,
        input_snapshot_hash: str | None = None,
        require_tradable: bool = True,
    ) -> CandidateBatchV2:
        self._validate_series(closes, highs, lows)
        derived_input_hash = _canonical_hash(
            {
                "symbol": symbol.upper(),
                "closes": [str(value) for value in closes],
                "highs": [str(value) for value in highs],
                "lows": [str(value) for value in lows],
            }
        )
        if (
            input_snapshot_hash is not None
            and re.fullmatch(r"[0-9a-f]{64}", input_snapshot_hash) is None
        ):
            raise ValueError("candidate input snapshot hash must be SHA-256")
        signals = tuple(
            signal
            for signal in (
                self.trend_breakout(symbol, closes, highs, lows),
                self.session_momentum(symbol, closes),
                self.mean_reversion(symbol, closes),
                self.statistical_baseline(symbol, closes),
            )
            if signal is not None and (not require_tradable or self.viable_net_of_cost(signal))
        )
        return CandidateBatchV2(
            self.version,
            self.engine_hash,
            self.parameters_hash,
            self.feature_schema_hash,
            self.cost_model_hash,
            symbol.upper(),
            input_snapshot_hash or derived_input_hash,
            signals,
        )


_CANONICAL_ENGINE = CandidateEngineV2()


def canonical_candidate_engine() -> CandidateEngineV2:
    return _CANONICAL_ENGINE


def trend_breakout_signal(
    symbol: str, closes: Sequence[Decimal], highs: Sequence[Decimal], lows: Sequence[Decimal]
) -> FamilySignal | None:
    return _CANONICAL_ENGINE.trend_breakout(symbol, closes, highs, lows)


def session_momentum_signal(symbol: str, closes: Sequence[Decimal]) -> FamilySignal | None:
    return _CANONICAL_ENGINE.session_momentum(symbol, closes)


def mean_reversion_signal(symbol: str, closes: Sequence[Decimal]) -> FamilySignal | None:
    return _CANONICAL_ENGINE.mean_reversion(symbol, closes)


def statistical_baseline_signal(symbol: str, closes: Sequence[Decimal]) -> FamilySignal | None:
    return _CANONICAL_ENGINE.statistical_baseline(symbol, closes)


def payoff_proxy_bps(signal: FamilySignal) -> Decimal:
    """Non-probabilistic tradability proxy; raw_confidence is not a calibrated probability."""

    return _CANONICAL_ENGINE.payoff_proxy_bps(signal)


def viable_net_of_cost(signal: FamilySignal, *, stress_multiple: Decimal = Decimal("1.5")) -> bool:
    if stress_multiple == _CANONICAL_ENGINE.parameters.cost_stress_multiple:
        return _CANONICAL_ENGINE.viable_net_of_cost(signal)
    if not signal.actionable:
        return False
    cost = _CANONICAL_ENGINE.round_trip_cost_bps.get(signal.symbol.upper(), Decimal("100"))
    return _CANONICAL_ENGINE.payoff_proxy_bps(signal) > cost * stress_multiple


def generate_core_candidate_batch(
    symbol: str,
    closes: Sequence[Decimal],
    highs: Sequence[Decimal],
    lows: Sequence[Decimal],
    *,
    input_snapshot_hash: str | None = None,
) -> CandidateBatchV2:
    return _CANONICAL_ENGINE.evaluate(
        symbol,
        closes,
        highs,
        lows,
        input_snapshot_hash=input_snapshot_hash,
    )


def generate_core_signals(
    symbol: str,
    closes: Sequence[Decimal],
    highs: Sequence[Decimal],
    lows: Sequence[Decimal],
) -> tuple[FamilySignal, ...]:
    """Compatibility API used by the coordinator; delegates to the canonical engine."""

    return generate_core_candidate_batch(symbol, closes, highs, lows).signals
