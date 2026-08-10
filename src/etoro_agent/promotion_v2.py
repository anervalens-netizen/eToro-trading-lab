from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class PromotionPolicyV2:
    min_closed_trades: int = 100
    min_shadow_days: int = 30
    min_deflated_sharpe_probability: Decimal = Decimal("0.95")
    max_pbo: Decimal = Decimal("0.20")
    max_reality_check_pvalue: Decimal = Decimal("0.05")
    max_drawdown_fraction: Decimal = Decimal("0.10")
    min_profit_factor: Decimal = Decimal("1.10")
    min_cost_stress_multiple: Decimal = Decimal("2")
    max_parity_delta_usd: Decimal = Decimal("0.01")
    max_unknown_orders: int = 0
    max_critical_incidents: int = 0


@dataclass(frozen=True)
class PromotionEvidenceV2:
    closed_trades: int
    shadow_days: int
    deflated_sharpe_probability: Decimal
    pbo: Decimal
    reality_check_pvalue: Decimal
    max_drawdown_fraction: Decimal
    profit_factor: Decimal
    profitable_at_cost_stress_multiple: Decimal
    parity_delta_usd: Decimal
    unknown_orders: int
    critical_incidents: int
    untouched_test_consumed: bool
    net_untouched_pnl_usd: Decimal


@dataclass(frozen=True)
class PromotionDecisionV2:
    decision: str
    reasons: tuple[str, ...]


class PromotionGateV2:
    def __init__(self, policy: PromotionPolicyV2 | None = None) -> None:
        self.policy = policy or PromotionPolicyV2()

    def evaluate(self, evidence: PromotionEvidenceV2) -> PromotionDecisionV2:
        p = self.policy
        reasons: list[str] = []
        if evidence.closed_trades < p.min_closed_trades:
            reasons.append("insufficient_closed_trades")
        if evidence.shadow_days < p.min_shadow_days:
            reasons.append("insufficient_shadow_days")
        if evidence.deflated_sharpe_probability < p.min_deflated_sharpe_probability:
            reasons.append("deflated_sharpe_gate")
        if evidence.pbo > p.max_pbo:
            reasons.append("pbo_gate")
        if evidence.reality_check_pvalue > p.max_reality_check_pvalue:
            reasons.append("reality_check_gate")
        if evidence.max_drawdown_fraction > p.max_drawdown_fraction:
            reasons.append("drawdown_gate")
        if evidence.profit_factor < p.min_profit_factor:
            reasons.append("profit_factor_gate")
        if evidence.profitable_at_cost_stress_multiple < p.min_cost_stress_multiple:
            reasons.append("cost_stress_gate")
        if abs(evidence.parity_delta_usd) > p.max_parity_delta_usd:
            reasons.append("parity_gate")
        if evidence.unknown_orders > p.max_unknown_orders:
            reasons.append("unknown_order_gate")
        if evidence.critical_incidents > p.max_critical_incidents:
            reasons.append("critical_incident_gate")
        if not evidence.untouched_test_consumed:
            reasons.append("untouched_test_not_run")
        elif evidence.net_untouched_pnl_usd <= 0:
            reasons.append("untouched_test_not_profitable")
        return PromotionDecisionV2("PROMOTE" if not reasons else "CONTINUE_SHADOW", tuple(reasons))
