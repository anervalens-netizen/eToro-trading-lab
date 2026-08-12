from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from .compatibility_v2 import (
    BrokerInstrumentRules,
    CompatibilityResult,
    CompatibilityValidator,
    StrategyExecutionProfile,
)
from .risk_seal_v2 import STANDING_DEMO_PROPOSAL_SOURCES
from .risk_v2 import CapitalMandate
from .strict_parsing_v2 import (
    load_strict_json_object,
    strict_bool,
    strict_decimal,
    strict_int,
    strict_list,
    strict_object,
    strict_string,
)


@dataclass(frozen=True)
class AppConfigV2:
    account_mode: str
    portfolio_id: str
    model_id: str
    release_namespace: str
    initial_cash_usd: Decimal
    symbols: dict[str, int]
    research_only_symbols: frozenset[str]
    mandate: CapitalMandate
    broker_rules: dict[str, BrokerInstrumentRules]
    strategy_profiles: tuple[StrategyExecutionProfile, ...]
    live_demo_execution_enabled: bool
    allowed_proposal_sources: frozenset[str]
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


_TOP_REQUIRED = {
    "account_mode",
    "portfolio_id",
    "model_id",
    "release_namespace",
    "initial_cash_usd",
    "symbols",
    "capital_mandate",
    "broker_rules",
    "strategy_profiles",
}
_TOP_OPTIONAL = {
    "research_only_symbols",
    "live_demo_execution_enabled",
    "allowed_proposal_sources",
    "websocket_enabled",
    "data_catalog_path",
    "postgres_dsn_file",
}
_MANDATE_FIELDS = {
    "allowed_symbols",
    "max_order_usd",
    "max_trade_risk_usd",
    "max_gross_exposure_usd",
    "max_correlated_exposure_usd",
    "max_open_positions",
    "max_daily_loss_usd",
    "max_weekly_loss_usd",
    "max_monthly_loss_usd",
    "reduce_only_drawdown_fraction",
    "lock_drawdown_fraction",
    "max_quote_age_seconds",
    "max_spread_bps",
    "max_mid_drift_bps",
    "risk_period_timezone",
}
_MANDATE_OPTIONAL = {"min_trade_interval_seconds", "max_leverage"}
_BROKER_RULE_REQUIRED = {
    "min_position_amount_usd",
    "min_stop_fraction",
    "max_stop_fraction",
}
_BROKER_RULE_OPTIONAL = {
    "max_position_amount_usd",
    "leverage_values",
    "precision_amount_usd",
}
_PROFILE_REQUIRED = {
    "strategy_id",
    "symbol",
    "min_amount_usd",
    "max_amount_usd",
    "min_stop_fraction",
    "max_stop_fraction",
}
_PROFILE_OPTIONAL = {"leverage_values", "requires_multi_leg_atomicity"}


def _unique_strings(value: object, *, label: str, upper: bool = False) -> tuple[str, ...]:
    normalized = tuple(
        strict_string(item, label=f"{label} item") for item in strict_list(value, label=label)
    )
    if upper:
        normalized = tuple(item.upper() for item in normalized)
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{label} contains duplicate values")
    return normalized


def _leverage_values(value: object, *, label: str) -> tuple[int, ...]:
    values = tuple(
        strict_int(item, label=f"{label} item", minimum=1)
        for item in strict_list(value, label=label)
    )
    if values != (1,):
        raise ValueError(f"{label} must contain exactly leverage 1")
    return values


def load_config_v2(path: str | Path) -> AppConfigV2:
    raw = strict_object(
        load_strict_json_object(path),
        label="v2 configuration",
        required=_TOP_REQUIRED,
        optional=_TOP_OPTIONAL,
    )
    if raw.get("account_mode") != "demo":
        raise ValueError("v2 runtime supports DEMO only; REAL is a separate future release")
    portfolio_id = strict_string(raw["portfolio_id"], label="portfolio_id")
    model_id = strict_string(raw["model_id"], label="model_id")
    release_namespace = strict_string(raw["release_namespace"], label="release_namespace")
    if re.fullmatch(r"demo-[a-z0-9][a-z0-9-]{2,63}", portfolio_id) is None:
        raise ValueError("portfolio_id must be an explicit DEMO namespace")
    if model_id != "gpt-5.6-sol":
        raise ValueError("model_id must match the attested release model")
    if re.fullmatch(r"etoro-v2-demo-[a-z0-9][a-z0-9.-]{2,63}", release_namespace) is None:
        raise ValueError("release_namespace is invalid")
    raw_symbols = raw["symbols"]
    if not isinstance(raw_symbols, dict) or not raw_symbols:
        raise ValueError("symbols must be a non-empty object")
    symbols: dict[str, int] = {}
    for key, value in raw_symbols.items():
        symbol = strict_string(key, label="symbol key").upper()
        if symbol in symbols:
            raise ValueError("symbols contains duplicate normalized keys")
        symbols[symbol] = strict_int(value, label=f"symbols.{symbol}", minimum=1)
    m = strict_object(
        raw["capital_mandate"],
        label="capital_mandate",
        required=_MANDATE_FIELDS,
        optional=_MANDATE_OPTIONAL,
    )
    max_open_positions = strict_int(
        m["max_open_positions"], label="capital_mandate.max_open_positions", minimum=1
    )
    if max_open_positions != 1:
        raise ValueError("capital_mandate.max_open_positions must be exactly 1")
    max_leverage = strict_int(
        m.get("max_leverage", 1), label="capital_mandate.max_leverage", minimum=1
    )
    if max_leverage != 1:
        raise ValueError("capital_mandate.max_leverage must be exactly 1")
    mandate = CapitalMandate(
        allowed_symbols=frozenset(
            _unique_strings(
                m["allowed_symbols"], label="capital_mandate.allowed_symbols", upper=True
            )
        ),
        max_order_usd=strict_decimal(m["max_order_usd"], label="max_order_usd", positive=True),
        max_trade_risk_usd=strict_decimal(
            m["max_trade_risk_usd"], label="max_trade_risk_usd", positive=True
        ),
        max_gross_exposure_usd=strict_decimal(
            m["max_gross_exposure_usd"], label="max_gross_exposure_usd", positive=True
        ),
        max_correlated_exposure_usd=strict_decimal(
            m["max_correlated_exposure_usd"],
            label="max_correlated_exposure_usd",
            positive=True,
        ),
        max_open_positions=max_open_positions,
        max_daily_loss_usd=strict_decimal(
            m["max_daily_loss_usd"], label="max_daily_loss_usd", positive=True
        ),
        max_weekly_loss_usd=strict_decimal(
            m["max_weekly_loss_usd"], label="max_weekly_loss_usd", positive=True
        ),
        max_monthly_loss_usd=strict_decimal(
            m["max_monthly_loss_usd"], label="max_monthly_loss_usd", positive=True
        ),
        reduce_only_drawdown_fraction=strict_decimal(
            m["reduce_only_drawdown_fraction"],
            label="reduce_only_drawdown_fraction",
            positive=True,
        ),
        lock_drawdown_fraction=strict_decimal(
            m["lock_drawdown_fraction"], label="lock_drawdown_fraction", positive=True
        ),
        max_quote_age_seconds=strict_int(
            m["max_quote_age_seconds"], label="max_quote_age_seconds", minimum=1
        ),
        max_spread_bps=strict_decimal(m["max_spread_bps"], label="max_spread_bps", positive=True),
        max_mid_drift_bps=strict_decimal(
            m["max_mid_drift_bps"], label="max_mid_drift_bps", positive=True
        ),
        min_trade_interval_seconds=strict_int(
            m.get("min_trade_interval_seconds", 0),
            label="min_trade_interval_seconds",
            minimum=0,
        ),
        max_leverage=max_leverage,
        risk_period_timezone=strict_string(
            m["risk_period_timezone"], label="capital_mandate.risk_period_timezone"
        ),
    )
    initial_cash = strict_decimal(raw["initial_cash_usd"], label="initial_cash_usd", positive=True)
    research_only = frozenset(
        _unique_strings(
            raw.get("research_only_symbols", []), label="research_only_symbols", upper=True
        )
    )
    if not research_only <= frozenset(symbols):
        raise ValueError("research_only_symbols must be in the fixed symbol catalog")
    if mandate.allowed_symbols != frozenset(symbols) - research_only:
        raise ValueError("capital mandate allowed_symbols must equal executable catalog symbols")
    rules: dict[str, BrokerInstrumentRules] = {}
    raw_rules = raw["broker_rules"]
    if not isinstance(raw_rules, dict) or not raw_rules:
        raise ValueError("broker_rules must be a non-empty object")
    for raw_symbol, raw_value in raw_rules.items():
        symbol = strict_string(raw_symbol, label="broker_rules symbol").upper()
        if symbol in rules:
            raise ValueError("broker_rules contains duplicate normalized keys")
        value = strict_object(
            raw_value,
            label=f"broker_rules.{symbol}",
            required=_BROKER_RULE_REQUIRED,
            optional=_BROKER_RULE_OPTIONAL,
        )
        minimum_amount = strict_decimal(
            value["min_position_amount_usd"],
            label=f"broker_rules.{symbol}.min_position_amount_usd",
            positive=True,
        )
        maximum_amount = (
            None
            if value.get("max_position_amount_usd") is None
            else strict_decimal(
                value["max_position_amount_usd"],
                label=f"broker_rules.{symbol}.max_position_amount_usd",
                positive=True,
            )
        )
        minimum_stop = strict_decimal(
            value["min_stop_fraction"],
            label=f"broker_rules.{symbol}.min_stop_fraction",
            positive=True,
        )
        maximum_stop = strict_decimal(
            value["max_stop_fraction"],
            label=f"broker_rules.{symbol}.max_stop_fraction",
            positive=True,
        )
        if maximum_amount is not None and maximum_amount < minimum_amount:
            raise ValueError(f"broker_rules.{symbol} amount range is invalid")
        if maximum_stop < minimum_stop or maximum_stop >= 1:
            raise ValueError(f"broker_rules.{symbol} stop range is invalid")
        rules[symbol.upper()] = BrokerInstrumentRules(
            symbol.upper(),
            minimum_amount,
            maximum_amount,
            minimum_stop,
            maximum_stop,
            _leverage_values(
                value.get("leverage_values", [1]),
                label=f"broker_rules.{symbol}.leverage_values",
            ),
            strict_decimal(
                value.get("precision_amount_usd", Decimal("0.01")),
                label=f"broker_rules.{symbol}.precision_amount_usd",
                positive=True,
            ),
        )
    profiles_list: list[StrategyExecutionProfile] = []
    for index, raw_item in enumerate(
        strict_list(raw["strategy_profiles"], label="strategy_profiles")
    ):
        item = strict_object(
            raw_item,
            label=f"strategy_profiles[{index}]",
            required=_PROFILE_REQUIRED,
            optional=_PROFILE_OPTIONAL,
        )
        minimum_amount = strict_decimal(
            item["min_amount_usd"], label=f"strategy_profiles[{index}].min_amount", positive=True
        )
        maximum_amount = strict_decimal(
            item["max_amount_usd"], label=f"strategy_profiles[{index}].max_amount", positive=True
        )
        minimum_stop = strict_decimal(
            item["min_stop_fraction"], label=f"strategy_profiles[{index}].min_stop", positive=True
        )
        maximum_stop = strict_decimal(
            item["max_stop_fraction"], label=f"strategy_profiles[{index}].max_stop", positive=True
        )
        if maximum_amount < minimum_amount or maximum_stop < minimum_stop or maximum_stop >= 1:
            raise ValueError(f"strategy_profiles[{index}] economic range is invalid")
        profiles_list.append(
            StrategyExecutionProfile(
                strict_string(item["strategy_id"], label=f"strategy_profiles[{index}].strategy_id"),
                strict_string(item["symbol"], label=f"strategy_profiles[{index}].symbol").upper(),
                minimum_amount,
                maximum_amount,
                minimum_stop,
                maximum_stop,
                _leverage_values(
                    item.get("leverage_values", [1]),
                    label=f"strategy_profiles[{index}].leverage_values",
                ),
                strict_bool(
                    item.get("requires_multi_leg_atomicity", False),
                    label=f"strategy_profiles[{index}].requires_multi_leg_atomicity",
                ),
            )
        )
    profiles = tuple(profiles_list)
    if not profiles:
        raise ValueError("at least one v2 strategy profile is required")
    allowed_sources = frozenset(
        _unique_strings(raw.get("allowed_proposal_sources", []), label="allowed_proposal_sources")
    )
    if not allowed_sources <= STANDING_DEMO_PROPOSAL_SOURCES:
        raise ValueError("v2 proposal source allowlist exceeds the DEMO standing mandate")
    live_enabled = strict_bool(
        raw.get("live_demo_execution_enabled", False), label="live_demo_execution_enabled"
    )
    if live_enabled and allowed_sources != STANDING_DEMO_PROPOSAL_SOURCES:
        raise ValueError("live v2 DEMO execution requires the exact standing proposal allowlist")
    config = AppConfigV2(
        "demo",
        portfolio_id,
        model_id,
        release_namespace,
        initial_cash,
        symbols,
        research_only,
        mandate,
        rules,
        profiles,
        live_enabled,
        allowed_sources,
        strict_bool(raw.get("websocket_enabled", True), label="websocket_enabled"),
        strict_string(raw.get("data_catalog_path", "runtime/data-v2"), label="data_catalog_path"),
        strict_string(raw["postgres_dsn_file"], label="postgres_dsn_file")
        if raw.get("postgres_dsn_file") is not None
        else None,
    )
    compatibility = config.compatibility()
    executable = [item for item in compatibility if item.status.value == "EXECUTABLE"]
    if config.live_demo_execution_enabled and not executable:
        raise ValueError("live DEMO master has no executable strategy profile")
    if config.live_demo_execution_enabled and len(executable) != len(compatibility):
        raise ValueError("live DEMO config contains a research-only or incompatible profile")
    if config.live_demo_execution_enabled and any(
        profile.symbol in config.research_only_symbols for profile in config.strategy_profiles
    ):
        raise ValueError("live DEMO config contains a research-only symbol profile")
    return config
