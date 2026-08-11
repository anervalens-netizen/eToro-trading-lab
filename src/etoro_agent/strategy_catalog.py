from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class CommodityRiskProfile:
    profile_id: str
    display_name: str
    order_amount_usd: Decimal
    minimum_confidence: Decimal
    threshold_multiplier: Decimal
    oil_stop_fraction: Decimal
    oil_take_fraction: Decimal
    gas_stop_fraction: Decimal
    gas_take_fraction: Decimal
    max_holding_seconds: int


COMMODITY_RISK_PROFILES: tuple[CommodityRiskProfile, ...] = (
    CommodityRiskProfile(
        "prudent",
        "Prudent",
        Decimal("50"),
        Decimal("0.68"),
        Decimal("1.30"),
        Decimal("0.020"),
        Decimal("0.040"),
        Decimal("0.035"),
        Decimal("0.070"),
        8 * 60 * 60,
    ),
    CommodityRiskProfile(
        "balanced",
        "Balanced",
        Decimal("100"),
        Decimal("0.60"),
        Decimal("1.00"),
        Decimal("0.030"),
        Decimal("0.060"),
        Decimal("0.055"),
        Decimal("0.100"),
        16 * 60 * 60,
    ),
    CommodityRiskProfile(
        "aggressive",
        "Aggressive",
        Decimal("150"),
        Decimal("0.55"),
        Decimal("0.75"),
        Decimal("0.050"),
        Decimal("0.090"),
        Decimal("0.080"),
        Decimal("0.140"),
        24 * 60 * 60,
    ),
)


CORE_STRATEGY_DEFINITIONS: tuple[dict[str, str], ...] = (
    {
        "id": "orb_15m_immediate",
        "name": "ORB 15m Immediate",
        "family": "breakout",
        "symbol": "SPX500",
        "risk_profile": "standard",
    },
    {
        "id": "orb_15m_retest",
        "name": "ORB 15m Retest",
        "family": "breakout",
        "symbol": "NSDQ100",
        "risk_profile": "standard",
    },
    {
        "id": "first_30m_last_30m_momentum",
        "name": "Session Momentum",
        "family": "momentum",
        "symbol": "SPX500",
        "risk_profile": "standard",
    },
    {
        "id": "donchian_atr_breakout",
        "name": "Donchian + ATR",
        "family": "trend",
        "symbol": "BTC",
        "risk_profile": "standard",
    },
    {
        "id": "ema_9_21_adx",
        "name": "EMA 9/21 + ADX",
        "family": "trend",
        "symbol": "AAPL",
        "risk_profile": "standard",
    },
    {
        "id": "bollinger_squeeze_breakout",
        "name": "Bollinger Squeeze",
        "family": "breakout",
        "symbol": "ETH",
        "risk_profile": "standard",
    },
    {
        "id": "bollinger_rsi_mean_reversion",
        "name": "Bollinger RSI Reversion",
        "family": "mean reversion",
        "symbol": "AAPL",
        "risk_profile": "standard",
    },
    {
        "id": "atr_shock_fade",
        "name": "ATR Shock Fade",
        "family": "mean reversion",
        "symbol": "TSLA",
        "risk_profile": "standard",
    },
    {
        "id": "london_breakout_eurusd",
        "name": "EURUSD London Breakout",
        "family": "fx session",
        "symbol": "EURUSD",
        "risk_profile": "standard",
    },
    {
        "id": "ny_london_overlap_momentum_eurusd",
        "name": "EURUSD NY/London",
        "family": "fx session",
        "symbol": "EURUSD",
        "risk_profile": "standard",
    },
    {
        "id": "spx_nasdaq_pairs_mean_reversion",
        "name": "SPX-Nasdaq Pairs",
        "family": "relative value",
        "symbol": "SPX500",
        "risk_profile": "standard",
    },
    {
        "id": "eurusd_4h_time_series_momentum",
        "name": "EURUSD 4h Momentum",
        "family": "swing",
        "symbol": "EURUSD",
        "risk_profile": "standard",
    },
)


COMMODITY_HYPOTHESES: tuple[dict[str, str], ...] = (
    {
        "base_id": "oil_adaptive_range",
        "name": "OIL Adaptive Range",
        "family": "commodity mean reversion",
        "symbol": "OIL",
        "hypothesis": "adaptive_range",
    },
    {
        "base_id": "oil_donchian_breakout",
        "name": "OIL Donchian Breakout",
        "family": "commodity breakout",
        "symbol": "OIL",
        "hypothesis": "donchian_breakout",
    },
    {
        "base_id": "oil_ema_trend",
        "name": "OIL EMA Trend",
        "family": "commodity trend",
        "symbol": "OIL",
        "hypothesis": "ema_trend",
    },
    {
        "base_id": "oil_shock_fade",
        "name": "OIL Shock Fade",
        "family": "commodity shock",
        "symbol": "OIL",
        "hypothesis": "shock_fade",
    },
    {
        "base_id": "oil_squeeze_breakout",
        "name": "OIL Volatility Squeeze",
        "family": "commodity volatility",
        "symbol": "OIL",
        "hypothesis": "squeeze_breakout",
    },
    {
        "base_id": "natgas_adaptive_range",
        "name": "NATGAS Adaptive Range",
        "family": "commodity mean reversion",
        "symbol": "NATGAS",
        "hypothesis": "adaptive_range",
    },
    {
        "base_id": "natgas_donchian_breakout",
        "name": "NATGAS Donchian Breakout",
        "family": "commodity breakout",
        "symbol": "NATGAS",
        "hypothesis": "donchian_breakout",
    },
    {
        "base_id": "natgas_ema_trend",
        "name": "NATGAS EMA Trend",
        "family": "commodity trend",
        "symbol": "NATGAS",
        "hypothesis": "ema_trend",
    },
    {
        "base_id": "natgas_positive_spike_fade",
        "name": "NATGAS Positive Spike Fade",
        "family": "commodity shock",
        "symbol": "NATGAS",
        "hypothesis": "positive_spike_fade",
    },
    {
        "base_id": "natgas_squeeze_breakout",
        "name": "NATGAS Volatility Squeeze",
        "family": "commodity volatility",
        "symbol": "NATGAS",
        "hypothesis": "squeeze_breakout",
    },
)


COMMODITY_STRATEGY_DEFINITIONS: tuple[dict[str, str], ...] = tuple(
    {
        "id": f"{hypothesis['base_id']}__{profile.profile_id}",
        "name": f"{hypothesis['name']} - {profile.display_name}",
        "family": hypothesis["family"],
        "symbol": hypothesis["symbol"],
        "hypothesis": hypothesis["hypothesis"],
        "risk_profile": profile.profile_id,
    }
    for hypothesis in COMMODITY_HYPOTHESES
    for profile in COMMODITY_RISK_PROFILES
)

STRATEGY_DEFINITIONS: tuple[dict[str, str], ...] = (
    CORE_STRATEGY_DEFINITIONS + COMMODITY_STRATEGY_DEFINITIONS
)
STRATEGY_PORTFOLIO_BY_ID = {
    item["id"]: f"strategy_{index:02d}" for index, item in enumerate(STRATEGY_DEFINITIONS, start=1)
}
STRATEGY_SYMBOLS: tuple[str, ...] = tuple(item["symbol"] for item in STRATEGY_DEFINITIONS)
SHADOW_PORTFOLIO_IDS: tuple[str, ...] = tuple(STRATEGY_PORTFOLIO_BY_ID.values())
STRATEGY_COUNT = len(STRATEGY_DEFINITIONS)
