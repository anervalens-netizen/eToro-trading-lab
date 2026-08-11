from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True)
class TradeIntent:
    symbol: str
    side: Side
    amount_usd: Decimal
    confidence: Decimal
    rationale: str
    stop_loss_fraction: Decimal
    take_profit_fraction: Decimal
    leverage: int = 1
    strategy_id: str = "legacy_ma"
    strategy_version: str = "1"
    portfolio_id: str = "shadow-legacy"
    signal_ts: int = 0
    max_holding_seconds: int = 86_400
    market_snapshot_hash: str = ""


@dataclass(frozen=True)
class CloseIntent:
    symbol: str
    position_id: int
    instrument_id: int
    units_to_deduct: Decimal | None
    rationale: str
    strategy_id: str = "ai_master"
    portfolio_id: str = "master_1000"


@dataclass(frozen=True)
class RiskContext:
    equity_usd: Decimal
    peak_equity_usd: Decimal
    daily_pnl_usd: Decimal
    gross_exposure_usd: Decimal
    symbol_exposure_usd: Decimal
    trades_today: int
    bid: Decimal
    ask: Decimal
    kill_switch_active: bool
    weekly_pnl_usd: Decimal = Decimal("0")
    monthly_pnl_usd: Decimal = Decimal("0")
    correlated_exposure_usd: Decimal = Decimal("0")
    open_positions: int = 0
    quote_observed_at: int = 0
    evaluated_at: int = 0
    last_trade_at: int = 0
    data_quality_ok: bool = True
    audit_writable: bool = True
    reconciliation_ok: bool = True


@dataclass(frozen=True)
class ApprovedOrder:
    proposal_id: str
    route: str
    method: str
    body_json: str
    issued_at: int
    expires_at: int
    seal: str
    account_mode: str = "DEMO"
    request_id: str = ""
    intent_hash: str = ""
    risk_snapshot_hash: str = ""
    risk_config_hash: str = ""
    quote_observed_at: int = 0
    signature_algorithm: str = "Ed25519"


@dataclass(frozen=True)
class RiskResult:
    approved: bool
    reasons: tuple[str, ...]
    order: ApprovedOrder | None = None


class KillState(StrEnum):
    ACTIVE = "ACTIVE"
    HALT_NEW = "HALT_NEW"
    REDUCE_ONLY = "REDUCE_ONLY"
    LOCKED = "LOCKED"


class ExecutionState(StrEnum):
    PROPOSED = "PROPOSED"
    RISK_REJECTED = "RISK_REJECTED"
    SEALED = "SEALED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPROVED = "APPROVED"
    SENDING = "SENDING"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    UNKNOWN = "UNKNOWN"
    REJECTED = "REJECTED"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    RECONCILED = "RECONCILED"
