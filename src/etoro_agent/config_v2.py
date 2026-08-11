from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from .compatibility_v2 import (
    BrokerInstrumentRules,
    CompatibilityResult,
    CompatibilityValidator,
    StrategyExecutionProfile,
)
from .risk_v2 import CapitalMandate


@dataclass(frozen=True)
class AppConfigV2:
    account_mode: str
    initial_cash_usd: Decimal
    symbols: dict[str, int]
    mandate: CapitalMandate
    broker_rules: dict[str, BrokerInstrumentRules]
    strategy_profiles: tuple[StrategyExecutionProfile, ...]
    live_demo_execution_enabled: bool
    websocket_enabled: bool
    data_catalog_path: str
    postgres_dsn_file: str | None

    def compatibility(self) -> tuple[CompatibilityResult, ...]:
        validator = CompatibilityValidator(
            max_order_usd=self.mandate.max_order_usd,
            max_trade_risk_usd=self.mandate.max_trade_risk_usd,
            max_gross_exposure_usd=self.mandate.max_gross_exposure_usd,
        )
        return validator.validate_all(self.strategy_profiles, self.broker_rules)


def _d(value: object) -> Decimal:
    return Decimal(str(value))


def load_config_v2(path: str | Path) -> AppConfigV2:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if raw.get("account_mode") != "demo":
        raise ValueError("v2 runtime supports DEMO only; REAL is a separate future release")
    symbols = {str(k).upper(): int(v) for k, v in raw["symbols"].items()}
    m = raw["capital_mandate"]
    mandate = CapitalMandate(
        allowed_symbols=frozenset(str(v).upper() for v in m["allowed_symbols"]),
        max_order_usd=_d(m["max_order_usd"]),
        max_trade_risk_usd=_d(m["max_trade_risk_usd"]),
        max_gross_exposure_usd=_d(m["max_gross_exposure_usd"]),
        max_correlated_exposure_usd=_d(m["max_correlated_exposure_usd"]),
        max_open_positions=int(m["max_open_positions"]),
        max_daily_loss_usd=_d(m["max_daily_loss_usd"]),
        max_weekly_loss_usd=_d(m["max_weekly_loss_usd"]),
        max_monthly_loss_usd=_d(m["max_monthly_loss_usd"]),
        reduce_only_drawdown_fraction=_d(m["reduce_only_drawdown_fraction"]),
        lock_drawdown_fraction=_d(m["lock_drawdown_fraction"]),
        max_quote_age_seconds=int(m["max_quote_age_seconds"]),
        max_spread_bps=_d(m["max_spread_bps"]),
        max_mid_drift_bps=_d(m["max_mid_drift_bps"]),
        min_trade_interval_seconds=int(m.get("min_trade_interval_seconds", 0)),
        max_leverage=int(m.get("max_leverage", 1)),
    )
    if mandate.allowed_symbols != frozenset(symbols):
        raise ValueError("capital mandate allowed_symbols must exactly match symbol catalog")
    rules: dict[str, BrokerInstrumentRules] = {}
    for symbol, value in raw.get("broker_rules", {}).items():
        rules[symbol.upper()] = BrokerInstrumentRules(
            symbol.upper(),
            _d(value["min_position_amount_usd"]),
            None
            if value.get("max_position_amount_usd") is None
            else _d(value["max_position_amount_usd"]),
            _d(value["min_stop_fraction"]),
            _d(value["max_stop_fraction"]),
            tuple(int(item) for item in value.get("leverage_values", [1])),
            _d(value.get("precision_amount_usd", "0.01")),
        )
    profiles = tuple(
        StrategyExecutionProfile(
            str(item["strategy_id"]),
            str(item["symbol"]).upper(),
            _d(item["min_amount_usd"]),
            _d(item["max_amount_usd"]),
            _d(item["min_stop_fraction"]),
            _d(item["max_stop_fraction"]),
            tuple(int(value) for value in item.get("leverage_values", [1])),
            bool(item.get("requires_multi_leg_atomicity", False)),
        )
        for item in raw.get("strategy_profiles", [])
    )
    if not profiles:
        raise ValueError("at least one v2 strategy profile is required")
    config = AppConfigV2(
        "demo",
        _d(raw["initial_cash_usd"]),
        symbols,
        mandate,
        rules,
        profiles,
        bool(raw.get("live_demo_execution_enabled", False)),
        bool(raw.get("websocket_enabled", True)),
        str(raw.get("data_catalog_path", "runtime/data-v2")),
        str(raw["postgres_dsn_file"]) if raw.get("postgres_dsn_file") else None,
    )
    executable = [item for item in config.compatibility() if item.status.value == "EXECUTABLE"]
    if config.live_demo_execution_enabled and not executable:
        raise ValueError("live DEMO master has no executable strategy profile")
    return config
