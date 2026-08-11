from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from .domain_v2 import ZERO, CompatibilityStatus


@dataclass(frozen=True)
class BrokerInstrumentRules:
    symbol: str
    min_position_amount_usd: Decimal
    max_position_amount_usd: Decimal | None
    min_stop_fraction: Decimal
    max_stop_fraction: Decimal
    leverage_values: tuple[int, ...] = (1,)
    precision_amount_usd: Decimal = Decimal("0.01")

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", self.symbol.strip().upper())
        if self.min_position_amount_usd <= ZERO or self.min_stop_fraction <= ZERO:
            raise ValueError("broker minima must be positive")
        if self.max_stop_fraction < self.min_stop_fraction:
            raise ValueError("broker stop range is invalid")


@dataclass(frozen=True)
class StrategyExecutionProfile:
    strategy_id: str
    symbol: str
    min_amount_usd: Decimal
    max_amount_usd: Decimal
    min_stop_fraction: Decimal
    max_stop_fraction: Decimal
    leverage_values: tuple[int, ...] = (1,)
    requires_multi_leg_atomicity: bool = False


@dataclass(frozen=True)
class CompatibilityResult:
    strategy_id: str
    symbol: str
    status: CompatibilityStatus
    reasons: tuple[str, ...]
    feasible_amount_min_usd: Decimal | None
    feasible_amount_max_usd: Decimal | None
    feasible_stop_min: Decimal | None
    feasible_stop_max: Decimal | None


class CompatibilityValidator:
    def __init__(
        self,
        *,
        max_order_usd: Decimal,
        max_trade_risk_usd: Decimal,
        max_gross_exposure_usd: Decimal,
    ) -> None:
        self.max_order_usd = max_order_usd
        self.max_trade_risk_usd = max_trade_risk_usd
        self.max_gross_exposure_usd = max_gross_exposure_usd

    def validate(
        self, profile: StrategyExecutionProfile, broker: BrokerInstrumentRules
    ) -> CompatibilityResult:
        reasons: list[str] = []
        if profile.symbol.upper() != broker.symbol:
            reasons.append("symbol_rule_mismatch")
        amount_min = max(profile.min_amount_usd, broker.min_position_amount_usd)
        amount_max = min(profile.max_amount_usd, self.max_order_usd, self.max_gross_exposure_usd)
        if broker.max_position_amount_usd is not None:
            amount_max = min(amount_max, broker.max_position_amount_usd)
        stop_min = max(profile.min_stop_fraction, broker.min_stop_fraction)
        stop_max = min(profile.max_stop_fraction, broker.max_stop_fraction)
        common_leverage = set(profile.leverage_values) & set(broker.leverage_values)
        if not common_leverage:
            reasons.append("leverage_incompatible")
        if amount_min > amount_max:
            reasons.append("amount_range_empty")
        if stop_min > stop_max:
            reasons.append("stop_range_empty")
        if (
            amount_min <= amount_max
            and stop_min <= stop_max
            and amount_min * stop_min > self.max_trade_risk_usd
        ):
            reasons.append("minimum_broker_order_exceeds_trade_risk")
        if profile.requires_multi_leg_atomicity:
            # eToro public API is leg-oriented; until leg-risk controls are validated this is shadow-only.
            status = CompatibilityStatus.SHADOW_ONLY
            reasons.append("multi_leg_live_adapter_not_validated")
        elif reasons:
            status = CompatibilityStatus.INVALID
        else:
            status = CompatibilityStatus.EXECUTABLE
        return CompatibilityResult(
            profile.strategy_id,
            broker.symbol,
            status,
            tuple(sorted(set(reasons))),
            amount_min if amount_min <= amount_max else None,
            amount_max if amount_min <= amount_max else None,
            stop_min if stop_min <= stop_max else None,
            stop_max if stop_min <= stop_max else None,
        )

    def validate_all(
        self,
        profiles: Iterable[StrategyExecutionProfile],
        rules: dict[str, BrokerInstrumentRules],
    ) -> tuple[CompatibilityResult, ...]:
        results = []
        for profile in profiles:
            broker = rules.get(profile.symbol.upper())
            if broker is None:
                results.append(
                    CompatibilityResult(
                        profile.strategy_id,
                        profile.symbol.upper(),
                        CompatibilityStatus.INVALID,
                        ("broker_rules_missing",),
                        None,
                        None,
                        None,
                        None,
                    )
                )
            else:
                results.append(self.validate(profile, broker))
        return tuple(results)
