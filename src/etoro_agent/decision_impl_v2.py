from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from .ai_v2 import (
    AIAction,
    AIIntentOutputV2,
    DecisionPacketV2,
    Lane,
    sanitize_packet_payload,
)
from .domain_v2 import (
    ExitReason,
    IntentEnvelope,
    PositionState,
    QuoteProvenance,
    Side,
    canonical_hash,
)
from .features_v2 import FeatureSnapshotV2
from .kernel_v2 import UnifiedTradingKernel
from .risk_v2 import BrokerTruth
from .roles_v2 import critic_gate_rejection_reason
from .strategy_v2 import FamilySignal


@dataclass(frozen=True)
class DecisionPacketContextV2:
    broker_snapshot_hash: str
    risk_config_hash: str
    portfolio: Mapping[str, Any]
    market_events: tuple[Mapping[str, Any], ...] = ()
    regime: Mapping[str, Any] | None = None
    critic: Mapping[str, Any] | None = None


class DecisionPacketBuilderV2:
    version = "decision-packet-v2.1"

    @staticmethod
    def signal_key(signal: FamilySignal) -> str:
        return ":".join(
            (signal.family.value, signal.strategy_version, signal.symbol, signal.side.value)
        )

    @staticmethod
    def _candidate(
        signal: FamilySignal,
        index: int,
        execution_plan: Mapping[str, Any] | None,
    ) -> Mapping[str, Any]:
        payload = {
            "family": signal.family.value,
            "strategy_id": signal.family.value,
            "strategy_version": signal.strategy_version,
            "symbol": signal.symbol,
            "side": signal.side.value,
            "raw_confidence": str(signal.raw_confidence),
            "threshold": str(signal.confidence_threshold),
            "actionable": signal.actionable,
            "stop_loss_fraction": str(signal.stop_fraction),
            "take_profit_fraction": str(signal.take_fraction),
            "max_holding_seconds": signal.max_holding_seconds,
            "rationale": signal.rationale,
            "evidence_refs": list(signal.evidence_refs),
            "executable": execution_plan is not None,
            "execution_plan": None if execution_plan is None else dict(execution_plan),
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
        ).hexdigest()
        payload["candidate_id"] = f"candidate-{index:02d}-{digest[:16]}"
        return sanitize_packet_payload(payload)

    def build(
        self,
        *,
        lane: Lane,
        mode: str,
        feature: FeatureSnapshotV2,
        market_snapshot_ids: Sequence[str],
        signals: Sequence[FamilySignal],
        context: DecisionPacketContextV2,
        position: PositionState | None,
        created_at: datetime,
        ttl_seconds: int = 300,
        execution_plans: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> DecisionPacketV2:
        if ttl_seconds < 30 or ttl_seconds > 1800:
            raise ValueError("decision packet TTL must be 30..1800 seconds")
        now = created_at.astimezone(UTC)
        candidates = tuple(
            self._candidate(
                signal,
                index,
                None if execution_plans is None else execution_plans.get(self.signal_key(signal)),
            )
            for index, signal in enumerate(signals, start=1)
            if signal.actionable
        )
        evidence = set(feature.source_snapshot_ids)
        for signal in signals:
            evidence.update(signal.evidence_refs)
        for event in context.market_events:
            event_id = event.get("event_id") or event.get("event_hash")
            if event_id:
                evidence.add(str(event_id))
        model_context = sanitize_packet_payload(
            {
                "packet_version": self.version,
                "feature": {
                    "snapshot_id": feature.snapshot_id,
                    "symbol": feature.symbol,
                    "event_time": feature.event_time.isoformat(),
                    "feature_version": feature.feature_version,
                    "values": {key: str(value) for key, value in feature.values.items()},
                    "data_quality_ok": feature.data_quality_ok,
                },
                "portfolio": dict(context.portfolio),
                "market_events": list(context.market_events),
                "regime": context.regime,
                "critic": context.critic,
                "constraints": {
                    "authority": "INTENT_ONLY",
                    "broker_access": False,
                    "risk_mutation": False,
                    "tool_access": False,
                    "real_money": False,
                },
            }
        )
        position_payload = None
        if position is not None:
            position_payload = sanitize_packet_payload(
                {
                    "position_id": position.position_id,
                    "broker_position_id": position.broker_position_id,
                    "symbol": position.symbol,
                    "side": position.side.value,
                    "quantity": str(position.quantity),
                    "entry_price": str(position.entry_price),
                    "stop_price": str(position.stop_price),
                    "take_profit_price": str(position.take_profit_price),
                    "expires_at": position.expires_at.isoformat(),
                    "strategy_id": position.strategy_id,
                    "lane_id": position.lane_id,
                }
            )
        seed = json.dumps(
            {
                "lane": lane.value,
                "mode": mode,
                "feature": feature.snapshot_id,
                "market_snapshot_ids": list(market_snapshot_ids),
                "broker_snapshot_hash": context.broker_snapshot_hash,
                "risk_config_hash": context.risk_config_hash,
                "position_id": None if position is None else position.position_id,
                "created_at": now.isoformat(),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        packet_id = f"packet-{hashlib.sha256(seed.encode()).hexdigest()[:24]}"
        return DecisionPacketV2(
            packet_id=packet_id,
            created_at=now.isoformat(),
            expires_at=(now + timedelta(seconds=ttl_seconds)).isoformat(),
            lane=lane.value,
            mode=mode,
            market_snapshot_ids=tuple(market_snapshot_ids),
            feature_snapshot_id=feature.snapshot_id,
            broker_snapshot_hash=context.broker_snapshot_hash,
            risk_config_hash=context.risk_config_hash,
            model_context=model_context,
            candidates=candidates,
            position=position_payload,
            exact_evidence_refs=tuple(sorted(evidence)),
        )


@dataclass(frozen=True)
class DecisionApplyResultV2:
    packet_id: str
    action: str
    applied: bool
    effect: Mapping[str, Any]


class DecisionApplierV2:
    """Translate validated model output into deterministic kernel commands only."""

    def __init__(self, kernel: UnifiedTradingKernel) -> None:
        self.kernel = kernel
        self.store = kernel.store

    @staticmethod
    def _validate_common(
        packet: DecisionPacketV2,
        output: AIIntentOutputV2,
        current: datetime,
    ) -> str | None:
        output.validate(packet)
        critic_rejection = critic_gate_rejection_reason(packet, output.action)
        if critic_rejection is not None:
            return critic_rejection
        if current > datetime.fromisoformat(packet.expires_at.replace("Z", "+00:00")):
            return "packet_expired"
        return None

    def apply_hold(
        self,
        packet: DecisionPacketV2,
        output: AIIntentOutputV2,
        *,
        now: datetime,
    ) -> DecisionApplyResultV2:
        """Consume HOLD through the deterministic applier; it never suppresses exits."""

        if output.action is not AIAction.HOLD:
            raise ValueError("apply_hold accepts HOLD only")
        current = now.astimezone(UTC)
        rejection = self._validate_common(packet, output, current)
        if rejection is not None:
            return DecisionApplyResultV2(packet.packet_id, "HOLD", False, {"reason": rejection})
        return DecisionApplyResultV2(
            packet.packet_id,
            "HOLD",
            True,
            {
                "status": "no_new_risk",
                "mandatory_exits_owned_by": "v2-exit-manager",
            },
        )

    @staticmethod
    def _intent(
        packet: DecisionPacketV2,
        output: AIIntentOutputV2,
        quote: QuoteProvenance,
        *,
        now: datetime,
    ) -> IntentEnvelope:
        if output.action is not AIAction.OPEN:
            raise ValueError("only OPEN can become an IntentEnvelope")
        output.validate(packet)
        candidate = output.selected_candidate(packet)
        if candidate is None or not isinstance(candidate.get("execution_plan"), Mapping):
            raise ValueError("validated AI OPEN output lacks a deterministic candidate plan")
        plan = candidate["execution_plan"]
        symbol = str(candidate["symbol"]).upper()
        side = Side(str(candidate["side"]).lower())
        amount_usd = Decimal(str(plan["amount_usd"]))
        stop_fraction = Decimal(str(candidate["stop_loss_fraction"]))
        take_fraction = Decimal(str(candidate["take_profit_fraction"]))
        max_holding_seconds = int(candidate["max_holding_seconds"])
        max_slippage_bps = Decimal(str(plan["max_slippage_bps"]))
        if symbol != quote.symbol.upper():
            raise ValueError("AI OPEN symbol is not bound to the fresh quote")
        created = now.astimezone(UTC)
        packet_expiry = datetime.fromisoformat(packet.expires_at.replace("Z", "+00:00"))
        expires = min(packet_expiry, created + timedelta(seconds=min(300, max_holding_seconds)))
        seed = f"{packet.packet_hash}:{canonical_hash(asdict(output))}"
        packet_version = str(packet.model_context.get("packet_version", "decision-packet-v2.1"))
        rationale = (
            f"{output.rationale} | uncertainty={output.uncertainty} | "
            f"reason_codes={','.join(output.reason_codes)}"
        )
        return IntentEnvelope(
            intent_id=f"intent-ai-{hashlib.sha256(seed.encode()).hexdigest()[:24]}",
            portfolio_id="master_1000",
            lane_id=packet.lane,
            strategy_id=str(candidate["strategy_id"]),
            strategy_version=str(candidate["strategy_version"]),
            symbol=symbol,
            side=side,
            amount_usd=amount_usd,
            raw_confidence=Decimal(str(candidate["raw_confidence"])),
            confidence_threshold=Decimal(str(candidate["threshold"])),
            stop_loss_fraction=stop_fraction,
            take_profit_fraction=take_fraction,
            max_holding_seconds=max_holding_seconds,
            created_at=created,
            valid_after=created,
            expires_at=expires,
            reference_bid=quote.bid,
            reference_ask=quote.ask,
            max_price_drift_bps=max_slippage_bps,
            max_slippage_bps=max_slippage_bps,
            snapshot_hash=quote.market_snapshot_hash,
            rationale=rationale,
            invalidation_conditions=tuple(output.invalidation_conditions),
            evidence_refs=tuple(output.evidence_refs),
            model_version="gpt-5.6-sol",
            prompt_version=packet_version,
            correlation_id=packet.packet_id,
        )

    def apply(
        self,
        packet: DecisionPacketV2,
        output: AIIntentOutputV2,
        *,
        quote: QuoteProvenance,
        broker: BrokerTruth,
        now: datetime,
    ) -> DecisionApplyResultV2:
        current = now.astimezone(UTC)
        rejection = self._validate_common(packet, output, current)
        if rejection is not None:
            return DecisionApplyResultV2(
                packet.packet_id,
                output.action.value,
                False,
                {"reason": rejection},
            )
        if output.action is AIAction.HOLD:
            return self.apply_hold(packet, output, now=current)
        if output.action is AIAction.OPEN:
            candidate_intent = self._intent(packet, output, quote, now=current)
            intent = self.store.intent_or_none(candidate_intent.intent_id) or candidate_intent
            risk, command = self.kernel.submit_open_intent(intent, quote, broker, now=current)
            return DecisionApplyResultV2(
                packet.packet_id,
                "OPEN",
                bool(risk.approved and command is not None),
                {
                    "intent_id": intent.intent_id,
                    "risk_approved": risk.approved,
                    "risk_reasons": list(risk.reasons),
                    "order_command_id": None if command is None else command.order_command_id,
                },
            )
        position_id = (
            None if packet.position is None else str(packet.position.get("position_id", ""))
        )
        candidates = [
            item
            for item in self.store.positions("master_1000", open_only=True)
            if item.position_id == position_id
        ]
        if len(candidates) != 1:
            return DecisionApplyResultV2(
                packet.packet_id,
                output.action.value,
                False,
                {"reason": "position_binding_mismatch"},
            )
        position = candidates[0]
        units = None
        if output.action is AIAction.PARTIAL_CLOSE:
            if output.partial_close_fraction is None:
                raise ValueError("validated partial close lacks a fraction")
            units = position.quantity * output.partial_close_fraction
        try:
            command = self.kernel.create_close_command(
                position,
                now=current,
                reason=ExitReason.AGENT_CLOSE,
                broker=broker,
                units_to_deduct=units,
            )
        except PermissionError:
            risk = self.kernel.risk.evaluate_reduce(broker)
            return DecisionApplyResultV2(
                packet.packet_id,
                output.action.value,
                False,
                {"risk_approved": False, "risk_reasons": list(risk.reasons)},
            )
        return DecisionApplyResultV2(
            packet.packet_id,
            output.action.value,
            True,
            {
                "order_command_id": command.order_command_id,
                "units_to_deduct": None if units is None else str(units),
            },
        )
