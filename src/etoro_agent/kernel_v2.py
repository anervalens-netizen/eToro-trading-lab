from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from decimal import Decimal

from .domain_v2 import (
    BPS,
    BrokerOrder,
    DomainEvent,
    ExitReason,
    Fill,
    IntentEnvelope,
    OrderCommand,
    OrderStatus,
    PositionState,
    PositionStatus,
    QuoteProvenance,
    ReconciliationCase,
    Side,
    canonical_hash,
    utc,
)
from .exits_v2 import ExitContext, ExitDecision, ExitEvaluator
from .oms_v2 import OrderManagementSystem
from .risk_seal_v2 import (
    SOL_MASTER_CLOSE,
    SOL_MASTER_OPEN,
    RiskCommandSealerV2,
    RiskCommandSignerV2,
    RiskCommandVerifierV2,
    risk_mandate_hash,
)
from .risk_v2 import BrokerTruth, GlobalRiskKernel, RiskDecision
from .runtime_store_v2 import RuntimeStoreV2

ZERO = Decimal("0")


def _stable_id(prefix: str, seed: str) -> str:
    return f"{prefix}-{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:24]}"


def _stable_uuid(seed: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"etoro-trading-lab:{seed}"))


def _event(
    event_type: str,
    *,
    idempotency_key: str,
    event_time: datetime,
    processing_time: datetime,
    correlation_id: str,
    causation_id: str = "",
    payload: Mapping[str, object] | None = None,
) -> DomainEvent:
    return DomainEvent(
        event_id=_stable_id("evt", f"{event_type}:{idempotency_key}"),
        event_type=event_type,
        schema_version=2,
        event_time=event_time,
        processing_time=processing_time,
        idempotency_key=idempotency_key,
        causation_id=causation_id,
        correlation_id=correlation_id,
        payload=dict(payload or {}),
    )


class UnifiedTradingKernel:
    """One deterministic economic core for replay, shadow and broker adapters."""

    version = "kernel-v2.0"

    def __init__(
        self,
        store: RuntimeStoreV2,
        risk: GlobalRiskKernel,
        *,
        exit_evaluator: ExitEvaluator | None = None,
        oms: OrderManagementSystem | None = None,
        command_signer: RiskCommandSealerV2 | None = None,
    ) -> None:
        self.store = store
        self.risk = risk
        self.exit_evaluator = exit_evaluator or ExitEvaluator()
        self.oms = oms or OrderManagementSystem()
        self.risk_config_hash = risk_mandate_hash(risk.mandate)
        self.command_signer = command_signer or RiskCommandSignerV2.generate()

    def command_verifier(self) -> RiskCommandVerifierV2:
        return self.command_signer.verifier(expected_risk_config_hash=self.risk_config_hash)

    def _reconciliation_case(
        self,
        order_command_id: str,
        *,
        status: str,
        at: datetime,
        broker_snapshot_hash: str,
        detail: str,
    ) -> ReconciliationCase:
        current = utc(at)
        existing = self.store.reconciliation_case(order_command_id)
        return ReconciliationCase(
            case_id=_stable_id("recon", order_command_id),
            order_command_id=order_command_id,
            status=status,
            opened_at=current if existing is None else existing.opened_at,
            updated_at=current,
            attempts=1 if existing is None else existing.attempts + 1,
            broker_snapshot_hash=broker_snapshot_hash,
            detail=detail[:500],
        )

    def submit_open_intent(
        self,
        intent: IntentEnvelope,
        quote: QuoteProvenance,
        broker: BrokerTruth,
        *,
        now: datetime,
    ) -> tuple[RiskDecision, OrderCommand | None]:
        current = utc(now)
        self.store.save_intent(intent)
        command_idempotency_key = f"open:{intent.intent_id}"
        existing_command = self.store.order_command_for_idempotency(command_idempotency_key)
        if existing_command is not None:
            return RiskDecision(True, ()), existing_command
        decision = self.risk.evaluate_open(intent, quote, broker, current)
        correlation_id = intent.correlation_id or intent.intent_id
        if not decision.approved:
            self.store.append_event(
                _event(
                    "RiskRejected",
                    idempotency_key=f"risk-reject:{intent.intent_id}:{canonical_hash(decision.reasons)}",
                    event_time=current,
                    processing_time=current,
                    correlation_id=correlation_id,
                    causation_id=intent.intent_id,
                    payload={"intent_id": intent.intent_id, "reasons": decision.reasons},
                )
            )
            return decision, None

        order_command_id = _stable_id("cmd", intent.intent_id)
        proposal_id = _stable_id("proposal", intent.intent_id)
        client_order_id = _stable_uuid(f"open:{intent.intent_id}")
        reference_entry = quote.ask if intent.side is Side.BUY else quote.bid
        execution_band_bps = min(
            intent.max_price_drift_bps,
            self.risk.mandate.max_mid_drift_bps,
        )
        band_fraction = execution_band_bps / BPS
        available_loss_budget = self.risk.available_loss_budget(broker)
        available_notional_budget = self.risk.available_notional_budget(broker)
        available_order_slots = self.risk.available_order_slots(broker)
        command = self.command_signer.seal(
            OrderCommand(
                order_command_id=order_command_id,
                intent_id=intent.intent_id,
                proposal_id=proposal_id,
                client_order_id=client_order_id,
                portfolio_id=intent.portfolio_id,
                symbol=intent.symbol,
                side=intent.side,
                amount_usd=intent.amount_usd,
                quantity=None,
                reduce_only=False,
                created_at=current,
                expires_at=min(intent.expires_at, current + timedelta(seconds=60)),
                idempotency_key=command_idempotency_key,
                correlation_id=correlation_id,
                intent_hash=canonical_hash(asdict(intent)),
                reference_entry=reference_entry,
                min_acceptable_entry=reference_entry * (Decimal("1") - band_fraction),
                max_acceptable_entry=reference_entry * (Decimal("1") + band_fraction),
                stop_loss_fraction=intent.stop_loss_fraction,
                take_profit_fraction=intent.take_profit_fraction,
                max_slippage_bps=intent.max_slippage_bps,
                max_loss_usd=min(
                    self.risk.mandate.max_trade_risk_usd,
                    available_loss_budget,
                ),
                available_loss_budget_usd=available_loss_budget,
                available_notional_budget_usd=available_notional_budget,
                available_order_slots=available_order_slots,
                proposal_source=SOL_MASTER_OPEN,
                risk_config_hash=self.risk_config_hash,
            )
        )
        order = self.oms.risk_approve(self.oms.create(command), current)
        event = _event(
            "RiskApproved",
            idempotency_key=f"risk-approved:{intent.intent_id}",
            event_time=current,
            processing_time=current,
            correlation_id=correlation_id,
            causation_id=intent.intent_id,
            payload={
                "intent_id": intent.intent_id,
                "order_command_id": command.order_command_id,
                "broker_snapshot_hash": broker.snapshot_hash,
                "quote": asdict(quote),
                "risk_version": self.risk.version,
                "proposal_source": command.proposal_source,
                "risk_payload_hash": command.risk_payload_hash,
                "risk_reservation": {
                    "max_loss_usd": str(command.max_loss_usd),
                    "notional_usd": str(command.amount_usd),
                    "available_loss_budget_usd": str(command.available_loss_budget_usd),
                    "available_notional_budget_usd": str(command.available_notional_budget_usd),
                    "available_order_slots": command.available_order_slots,
                },
            },
        )
        inserted = self.store.save_order_bundle(
            command,
            order,
            event,
            outbox_topic="broker.submit",
            outbox_payload={"order_command_id": command.order_command_id},
        )
        if not inserted:
            existing_command = self.store.order_command_for_idempotency(command_idempotency_key)
            if existing_command is None:
                raise RuntimeError("idempotent open command is unavailable")
            return decision, existing_command
        return decision, command

    def create_close_command(
        self,
        position: PositionState,
        *,
        now: datetime,
        reason: ExitReason,
        broker: BrokerTruth,
        units_to_deduct: Decimal | None = None,
    ) -> OrderCommand:
        current = utc(now)
        risk_decision = self.risk.evaluate_reduce(broker)
        if not risk_decision.approved:
            self.store.append_event(
                _event(
                    "ReduceRiskRejected",
                    idempotency_key=(
                        f"reduce-risk-reject:{position.position_id}:"
                        f"{canonical_hash(risk_decision.reasons)}"
                    ),
                    event_time=current,
                    processing_time=current,
                    correlation_id=position.position_id,
                    causation_id=position.intent_id,
                    payload={
                        "position_id": position.position_id,
                        "reasons": risk_decision.reasons,
                        "broker_snapshot_hash": broker.snapshot_hash,
                    },
                )
            )
            raise PermissionError("deterministic reduce risk rejected")
        if position.status is not PositionStatus.OPEN:
            raise ValueError("only an open position can be reduced")
        units = units_to_deduct or position.quantity
        if units <= ZERO or units > position.quantity:
            raise ValueError("close units exceed open position")
        seed = (
            f"close:{position.position_id}:{reason.value}:{units}:"
            f"{position.quantity}:{position.realized_pnl}:"
            f"{position.broker_position_id or ''}"
        )
        idempotency_key = _stable_id("reduce", seed)
        existing_command = self.store.order_command_for_idempotency(idempotency_key)
        if existing_command is not None:
            return existing_command
        active_states = (
            OrderStatus.RISK_APPROVED.value,
            OrderStatus.SUBMITTING.value,
            OrderStatus.ACKNOWLEDGED.value,
            OrderStatus.PARTIALLY_FILLED.value,
            OrderStatus.UNKNOWN.value,
        )
        for active_order in self.store.broker_orders_by_status(active_states):
            active_command = self.store.order_command(active_order.order_command_id)
            if active_command.reduce_only and active_command.correlation_id == position.position_id:
                raise ValueError("position already has an active reduce-only command")
        command = self.command_signer.seal(
            OrderCommand(
                order_command_id=_stable_id("cmd", seed),
                intent_id=position.intent_id,
                proposal_id=_stable_id("proposal", seed),
                client_order_id=_stable_uuid(seed),
                portfolio_id=position.portfolio_id,
                symbol=position.symbol,
                side=Side.SELL if position.side is Side.BUY else Side.BUY,
                amount_usd=ZERO,
                quantity=units,
                reduce_only=True,
                created_at=current,
                expires_at=current + timedelta(seconds=60),
                idempotency_key=idempotency_key,
                correlation_id=position.position_id,
                broker_position_id=position.broker_position_id,
                units_to_deduct=units,
                proposal_source=SOL_MASTER_CLOSE,
                risk_config_hash=self.risk_config_hash,
            )
        )
        broker_order = self.oms.risk_approve(self.oms.create(command), current)
        inserted = self.store.save_order_bundle(
            command,
            broker_order,
            _event(
                "RiskApproved",
                idempotency_key=f"risk-approved:{command.order_command_id}",
                event_time=current,
                processing_time=current,
                correlation_id=position.position_id,
                causation_id=position.position_id,
                payload={
                    "order_command_id": command.order_command_id,
                    "reduce_only": True,
                    "exit_reason": reason.value,
                    "units": str(units),
                    "proposal_source": command.proposal_source,
                    "risk_payload_hash": command.risk_payload_hash,
                },
            ),
            outbox_topic="broker.submit",
            outbox_payload={"order_command_id": command.order_command_id},
        )
        if not inserted:
            existing_command = self.store.order_command_for_idempotency(idempotency_key)
            if existing_command is None:
                raise RuntimeError("idempotent close command is unavailable")
            return existing_command
        return command

    def begin_submit(
        self,
        order_command_id: str,
        at: datetime,
        *,
        preflight_evidence: Mapping[str, object] | None = None,
    ) -> BrokerOrder:
        order = self.store.broker_order(order_command_id)
        updated = self.oms.begin_submit(order, at)
        self.store.save_broker_order(
            updated,
            _event(
                "OrderSubmitted",
                idempotency_key=f"submitted:{order_command_id}",
                event_time=at,
                processing_time=at,
                correlation_id=order_command_id,
                causation_id=order_command_id,
                payload={
                    "order_command_id": order_command_id,
                    "preflight": dict(preflight_evidence or {}),
                },
            ),
        )
        return updated

    def acknowledge(
        self,
        order_command_id: str,
        *,
        at: datetime,
        broker_order_id: str,
        broker_position_id: str | None = None,
    ) -> BrokerOrder:
        order = self.store.broker_order(order_command_id)
        updated = self.oms.acknowledge(
            order,
            at,
            broker_order_id=broker_order_id,
            broker_position_id=broker_position_id,
        )
        self.store.save_broker_order(
            updated,
            _event(
                "OrderAccepted",
                idempotency_key=f"ack:{order_command_id}:{broker_order_id}",
                event_time=at,
                processing_time=at,
                correlation_id=order_command_id,
                causation_id=order_command_id,
                payload={
                    "order_command_id": order_command_id,
                    "broker_order_id": broker_order_id,
                    "broker_position_id": broker_position_id,
                },
            ),
        )
        return updated

    def mark_unknown(self, order_command_id: str, *, at: datetime, reason: str) -> BrokerOrder:
        order = self.store.broker_order(order_command_id)
        updated = self.oms.mark_unknown(order, at, reason)
        self.store.save_broker_order(
            updated,
            _event(
                "OrderUnknown",
                idempotency_key=f"unknown:{order_command_id}:{canonical_hash(reason)}",
                event_time=at,
                processing_time=at,
                correlation_id=order_command_id,
                causation_id=order_command_id,
                payload={"order_command_id": order_command_id, "reason": reason[:500]},
            ),
        )
        return updated

    def reject_before_send(
        self, order_command_id: str, *, at: datetime, reason: str
    ) -> BrokerOrder:
        order = self.store.broker_order(order_command_id)
        updated = self.oms.reject(order, at, reason)
        self.store.save_broker_order(
            updated,
            _event(
                "OrderRejectedBeforeSend",
                idempotency_key=(f"preflight-rejected:{order_command_id}:{canonical_hash(reason)}"),
                event_time=at,
                processing_time=at,
                correlation_id=order_command_id,
                causation_id=order_command_id,
                payload={
                    "order_command_id": order_command_id,
                    "reason": reason[:500],
                    "network_write_attempted": False,
                },
            ),
        )
        return updated

    def _open_position_for_command(self, command: OrderCommand) -> PositionState | None:
        for position in self.store.positions(command.portfolio_id, open_only=True):
            if position.intent_id == command.intent_id and position.symbol == command.symbol:
                return position
        return None

    def _position_by_broker_id(self, command: OrderCommand) -> PositionState:
        candidates = [
            position
            for position in self.store.positions(command.portfolio_id, open_only=True)
            if position.symbol == command.symbol
            and (
                command.broker_position_id is None
                or position.broker_position_id == command.broker_position_id
            )
        ]
        if len(candidates) != 1:
            raise ValueError("reduce-only fill requires exactly one reconciled position")
        return candidates[0]

    def apply_fill(
        self,
        fill: Fill,
        *,
        final: bool,
        exit_reason: ExitReason | None = None,
        broker_snapshot_hash: str | None = None,
    ) -> PositionState:
        command = self.store.order_command(fill.order_command_id)
        if fill.symbol != command.symbol or fill.side is not command.side:
            raise ValueError("fill economic identity does not match order command")
        existing_fill = self.store.fill_by_idempotency(fill.idempotency_key)
        if existing_fill is not None:
            if existing_fill != fill:
                raise ValueError("fill idempotency key cannot be rebound")
            position = (
                self._position_by_broker_id(command)
                if command.reduce_only
                else self._open_position_for_command(command)
            )
            if position is None:
                # A fully closed reduce-only duplicate should resolve to the historical position.
                candidates = [
                    item
                    for item in self.store.positions(command.portfolio_id)
                    if item.symbol == command.symbol and item.intent_id == command.intent_id
                ]
                if len(candidates) != 1:
                    raise RuntimeError(
                        "duplicate fill exists but position projection is unavailable"
                    )
                position = candidates[0]
            return position

        order = self.store.broker_order(fill.order_command_id)
        if not command.reduce_only:
            intent = self.store.intent(command.intent_id)
            previous_notional = order.filled_quantity * (order.average_fill_price or ZERO)
            maximum_notional = command.amount_usd * (Decimal("1") + intent.max_slippage_bps / BPS)
            if previous_notional + fill.quantity * fill.price > maximum_notional:
                raise ValueError("open fill notional exceeds sealed amount boundary")
        updated_order = self.oms.apply_fill(
            order,
            fill,
            expected_quantity=command.quantity,
            expected_symbol=command.symbol,
            expected_side=command.side,
            is_final=final,
        )
        fill_event = _event(
            "OrderFilled" if final else "OrderPartiallyFilled",
            idempotency_key=f"fill:{fill.idempotency_key}",
            event_time=fill.event_time,
            processing_time=fill.processing_time,
            correlation_id=command.correlation_id,
            causation_id=command.order_command_id,
            payload={
                "fill_id": fill.fill_id,
                "order_command_id": command.order_command_id,
                "quantity": str(fill.quantity),
                "price": str(fill.price),
                "final": final,
            },
        )

        realized_delta = ZERO
        if command.reduce_only:
            position = self._position_by_broker_id(command)
            if fill.quantity > position.quantity:
                raise ValueError("reduce-only fill exceeds open position")
            ratio = fill.quantity / position.quantity
            allocated_fees = position.fees_accrued * ratio
            allocated_financing = position.financing_accrued * ratio
            realized = (
                fill.quantity * position.side.direction * (fill.price - position.entry_price)
                - allocated_fees
                - allocated_financing
                - fill.fee_usd
                - fill.financing_usd
            )
            realized_delta = realized
            remaining = position.quantity - fill.quantity
            closed = remaining == ZERO
            remaining_fees = position.fees_accrued - allocated_fees
            remaining_financing = position.financing_accrued - allocated_financing
            remaining_unrealized = (
                ZERO
                if closed
                else remaining * position.side.direction * (fill.price - position.entry_price)
                - remaining_fees
                - remaining_financing
            )
            new_position = replace(
                position,
                quantity=remaining,
                fees_accrued=remaining_fees,
                financing_accrued=remaining_financing,
                realized_pnl=position.realized_pnl + realized,
                unrealized_pnl=remaining_unrealized,
                status=PositionStatus.CLOSED if closed else PositionStatus.OPEN,
                exit_reason=exit_reason if closed else None,
                last_mark=fill.price,
            )
            event_type = "PositionClosed" if closed else "PositionReduced"
        else:
            position = self._open_position_for_command(command)
            intent = self.store.intent(command.intent_id)
            if position is None:
                stop_fraction = intent.stop_loss_fraction
                take_fraction = intent.take_profit_fraction
                side = intent.side
                entry = fill.price
                stop_price = entry * (
                    Decimal("1") - stop_fraction
                    if side is Side.BUY
                    else Decimal("1") + stop_fraction
                )
                take_price = entry * (
                    Decimal("1") + take_fraction
                    if side is Side.BUY
                    else Decimal("1") - take_fraction
                )
                new_position = PositionState(
                    position_id=_stable_id("pos", command.intent_id),
                    portfolio_id=command.portfolio_id,
                    strategy_id=intent.strategy_id,
                    lane_id=intent.lane_id,
                    strategy_version=intent.strategy_version,
                    intent_id=command.intent_id,
                    symbol=command.symbol,
                    side=side,
                    quantity=fill.quantity,
                    entry_price=fill.price,
                    entry_event_time=fill.event_time,
                    entry_processing_time=fill.processing_time,
                    stop_price=stop_price,
                    take_profit_price=take_price,
                    stop_fraction=stop_fraction,
                    take_profit_fraction=take_fraction,
                    max_holding_seconds=intent.max_holding_seconds,
                    expires_at=fill.event_time + timedelta(seconds=intent.max_holding_seconds),
                    fees_accrued=fill.fee_usd,
                    financing_accrued=fill.financing_usd,
                    broker_position_id=fill.broker_position_id,
                    last_mark=fill.price,
                )
                event_type = "PositionOpened"
            else:
                total_qty = position.quantity + fill.quantity
                average = (
                    position.quantity * position.entry_price + fill.quantity * fill.price
                ) / total_qty
                stop = average * (
                    Decimal("1") - position.stop_fraction
                    if position.side is Side.BUY
                    else Decimal("1") + position.stop_fraction
                )
                take = average * (
                    Decimal("1") + position.take_profit_fraction
                    if position.side is Side.BUY
                    else Decimal("1") - position.take_profit_fraction
                )
                new_position = replace(
                    position,
                    quantity=total_qty,
                    entry_price=average,
                    stop_price=stop,
                    take_profit_price=take,
                    fees_accrued=position.fees_accrued + fill.fee_usd,
                    financing_accrued=position.financing_accrued + fill.financing_usd,
                    broker_position_id=fill.broker_position_id or position.broker_position_id,
                    last_mark=fill.price,
                )
                event_type = "PositionIncreased"

        position_event = _event(
            event_type,
            idempotency_key=f"position:{fill.idempotency_key}",
            event_time=fill.event_time,
            processing_time=fill.processing_time,
            correlation_id=command.correlation_id,
            causation_id=fill.fill_id,
            payload={
                "position": asdict(new_position),
                "realized_delta_usd": str(realized_delta),
            },
        )
        reconciliation_case = None
        reconciliation_event = None
        if order.status is OrderStatus.UNKNOWN:
            if broker_snapshot_hash is None or len(broker_snapshot_hash) != 64:
                raise ValueError("UNKNOWN fill requires exact broker snapshot provenance")
            reconciliation_case = self._reconciliation_case(
                command.order_command_id,
                status="RESOLVED_FILLED",
                at=fill.processing_time,
                broker_snapshot_hash=broker_snapshot_hash,
                detail="exact broker fill projected from reconciled position identity",
            )
            reconciliation_event = _event(
                "ReconciliationSucceeded",
                idempotency_key=f"reconciliation-fill:{fill.idempotency_key}",
                event_time=fill.event_time,
                processing_time=fill.processing_time,
                correlation_id=command.correlation_id,
                causation_id=fill.fill_id,
                payload={
                    "order_command_id": command.order_command_id,
                    "status": updated_order.status.value,
                    "broker_snapshot_hash": broker_snapshot_hash,
                },
            )
        inserted = self.store.save_fill_position_bundle(
            fill,
            updated_order,
            new_position,
            fill_event,
            position_event,
            reconciliation_case,
            reconciliation_event,
        )
        if not inserted:
            existing_fill = self.store.fill_by_idempotency(fill.idempotency_key)
            if existing_fill != fill:
                raise ValueError("fill idempotency key cannot be rebound")
            return self.apply_fill(
                fill,
                final=final,
                exit_reason=exit_reason,
                broker_snapshot_hash=broker_snapshot_hash,
            )
        return new_position

    def reconcile_unknown(
        self,
        order_command_id: str,
        *,
        at: datetime,
        found: bool | None,
        broker_snapshot_hash: str,
        broker_order_id: str | None = None,
        broker_position_id: str | None = None,
        detail: str = "",
    ) -> BrokerOrder:
        if found is True:
            raise ValueError("filled reconciliation requires an exact Fill projection")
        order = self.store.broker_order(order_command_id)
        updated = self.oms.reconcile_unknown(
            order,
            at,
            found=found,
            broker_order_id=broker_order_id,
            broker_position_id=broker_position_id,
        )
        case_status = "RESOLVED_ABSENT" if found is False else "MANUAL_REVIEW"
        case = self._reconciliation_case(
            order_command_id,
            status=case_status,
            at=at,
            broker_snapshot_hash=broker_snapshot_hash,
            detail=detail,
        )
        self.store.save_reconciliation_bundle(
            updated,
            case,
            _event(
                "ReconciliationSucceeded" if found is False else "ReconciliationFailed",
                idempotency_key=(
                    f"reconciled:{order_command_id}:{updated.status.value}:{broker_snapshot_hash}"
                ),
                event_time=at,
                processing_time=at,
                correlation_id=order_command_id,
                causation_id=order_command_id,
                payload={
                    "order_command_id": order_command_id,
                    "status": updated.status.value,
                    "broker_snapshot_hash": broker_snapshot_hash,
                    "detail": detail[:500],
                },
            ),
        )
        return updated

    def apply_financing(
        self,
        position: PositionState,
        amount_usd: Decimal,
        *,
        at: datetime,
        reference: str,
    ) -> PositionState:
        if amount_usd < ZERO:
            raise ValueError("financing amount cannot be negative")
        if position.status is not PositionStatus.OPEN:
            raise ValueError("financing applies only to open positions")
        current = utc(at)
        updated = replace(position, financing_accrued=position.financing_accrued + amount_usd)
        event = _event(
            "FinancingApplied",
            idempotency_key=f"financing:{position.position_id}:{reference}",
            event_time=current,
            processing_time=current,
            correlation_id=position.position_id,
            causation_id=position.position_id,
            payload={
                "position_id": position.position_id,
                "amount_usd": str(amount_usd),
                "reference": reference,
            },
        )
        self.store.save_position(updated, event)
        return updated

    def evaluate_exit(self, position: PositionState, context: ExitContext) -> ExitDecision:
        return self.exit_evaluator.evaluate(position, context)
