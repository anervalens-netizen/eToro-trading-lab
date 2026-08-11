from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Mapping, Sequence

from .ai_v2 import AIRole, AIAction, AIIntentOutputV2, DecisionPacketV2, Lane, sanitize_packet_payload
from .domain_v2 import ExitReason, IntentEnvelope, PositionState, QuoteProvenance, Side
from .features_v2 import FeatureSnapshotV2
from .kernel_v2 import UnifiedTradingKernel
from .risk_v2 import BrokerTruth
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
    version = "decision-packet-v2.0"

    @staticmethod
    def _candidate(signal: FamilySignal, index: int) -> Mapping[str, Any]:
        payload = {
            "candidate_id": f"candidate-{index:02d}-{hashlib.sha256(signal.rationale.encode()).hexdigest()[:12]}",
            "family": signal.family.value,
            "strategy_version": signal.strategy_version,
            "symbol": signal.symbol,
            "side": signal.side.value,
            "raw_confidence": str(signal.raw_confidence),
            "threshold": str(signal.threshold),
            "actionable": signal.actionable,
            "stop_loss_fraction": str(signal.stop_loss_fraction),
            "take_profit_fraction": str(signal.take_profit_fraction),
            "max_holding_seconds": signal.max_holding_seconds,
            "rationale": signal.rationale,
            "evidence_refs": list(signal.evidence_refs),
        }
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
    ) -> DecisionPacketV2:
        if ttl_seconds < 30 or ttl_seconds > 1800:
            raise ValueError("decision packet TTL must be 30..1800 seconds")
        now = created_at.astimezone(timezone.utc)
        candidates = tuple(
            self._candidate(signal, index)
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
                    "broker_credentials": False,
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
        assert output.symbol is not None
        assert output.side is not None
        assert output.amount_usd is not None
        assert output.stop_loss_fraction is not None
        assert output.take_profit_fraction is not None
        assert output.max_holding_seconds is not None
        assert output.max_slippage_bps is not None
        if output.symbol.upper() != quote.symbol.upper():
            raise ValueError("AI OPEN symbol is not bound to the fresh quote")
        created = now.astimezone(timezone.utc)
        packet_expiry = datetime.fromisoformat(packet.expires_at.replace("Z", "+00:00"))
        expires = min(packet_expiry, created + timedelta(seconds=min(300, output.max_holding_seconds)))
        seed = f"{packet.packet_hash}:{output.hypothesis_id}:{output.symbol}:{output.side}:{created.isoformat()}"
        return IntentEnvelope(
            intent_id=f"intent-ai-{hashlib.sha256(seed.encode()).hexdigest()[:24]}",
            portfolio_id="master_1000",
            lane_id=output.lane_id,
            strategy_id=output.hypothesis_id,
            strategy_version="ai-intent-v2",
            symbol=output.symbol.upper(),
            side=Side(output.side.lower()),
            amount_usd=output.amount_usd,
            confidence=output.confidence,
            uncertainty=output.uncertainty,
            stop_loss_fraction=output.stop_loss_fraction,
            take_profit_fraction=output.take_profit_fraction,
            max_holding_seconds=output.max_holding_seconds,
            signal_event_time=quote.quote_observed_at,
            created_at=created,
            expires_at=expires,
            signal_bid=quote.bid,
            signal_ask=quote.ask,
            max_slippage_bps=output.max_slippage_bps,
            max_price_drift_bps=output.max_slippage_bps,
            market_snapshot_hash=quote.market_snapshot_hash,
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
        output.validate(packet)
        current = now.astimezone(timezone.utc)
        if current > datetime.fromisoformat(packet.expires_at.replace("Z", "+00:00")):
            return DecisionApplyResultV2(packet.packet_id, output.action.value, False, {"reason": "packet_expired"})
        if output.action is AIAction.HOLD:
            return DecisionApplyResultV2(packet.packet_id, "HOLD", True, {"status": "no_new_risk"})
        if output.action is AIAction.OPEN:
            intent = self._intent(packet, output, quote, now=current)
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
        position_id = None if packet.position is None else str(packet.position.get("position_id", ""))
        candidates = [item for item in self.store.positions("master_1000", open_only=True) if item.position_id == position_id]
        if len(candidates) != 1:
            return DecisionApplyResultV2(packet.packet_id, output.action.value, False, {"reason": "position_binding_mismatch"})
        position = candidates[0]
        units = None
        if output.action is AIAction.PARTIAL_CLOSE:
            assert output.partial_close_fraction is not None
            units = position.quantity * output.partial_close_fraction
        command = self.kernel.create_close_command(
            position,
            now=current,
            reason=ExitReason.AGENT_CLOSE,
            units_to_deduct=units,
        )
        return DecisionApplyResultV2(
            packet.packet_id,
            output.action.value,
            True,
            {"order_command_id": command.order_command_id, "units_to_deduct": None if units is None else str(units)},
        )
