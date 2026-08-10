from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from .domain_v2 import BPS, IntentEnvelope, QuoteProvenance, ZERO, utc


@dataclass(frozen=True)
class CapitalMandate:
    allowed_symbols: frozenset[str]
    max_order_usd: Decimal
    max_trade_risk_usd: Decimal
    max_gross_exposure_usd: Decimal
    max_correlated_exposure_usd: Decimal
    max_open_positions: int
    max_daily_loss_usd: Decimal
    max_weekly_loss_usd: Decimal
    max_monthly_loss_usd: Decimal
    reduce_only_drawdown_fraction: Decimal
    lock_drawdown_fraction: Decimal
    max_quote_age_seconds: int
    max_spread_bps: Decimal
    max_mid_drift_bps: Decimal
    min_trade_interval_seconds: int = 0
    max_leverage: int = 1

    def __post_init__(self) -> None:
        if not self.allowed_symbols:
            raise ValueError("allowed_symbols cannot be empty")
        if min(
            self.max_order_usd,
            self.max_trade_risk_usd,
            self.max_gross_exposure_usd,
            self.max_correlated_exposure_usd,
            self.max_daily_loss_usd,
            self.max_weekly_loss_usd,
            self.max_monthly_loss_usd,
        ) <= ZERO:
            raise ValueError("capital limits must be positive")
        if not ZERO < self.reduce_only_drawdown_fraction < self.lock_drawdown_fraction < Decimal("1"):
            raise ValueError("drawdown thresholds are invalid")
        if self.max_open_positions < 1 or self.max_quote_age_seconds < 1:
            raise ValueError("position/quote limits are invalid")


@dataclass(frozen=True)
class BrokerTruth:
    equity_usd: Decimal
    peak_equity_usd: Decimal
    available_cash_usd: Decimal
    gross_exposure_usd: Decimal
    correlated_exposure_usd: Decimal
    open_positions: int
    pending_order_notional_usd: Decimal
    daily_pnl_usd: Decimal
    weekly_pnl_usd: Decimal
    monthly_pnl_usd: Decimal
    snapshot_hash: str
    observed_at: datetime
    last_trade_at: datetime | None = None
    reconciliation_ok: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "observed_at", utc(self.observed_at))
        if self.last_trade_at is not None:
            object.__setattr__(self, "last_trade_at", utc(self.last_trade_at))
        if self.equity_usd <= ZERO or self.peak_equity_usd <= ZERO:
            raise ValueError("broker equity is invalid")
        if self.available_cash_usd < ZERO or self.pending_order_notional_usd < ZERO:
            raise ValueError("broker cash/pending order state is invalid")
        if not self.snapshot_hash.strip():
            raise ValueError("broker snapshot hash is required")

    @property
    def drawdown_fraction(self) -> Decimal:
        return max(ZERO, (self.peak_equity_usd - self.equity_usd) / self.peak_equity_usd)


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    reasons: tuple[str, ...]
    reduce_only: bool = False
    mandate_state: str = "ACTIVE"


class GlobalRiskKernel:
    """Deterministic capital contract. It never approves based on an LLM rationale."""

    version = "risk-v2.0"

    def __init__(self, mandate: CapitalMandate) -> None:
        self.mandate = mandate

    def evaluate_open(
        self,
        intent: IntentEnvelope,
        quote: QuoteProvenance,
        broker: BrokerTruth,
        now: datetime,
    ) -> RiskDecision:
        current = utc(now)
        reasons: list[str] = []
        m = self.mandate
        if not broker.reconciliation_ok:
            reasons.append("reconciliation_drift")
        if intent.symbol not in m.allowed_symbols:
            reasons.append("symbol_not_allowed")
        if not intent.is_live(current):
            reasons.append("intent_expired_or_not_yet_valid")
        if quote.symbol != intent.symbol:
            reasons.append("quote_symbol_mismatch")
        if quote.broker_snapshot_hash != broker.snapshot_hash:
            reasons.append("broker_snapshot_hash_mismatch")
        quote_age = quote.age_seconds(current)
        if quote_age < ZERO:
            reasons.append("future_quote")
        elif quote_age > Decimal(m.max_quote_age_seconds):
            reasons.append("stale_quote")
        if quote.spread_bps > m.max_spread_bps:
            reasons.append("wide_spread")
        if intent.drift_bps(quote) > min(m.max_mid_drift_bps, intent.max_price_drift_bps):
            reasons.append("price_drift")
        projected_risk = intent.amount_usd * intent.stop_loss_fraction
        if projected_risk > m.max_trade_risk_usd:
            reasons.append("trade_risk_limit")
        if intent.amount_usd > m.max_order_usd:
            reasons.append("order_notional_limit")
        if intent.amount_usd > broker.available_cash_usd:
            reasons.append("available_cash_limit")
        if broker.gross_exposure_usd + broker.pending_order_notional_usd + intent.amount_usd > m.max_gross_exposure_usd:
            reasons.append("gross_exposure_limit")
        if broker.correlated_exposure_usd + intent.amount_usd > m.max_correlated_exposure_usd:
            reasons.append("correlated_exposure_limit")
        if broker.open_positions >= m.max_open_positions:
            reasons.append("open_position_limit")
        if broker.daily_pnl_usd <= -m.max_daily_loss_usd:
            reasons.append("daily_loss_limit")
        if broker.weekly_pnl_usd <= -m.max_weekly_loss_usd:
            reasons.append("weekly_loss_limit")
        if broker.monthly_pnl_usd <= -m.max_monthly_loss_usd:
            reasons.append("monthly_loss_limit")
        if broker.last_trade_at is not None and m.min_trade_interval_seconds:
            elapsed = (current - broker.last_trade_at).total_seconds()
            if elapsed < m.min_trade_interval_seconds:
                reasons.append("trade_cooldown")
        if broker.drawdown_fraction >= m.lock_drawdown_fraction:
            reasons.append("drawdown_lock")
        elif broker.drawdown_fraction >= m.reduce_only_drawdown_fraction:
            reasons.append("drawdown_reduce_only")
        return RiskDecision(
            not reasons,
            tuple(sorted(set(reasons))),
            reduce_only=(broker.drawdown_fraction >= m.reduce_only_drawdown_fraction),
            mandate_state=(
                "LOCKED"
                if broker.drawdown_fraction >= m.lock_drawdown_fraction
                else "REDUCE_ONLY"
                if broker.drawdown_fraction >= m.reduce_only_drawdown_fraction
                else "ACTIVE"
            ),
        )

    def evaluate_reduce(self, broker: BrokerTruth) -> RiskDecision:
        # Risk-reducing exits remain available even after loss/drawdown gates.
        if not broker.reconciliation_ok:
            return RiskDecision(False, ("reconciliation_drift",), True, "LOCKED")
        return RiskDecision(True, (), True, "REDUCE_ONLY")
