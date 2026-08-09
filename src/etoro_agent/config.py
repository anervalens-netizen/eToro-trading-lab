from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path


@dataclass(frozen=True)
class StrategyConfig:
    fast_window: int
    slow_window: int
    minimum_confidence: Decimal
    order_amount_usd: Decimal


@dataclass(frozen=True)
class RiskLimits:
    allowed_symbols: frozenset[str]
    max_order_notional_usd: Decimal
    max_symbol_exposure_usd: Decimal
    max_gross_exposure_usd: Decimal
    max_trades_per_day: int
    max_daily_loss_usd: Decimal
    max_drawdown_fraction: Decimal
    max_leverage: int
    min_stop_loss_fraction: Decimal
    max_stop_loss_fraction: Decimal
    approval_ttl_seconds: int
    max_weekly_loss_usd: Decimal = Decimal("30")
    max_monthly_loss_usd: Decimal = Decimal("50")
    max_trade_risk_usd: Decimal = Decimal("5")
    max_open_positions: int = 1
    max_correlated_exposure_usd: Decimal = Decimal("750")
    min_trade_interval_seconds: int = 900
    max_quote_age_seconds: int = 30
    max_spread_fraction: Decimal = Decimal("0.02")


@dataclass(frozen=True)
class AppConfig:
    account_mode: str
    initial_cash_usd: Decimal
    symbols: dict[str, int]
    candle_interval: str
    candle_count: int
    strategy: StrategyConfig
    risk: RiskLimits
    etoro_demo_execution_enabled: bool
    shadow_portfolio_count: int = 12
    report_timezone: str = "Europe/Bucharest"
    ai_decision_enabled: bool = True
    ai_decision_ttl_seconds: int = 1800
    master_strategy_ids: tuple[str, ...] = (
        "bollinger_rsi_mean_reversion",
        "london_breakout_eurusd",
        "eurusd_4h_time_series_momentum",
    )


def _decimal(value: object) -> Decimal:
    return Decimal(str(value))


def load_config(path: str | Path) -> AppConfig:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    mode = raw["account_mode"]
    if mode not in {"paper", "demo"}:
        raise ValueError("account_mode must be paper or demo; real is forbidden")
    strategy = raw["strategy"]
    risk = raw["risk"]
    config = AppConfig(
        account_mode=mode,
        initial_cash_usd=_decimal(raw["initial_cash_usd"]),
        symbols={str(k).upper(): int(v) for k, v in raw["symbols"].items()},
        candle_interval=str(raw["candle_interval"]),
        candle_count=int(raw["candle_count"]),
        strategy=StrategyConfig(
            fast_window=int(strategy["fast_window"]),
            slow_window=int(strategy["slow_window"]),
            minimum_confidence=_decimal(strategy["minimum_confidence"]),
            order_amount_usd=_decimal(strategy["order_amount_usd"]),
        ),
        risk=RiskLimits(
            allowed_symbols=frozenset(str(v).upper() for v in risk["allowed_symbols"]),
            max_order_notional_usd=_decimal(risk["max_order_notional_usd"]),
            max_symbol_exposure_usd=_decimal(risk["max_symbol_exposure_usd"]),
            max_gross_exposure_usd=_decimal(risk["max_gross_exposure_usd"]),
            max_trades_per_day=int(risk["max_trades_per_day"]),
            max_daily_loss_usd=_decimal(risk["max_daily_loss_usd"]),
            max_drawdown_fraction=_decimal(risk["max_drawdown_fraction"]),
            max_leverage=int(risk["max_leverage"]),
            min_stop_loss_fraction=_decimal(risk["min_stop_loss_fraction"]),
            max_stop_loss_fraction=_decimal(risk["max_stop_loss_fraction"]),
            approval_ttl_seconds=int(risk["approval_ttl_seconds"]),
            max_weekly_loss_usd=_decimal(risk.get("max_weekly_loss_usd", 30)),
            max_monthly_loss_usd=_decimal(risk.get("max_monthly_loss_usd", 50)),
            max_trade_risk_usd=_decimal(risk.get("max_trade_risk_usd", 5)),
            max_open_positions=int(risk.get("max_open_positions", 1)),
            max_correlated_exposure_usd=_decimal(
                risk.get("max_correlated_exposure_usd", 750)
            ),
            min_trade_interval_seconds=int(risk.get("min_trade_interval_seconds", 900)),
            max_quote_age_seconds=int(risk.get("max_quote_age_seconds", 30)),
            max_spread_fraction=_decimal(risk.get("max_spread_fraction", "0.02")),
        ),
        etoro_demo_execution_enabled=bool(raw["etoro_demo_execution_enabled"]),
        shadow_portfolio_count=int(raw.get("shadow_portfolio_count", 12)),
        report_timezone=str(raw.get("report_timezone", "Europe/Bucharest")),
        ai_decision_enabled=bool(raw.get("ai_decision", {}).get("enabled", True)),
        ai_decision_ttl_seconds=int(
            raw.get("ai_decision", {}).get("ttl_seconds", 1800)
        ),
        master_strategy_ids=tuple(
            str(value)
            for value in raw.get("ai_decision", {}).get(
                "master_strategy_ids",
                (
                    "bollinger_rsi_mean_reversion",
                    "london_breakout_eurusd",
                    "eurusd_4h_time_series_momentum",
                ),
            )
        ),
    )
    if config.shadow_portfolio_count != 12:
        raise ValueError("shadow_portfolio_count must be exactly 12")
    if config.risk.allowed_symbols != frozenset(config.symbols):
        raise ValueError("allowed_symbols must exactly match configured symbols")
    if config.etoro_demo_execution_enabled and config.account_mode != "demo":
        raise ValueError("eToro DEMO execution can be enabled only in demo mode")
    if config.ai_decision_ttl_seconds < 300:
        raise ValueError("AI decision TTL must be at least five minutes")
    if not config.master_strategy_ids or len(set(config.master_strategy_ids)) != len(
        config.master_strategy_ids
    ):
        raise ValueError("master strategy identifiers must be non-empty and unique")
    if config.risk.max_monthly_loss_usd != Decimal("50"):
        raise ValueError("monthly loss limit is locked at USD 50")
    return config
