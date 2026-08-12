from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import Enum, StrEnum
from typing import Any, cast

ZERO = Decimal("0")
ONE = Decimal("1")
BPS = Decimal("10000")


class AuditIntegrityError(RuntimeError):
    """An idempotency key was rebound to a different canonical event body."""


def utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC)


def canonical_json(value: Any) -> str:
    def default(item: Any) -> Any:
        if isinstance(item, Decimal):
            return str(item)
        if isinstance(item, datetime):
            return utc(item).isoformat()
        if isinstance(item, Enum):
            return item.value
        if isinstance(item, tuple):
            return list(item)
        raise TypeError(type(item).__name__)

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        default=default,
        allow_nan=False,
    )


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"

    @property
    def direction(self) -> Decimal:
        return ONE if self is Side.BUY else Decimal("-1")


class PositionStatus(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class OrderStatus(StrEnum):
    CREATED = "CREATED"
    RISK_APPROVED = "RISK_APPROVED"
    SUBMITTING = "SUBMITTING"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"
    RECONCILED_FILLED = "RECONCILED_FILLED"
    RECONCILED_ABSENT = "RECONCILED_ABSENT"
    MANUAL_REVIEW = "MANUAL_REVIEW"


TERMINAL_ORDER_STATES = frozenset(
    {
        OrderStatus.FILLED,
        OrderStatus.REJECTED,
        OrderStatus.CANCELLED,
        OrderStatus.EXPIRED,
        OrderStatus.RECONCILED_FILLED,
        OrderStatus.RECONCILED_ABSENT,
        OrderStatus.MANUAL_REVIEW,
    }
)


class ExitReason(StrEnum):
    AGENT_CLOSE = "AGENT_CLOSE"
    REDUCE_ONLY = "REDUCE_ONLY"
    DATA_INVALIDATION = "DATA_INVALIDATION"
    GAP_STOP = "GAP_STOP"
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"
    TIME_STOP = "TIME_STOP"
    STRATEGY_INVALIDATION = "STRATEGY_INVALIDATION"
    OVERNIGHT_POLICY = "OVERNIGHT_POLICY"
    END_OF_TEST = "END_OF_TEST"
    BROKER_RECONCILIATION = "BROKER_RECONCILIATION"
    UNCLASSIFIED_BROKER = "UNCLASSIFIED_BROKER"


class CompatibilityStatus(StrEnum):
    EXECUTABLE = "EXECUTABLE"
    SHADOW_ONLY = "SHADOW_ONLY"
    INVALID = "INVALID"


@dataclass(frozen=True)
class QuoteProvenance:
    symbol: str
    bid: Decimal
    ask: Decimal
    quote_observed_at: datetime
    quote_received_at: datetime
    quote_source: str
    quote_sequence_or_event_id: str
    market_snapshot_hash: str
    broker_snapshot_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", self.symbol.strip().upper())
        object.__setattr__(self, "quote_observed_at", utc(self.quote_observed_at))
        object.__setattr__(self, "quote_received_at", utc(self.quote_received_at))
        if not self.symbol:
            raise ValueError("quote symbol is required")
        if not all(value.is_finite() for value in (self.bid, self.ask)):
            raise ValueError("quote bid/ask must be finite")
        if self.bid <= ZERO or self.ask <= ZERO or self.ask < self.bid:
            raise ValueError("quote bid/ask is invalid")
        if self.quote_observed_at > self.quote_received_at + timedelta(seconds=5):
            raise ValueError("quote timestamp is materially in the future")
        for name, value in (
            ("quote_source", self.quote_source),
            ("quote_sequence_or_event_id", self.quote_sequence_or_event_id),
            ("market_snapshot_hash", self.market_snapshot_hash),
            ("broker_snapshot_hash", self.broker_snapshot_hash),
        ):
            if not str(value).strip():
                raise ValueError(f"{name} is required")

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / Decimal("2")

    @property
    def spread_bps(self) -> Decimal:
        return (self.ask - self.bid) / self.mid * BPS

    def age_seconds(self, now: datetime) -> Decimal:
        return Decimal(str((utc(now) - self.quote_observed_at).total_seconds()))


@dataclass(frozen=True)
class IntentEnvelope:
    intent_id: str
    portfolio_id: str
    lane_id: str
    strategy_id: str
    strategy_version: str
    symbol: str
    side: Side
    amount_usd: Decimal
    raw_confidence: Decimal
    confidence_threshold: Decimal
    stop_loss_fraction: Decimal
    take_profit_fraction: Decimal
    max_holding_seconds: int
    created_at: datetime
    valid_after: datetime
    expires_at: datetime
    reference_bid: Decimal
    reference_ask: Decimal
    max_price_drift_bps: Decimal
    max_slippage_bps: Decimal
    snapshot_hash: str
    rationale: str = ""
    invalidation_conditions: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    model_version: str = "deterministic"
    prompt_version: str = "none"
    correlation_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", self.symbol.strip().upper())
        object.__setattr__(self, "created_at", utc(self.created_at))
        object.__setattr__(self, "valid_after", utc(self.valid_after))
        object.__setattr__(self, "expires_at", utc(self.expires_at))
        if (
            not self.intent_id.strip()
            or not self.portfolio_id.strip()
            or not self.lane_id.strip()
            or not self.strategy_id.strip()
            or not self.strategy_version.strip()
            or not self.symbol
        ):
            raise ValueError("intent identity is incomplete")
        numeric: tuple[Decimal, ...] = (
            self.amount_usd,
            self.raw_confidence,
            self.confidence_threshold,
            self.stop_loss_fraction,
            self.take_profit_fraction,
            self.reference_bid,
            self.reference_ask,
            self.max_price_drift_bps,
            self.max_slippage_bps,
        )
        if not all(value.is_finite() for value in numeric):
            raise ValueError("intent economics must be finite")
        if self.amount_usd <= ZERO:
            raise ValueError("intent amount must be positive")
        if not ZERO <= self.raw_confidence <= ONE:
            raise ValueError("raw confidence must be in [0,1]")
        if not ZERO <= self.confidence_threshold <= ONE:
            raise ValueError("confidence threshold must be in [0,1]")
        if self.raw_confidence < self.confidence_threshold:
            raise ValueError("sub-threshold signal must be HOLD, not an IntentEnvelope")
        if self.stop_loss_fraction <= ZERO or self.take_profit_fraction <= ZERO:
            raise ValueError("stop and take-profit must be positive")
        if self.max_holding_seconds < 1:
            raise ValueError("max holding must be positive")
        if not self.created_at <= self.valid_after <= self.expires_at:
            raise ValueError("intent time bounds are invalid")
        if self.reference_bid <= ZERO or self.reference_ask < self.reference_bid:
            raise ValueError("intent reference quote is invalid")
        if self.max_price_drift_bps < ZERO or self.max_slippage_bps < ZERO:
            raise ValueError("drift/slippage limits cannot be negative")
        if not self.snapshot_hash.strip():
            raise ValueError("snapshot hash is required")

    @property
    def reference_mid(self) -> Decimal:
        return (self.reference_bid + self.reference_ask) / Decimal("2")

    def is_live(self, now: datetime) -> bool:
        current = utc(now)
        return self.valid_after <= current <= self.expires_at

    def drift_bps(self, quote: QuoteProvenance) -> Decimal:
        if quote.symbol != self.symbol:
            raise ValueError("intent/quote symbol mismatch")
        return abs(quote.mid / self.reference_mid - ONE) * BPS


@dataclass(frozen=True)
class PositionState:
    position_id: str
    portfolio_id: str
    strategy_id: str
    lane_id: str
    strategy_version: str
    intent_id: str
    symbol: str
    side: Side
    quantity: Decimal
    entry_price: Decimal
    entry_event_time: datetime
    entry_processing_time: datetime
    stop_price: Decimal
    take_profit_price: Decimal
    stop_fraction: Decimal
    take_profit_fraction: Decimal
    max_holding_seconds: int
    expires_at: datetime
    financing_accrued: Decimal = ZERO
    fees_accrued: Decimal = ZERO
    realized_pnl: Decimal = ZERO
    unrealized_pnl: Decimal = ZERO
    status: PositionStatus = PositionStatus.OPEN
    exit_reason: ExitReason | None = None
    broker_position_id: str | None = None
    last_mark: Decimal | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", self.symbol.strip().upper())
        object.__setattr__(self, "entry_event_time", utc(self.entry_event_time))
        object.__setattr__(self, "entry_processing_time", utc(self.entry_processing_time))
        object.__setattr__(self, "expires_at", utc(self.expires_at))
        numeric: tuple[Decimal, ...] = (
            self.quantity,
            self.entry_price,
            self.stop_price,
            self.take_profit_price,
            self.stop_fraction,
            self.take_profit_fraction,
            self.financing_accrued,
            self.fees_accrued,
            self.realized_pnl,
            self.unrealized_pnl,
        )
        if self.last_mark is not None:
            numeric += (self.last_mark,)
        if not all(value.is_finite() for value in numeric):
            raise ValueError("position economics must be finite")
        if not all(
            str(value).strip()
            for value in (
                self.position_id,
                self.portfolio_id,
                self.strategy_id,
                self.lane_id,
                self.strategy_version,
                self.intent_id,
                self.symbol,
            )
        ):
            raise ValueError("position identity is incomplete")
        if self.quantity < ZERO:
            raise ValueError("position quantity is unsigned and cannot be negative")
        if (
            min(
                self.entry_price,
                self.stop_price,
                self.take_profit_price,
                self.stop_fraction,
                self.take_profit_fraction,
            )
            <= ZERO
        ):
            raise ValueError("position prices and stop/take fractions must be positive")
        if self.max_holding_seconds < 1:
            raise ValueError("position maximum holding period must be positive")
        if not self.entry_event_time <= self.entry_processing_time:
            raise ValueError("position processing time precedes its entry event")
        if self.expires_at < self.entry_event_time:
            raise ValueError("position expiry precedes its entry event")
        if (
            self.side is Side.BUY
            and not self.stop_price < self.entry_price < self.take_profit_price
        ):
            raise ValueError("long position stop/entry/take ordering is invalid")
        if (
            self.side is Side.SELL
            and not self.take_profit_price < self.entry_price < self.stop_price
        ):
            raise ValueError("short position take/entry/stop ordering is invalid")
        if min(self.financing_accrued, self.fees_accrued) < ZERO:
            raise ValueError("fees/financing cannot be negative")
        if self.status is PositionStatus.OPEN and self.quantity <= ZERO:
            raise ValueError("open position must have positive quantity")
        if self.status is PositionStatus.OPEN and self.exit_reason is not None:
            raise ValueError("open position cannot have an exit reason")
        if self.status is PositionStatus.CLOSED and (
            self.quantity != ZERO or self.exit_reason is None
        ):
            raise ValueError("closed position requires zero quantity and an exit reason")
        if self.last_mark is not None and self.last_mark <= ZERO:
            raise ValueError("position last mark must be positive")

    @property
    def signed_quantity(self) -> Decimal:
        return self.quantity * self.side.direction

    def market_value(self, mark: Decimal) -> Decimal:
        if mark <= ZERO:
            raise ValueError("mark must be positive")
        return self.quantity * mark

    def gross_exposure(self, mark: Decimal) -> Decimal:
        return abs(self.market_value(mark))

    def pnl_at(self, mark: Decimal) -> Decimal:
        if mark <= ZERO:
            raise ValueError("mark must be positive")
        gross = self.quantity * self.side.direction * (mark - self.entry_price)
        return gross - self.fees_accrued - self.financing_accrued

    def with_mark(self, mark: Decimal) -> PositionState:
        values = asdict(self)
        values["last_mark"] = mark
        values["unrealized_pnl"] = self.pnl_at(mark)
        return PositionState(**values)


def reduce_command_provenance_hash(
    *,
    position_hash: str,
    broker_position_id: str,
    quantity_before: Decimal,
    units: Decimal,
    exit_reason: str,
    broker_snapshot_hash: str,
    risk_config_hash: str,
) -> str:
    """Bind a reduce command to one exact local/broker economic snapshot."""

    return canonical_hash(
        {
            "position_hash": position_hash,
            "broker_position_id": broker_position_id,
            "quantity_before": quantity_before,
            "units": units,
            "exit_reason": exit_reason,
            "broker_snapshot_hash": broker_snapshot_hash,
            "risk_config_hash": risk_config_hash,
        }
    )


@dataclass(frozen=True)
class OrderCommand:
    order_command_id: str
    intent_id: str
    proposal_id: str
    client_order_id: str
    portfolio_id: str
    symbol: str
    side: Side
    amount_usd: Decimal
    quantity: Decimal | None
    reduce_only: bool
    created_at: datetime
    expires_at: datetime
    idempotency_key: str
    correlation_id: str
    intent_hash: str = ""
    reference_entry: Decimal | None = None
    min_acceptable_entry: Decimal | None = None
    max_acceptable_entry: Decimal | None = None
    stop_loss_fraction: Decimal | None = None
    take_profit_fraction: Decimal | None = None
    max_slippage_bps: Decimal | None = None
    max_loss_usd: Decimal | None = None
    available_loss_budget_usd: Decimal | None = None
    available_notional_budget_usd: Decimal | None = None
    available_order_slots: int | None = None
    broker_position_id: str | None = None
    units_to_deduct: Decimal | None = None
    reduce_position_hash: str = ""
    reduce_position_quantity: Decimal | None = None
    reduce_exit_reason: str = ""
    reduce_broker_snapshot_hash: str = ""
    reduce_provenance_hash: str = ""
    reconciliation_only: bool = False
    proposal_source: str = ""
    risk_config_hash: str = ""
    risk_payload_hash: str = ""
    risk_seal: str = ""
    account_mode: str = "DEMO"
    signature_algorithm: str = "Ed25519"

    def __post_init__(self) -> None:
        object.__setattr__(self, "created_at", utc(self.created_at))
        object.__setattr__(self, "expires_at", utc(self.expires_at))
        object.__setattr__(self, "symbol", self.symbol.strip().upper())
        if not all(
            value.strip()
            for value in (
                self.order_command_id,
                self.intent_id,
                self.proposal_id,
                self.client_order_id,
                self.portfolio_id,
                self.idempotency_key,
                self.correlation_id,
                self.proposal_source,
                self.risk_config_hash,
            )
        ):
            raise ValueError("order command identity is incomplete")
        if self.account_mode != "DEMO":
            raise ValueError("v2 order commands are DEMO-only")
        if not self.symbol:
            raise ValueError("order command symbol is required")
        if self.expires_at < self.created_at:
            raise ValueError("order command expiry precedes creation")
        numeric = tuple(
            value
            for value in (
                self.amount_usd,
                self.quantity,
                self.units_to_deduct,
                self.reduce_position_quantity,
                self.reference_entry,
                self.min_acceptable_entry,
                self.max_acceptable_entry,
                self.stop_loss_fraction,
                self.take_profit_fraction,
                self.max_slippage_bps,
                self.max_loss_usd,
                self.available_loss_budget_usd,
                self.available_notional_budget_usd,
            )
            if value is not None
        )
        if not all(value.is_finite() for value in numeric):
            raise ValueError("order command economics must be finite")
        if self.amount_usd < ZERO:
            raise ValueError("order amount cannot be negative")
        if not self.reduce_only and self.amount_usd <= ZERO:
            raise ValueError("open order requires positive amount")
        if self.quantity is not None and self.quantity <= ZERO:
            raise ValueError("quantity must be positive")
        if self.units_to_deduct is not None and self.units_to_deduct <= ZERO:
            raise ValueError("partial close units must be positive")
        if self.reduce_only:
            if self.reconciliation_only:
                if self.proposal_source != "broker_reconciliation_close":
                    raise ValueError("reconciliation command source is invalid")
                if self.risk_payload_hash or self.risk_seal:
                    raise ValueError("reconciliation-only command cannot carry an execution seal")
            if self.quantity is None or self.reduce_position_quantity is None:
                raise ValueError("reduce order lacks signed quantity provenance")
            if self.quantity > self.reduce_position_quantity:
                raise ValueError("reduce quantity exceeds signed position quantity")
            if self.units_to_deduct is None:
                if self.quantity != self.reduce_position_quantity:
                    raise ValueError("full close must cover the signed position quantity")
            elif self.units_to_deduct != self.quantity:
                raise ValueError("partial close units differ from signed reduce quantity")
            if not str(self.broker_position_id or "").strip():
                raise ValueError("reduce order lacks broker position identity")
            try:
                ExitReason(self.reduce_exit_reason)
            except ValueError as exc:
                raise ValueError("reduce order exit reason is invalid") from exc
            for name, value in (
                ("reduce_position_hash", self.reduce_position_hash),
                ("reduce_provenance_hash", self.reduce_provenance_hash),
            ):
                if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
                    raise ValueError(f"{name} is invalid")
            if not self.reduce_broker_snapshot_hash.strip():
                raise ValueError("reduce broker snapshot provenance is missing")
            expected_reduce_hash = reduce_command_provenance_hash(
                position_hash=self.reduce_position_hash,
                broker_position_id=str(self.broker_position_id),
                quantity_before=self.reduce_position_quantity,
                units=self.quantity,
                exit_reason=self.reduce_exit_reason,
                broker_snapshot_hash=self.reduce_broker_snapshot_hash,
                risk_config_hash=self.risk_config_hash,
            )
            if expected_reduce_hash != self.reduce_provenance_hash:
                raise ValueError("reduce provenance hash is invalid")
            return
        signed_risk_values = (
            self.reference_entry,
            self.min_acceptable_entry,
            self.max_acceptable_entry,
            self.stop_loss_fraction,
            self.take_profit_fraction,
            self.max_slippage_bps,
            self.max_loss_usd,
            self.available_loss_budget_usd,
            self.available_notional_budget_usd,
        )
        if any(value is None for value in signed_risk_values):
            raise ValueError("open order lacks signed execution-risk bounds")
        if len(self.intent_hash) != 64 or any(
            ch not in "0123456789abcdef" for ch in self.intent_hash
        ):
            raise ValueError("open order intent hash is invalid")
        reference = cast(Decimal, self.reference_entry)
        minimum = cast(Decimal, self.min_acceptable_entry)
        maximum = cast(Decimal, self.max_acceptable_entry)
        stop_fraction = cast(Decimal, self.stop_loss_fraction)
        take_fraction = cast(Decimal, self.take_profit_fraction)
        slippage = cast(Decimal, self.max_slippage_bps)
        max_loss = cast(Decimal, self.max_loss_usd)
        loss_budget = cast(Decimal, self.available_loss_budget_usd)
        notional_budget = cast(Decimal, self.available_notional_budget_usd)
        if not ZERO < minimum <= reference <= maximum:
            raise ValueError("signed execution band is invalid")
        if stop_fraction <= ZERO or take_fraction <= ZERO:
            raise ValueError("signed stop/take fractions must be positive")
        if slippage < ZERO:
            raise ValueError("signed slippage cannot be negative")
        if not ZERO < max_loss <= loss_budget:
            raise ValueError("signed loss budgets are invalid")
        if self.amount_usd > notional_budget:
            raise ValueError("signed notional budget is insufficient")
        if self.available_order_slots is None or self.available_order_slots < 1:
            raise ValueError("signed order-slot budget is invalid")


@dataclass(frozen=True)
class BrokerOrder:
    order_command_id: str
    client_order_id: str
    status: OrderStatus
    submitted_at: datetime | None = None
    acknowledged_at: datetime | None = None
    broker_order_id: str | None = None
    broker_position_id: str | None = None
    filled_quantity: Decimal = ZERO
    average_fill_price: Decimal | None = None
    last_update_at: datetime | None = None
    failure_reason: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("submitted_at", "acknowledged_at", "last_update_at"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, utc(value))
        if not self.order_command_id.strip() or not self.client_order_id.strip():
            raise ValueError("broker order identity is incomplete")
        economics = tuple(
            value for value in (self.filled_quantity, self.average_fill_price) if value is not None
        )
        if not all(value.is_finite() for value in economics):
            raise ValueError("broker order economics must be finite")
        if self.filled_quantity < ZERO:
            raise ValueError("filled quantity cannot be negative")
        if self.average_fill_price is not None and self.average_fill_price <= ZERO:
            raise ValueError("average fill price must be positive")
        if (self.filled_quantity > ZERO) != (self.average_fill_price is not None):
            raise ValueError(
                "broker order fill quantity and average price must be present together"
            )
        if (
            self.submitted_at is not None
            and self.last_update_at is not None
            and self.last_update_at < self.submitted_at
        ):
            raise ValueError("broker order update precedes submission")
        if self.acknowledged_at is not None:
            if self.submitted_at is None or self.acknowledged_at < self.submitted_at:
                raise ValueError("broker acknowledgement precedes or lacks submission")
            if self.last_update_at is not None and self.last_update_at < self.acknowledged_at:
                raise ValueError("broker order update precedes acknowledgement")
        if self.status is OrderStatus.ACKNOWLEDGED and not str(self.broker_order_id or "").strip():
            raise ValueError("acknowledged broker order lacks broker identity")


@dataclass(frozen=True)
class Fill:
    fill_id: str
    order_command_id: str
    client_order_id: str
    broker_order_id: str | None
    broker_position_id: str | None
    symbol: str
    side: Side
    quantity: Decimal
    price: Decimal
    fee_usd: Decimal
    financing_usd: Decimal
    event_time: datetime
    processing_time: datetime
    idempotency_key: str
    broker_reported_net_pnl_usd: Decimal | None = None
    broker_reported_fees_usd: Decimal | None = None
    broker_costs_source: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_time", utc(self.event_time))
        object.__setattr__(self, "processing_time", utc(self.processing_time))
        object.__setattr__(self, "symbol", self.symbol.strip().upper())
        numeric = (self.quantity, self.price, self.fee_usd, self.financing_usd)
        if not all(value.is_finite() for value in numeric):
            raise ValueError("fill economics must be finite")
        if self.quantity <= ZERO or self.price <= ZERO:
            raise ValueError("fill quantity/price must be positive")
        if min(self.fee_usd, self.financing_usd) < ZERO:
            raise ValueError("fill costs cannot be negative")
        for value in (self.broker_reported_net_pnl_usd, self.broker_reported_fees_usd):
            if value is not None and not value.is_finite():
                raise ValueError("broker-reported fill economics must be finite")
        if not all(
            str(value).strip()
            for value in (
                self.fill_id,
                self.order_command_id,
                self.client_order_id,
                self.symbol,
                self.idempotency_key,
            )
        ):
            raise ValueError("fill identity is required")
        if self.processing_time < self.event_time:
            raise ValueError("fill processing time precedes event time")


@dataclass(frozen=True)
class ReconciliationCase:
    case_id: str
    order_command_id: str
    status: str
    opened_at: datetime
    updated_at: datetime
    attempts: int
    broker_snapshot_hash: str
    detail: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "opened_at", utc(self.opened_at))
        object.__setattr__(self, "updated_at", utc(self.updated_at))
        if not self.case_id.strip() or not self.order_command_id.strip():
            raise ValueError("reconciliation identity is incomplete")
        if self.status not in {
            "OPEN",
            "RESOLVED_FILLED",
            "RESOLVED_ABSENT",
            "MANUAL_REVIEW",
        }:
            raise ValueError("reconciliation status is invalid")
        if self.attempts < 0:
            raise ValueError("reconciliation attempts cannot be negative")
        if self.updated_at < self.opened_at:
            raise ValueError("reconciliation update precedes case opening")
        if len(self.broker_snapshot_hash) != 64 or any(
            character not in "0123456789abcdef" for character in self.broker_snapshot_hash
        ):
            raise ValueError("reconciliation broker snapshot hash is invalid")


@dataclass(frozen=True)
class DomainEvent:
    event_id: str
    event_type: str
    schema_version: int
    event_time: datetime
    processing_time: datetime
    idempotency_key: str
    causation_id: str
    correlation_id: str
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_time", utc(self.event_time))
        object.__setattr__(self, "processing_time", utc(self.processing_time))
        if self.schema_version < 1:
            raise ValueError("event schema version must be positive")
        if self.processing_time < self.event_time:
            raise ValueError("event processing time precedes event time")
        if not all(
            str(value).strip()
            for value in (
                self.event_id,
                self.event_type,
                self.idempotency_key,
                self.correlation_id,
            )
        ):
            raise ValueError("event identity is incomplete")

    @property
    def canonical_payload_hash(self) -> str:
        return canonical_hash(dict(self.payload))
