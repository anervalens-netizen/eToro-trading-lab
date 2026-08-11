from __future__ import annotations

import hashlib
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from .ai_v2 import AIAction, AIIntentOutputV2, DecisionPacketV2
from .decision_impl_v2 import DecisionApplierV2 as _DecisionApplierV2
from .decision_impl_v2 import (
    DecisionApplyResultV2,
    DecisionPacketBuilderV2,
    DecisionPacketContextV2,
)
from .domain_v2 import IntentEnvelope, QuoteProvenance, Side, canonical_hash


class DecisionApplierV2(_DecisionApplierV2):
    """Apply validated AI output to the canonical v2 intent contract."""

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
        if any(
            value is None
            for value in (
                output.symbol,
                output.side,
                output.amount_usd,
                output.stop_loss_fraction,
                output.take_profit_fraction,
                output.max_holding_seconds,
                output.max_slippage_bps,
            )
        ):
            raise ValueError("validated AI OPEN output lacks required fields")
        if output.symbol.upper() != quote.symbol.upper():
            raise ValueError("AI OPEN symbol is not bound to the fresh quote")

        created = now.astimezone(UTC)
        packet_expiry = datetime.fromisoformat(packet.expires_at.replace("Z", "+00:00"))
        expires = min(
            packet_expiry,
            created + timedelta(seconds=min(300, output.max_holding_seconds)),
        )
        seed = f"{packet.packet_hash}:{canonical_hash(asdict(output))}"
        packet_version = str(packet.model_context.get("packet_version", "decision-packet-v2.0"))
        rationale = (
            f"{output.rationale} | uncertainty={output.uncertainty} | "
            f"reason_codes={','.join(output.reason_codes)}"
        )
        return IntentEnvelope(
            intent_id=f"intent-ai-{hashlib.sha256(seed.encode()).hexdigest()[:24]}",
            portfolio_id="master_1000",
            lane_id=output.lane_id,
            strategy_id=output.hypothesis_id,
            strategy_version="ai-intent-v2",
            symbol=output.symbol.upper(),
            side=Side(output.side.lower()),
            amount_usd=output.amount_usd,
            raw_confidence=output.confidence,
            confidence_threshold=Decimal("0"),
            stop_loss_fraction=output.stop_loss_fraction,
            take_profit_fraction=output.take_profit_fraction,
            max_holding_seconds=output.max_holding_seconds,
            created_at=created,
            valid_after=created,
            expires_at=expires,
            reference_bid=quote.bid,
            reference_ask=quote.ask,
            max_price_drift_bps=output.max_slippage_bps,
            max_slippage_bps=output.max_slippage_bps,
            snapshot_hash=quote.market_snapshot_hash,
            rationale=rationale,
            invalidation_conditions=tuple(output.invalidation_conditions),
            evidence_refs=tuple(output.evidence_refs),
            model_version="gpt-5.6-sol",
            prompt_version=packet_version,
            correlation_id=packet.packet_id,
        )


__all__ = [
    "DecisionApplyResultV2",
    "DecisionApplierV2",
    "DecisionPacketBuilderV2",
    "DecisionPacketContextV2",
]
