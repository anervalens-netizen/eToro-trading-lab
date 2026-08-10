from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Mapping, Sequence

from .domain_v2 import ONE, ZERO, Side
from .events_v2 import StructuredMarketEvent, numeric_surprise
from .strategy_v2 import FamilySignal, StrategyFamily


@dataclass(frozen=True)
class CommodityReleaseV2:
    release_id: str
    symbol: str
    event_type: str
    publisher: str
    publication_time: datetime
    actual: Decimal
    consensus: Decimal
    prior: Decimal | None
    scale: Decimal
    revision: bool = False

    @property
    def surprise_zscore(self) -> Decimal:
        result = numeric_surprise(self.actual, self.consensus, self.scale)
        assert result is not None
        return result

    def to_event(self, observed_at: datetime) -> StructuredMarketEvent:
        if self.publication_time.tzinfo is None or observed_at.tzinfo is None:
            raise ValueError("commodity event timestamps must be timezone-aware")
        canonical = (
            f"{self.release_id}:{self.symbol}:{self.event_type}:{self.actual}:"
            f"{self.consensus}:{self.prior}:{self.publication_time.isoformat()}"
        )
        return StructuredMarketEvent(
            event_id=f"commodity-{hashlib.sha256(canonical.encode()).hexdigest()[:24]}",
            event_type=self.event_type,
            source="quantitative_release_v2",
            publisher=self.publisher,
            publication_time=self.publication_time.astimezone(timezone.utc),
            observed_at=observed_at.astimezone(timezone.utc),
            symbols=(self.symbol.upper(),),
            actual=self.actual,
            prior=self.prior,
            consensus=self.consensus,
            surprise=self.surprise_zscore,
            revision=self.revision,
            ttl_seconds=6 * 3600,
            source_credibility=Decimal("1"),
            raw_text_hash=hashlib.sha256(canonical.encode()).hexdigest(),
            normalized_summary=(
                f"{self.event_type}: actual={self.actual};consensus={self.consensus};"
                f"prior={self.prior};surprise_z={self.surprise_zscore}"
            ),
        )


@dataclass(frozen=True)
class TermStructureSnapshotV2:
    symbol: str
    front_price: Decimal
    second_price: Decimal
    days_between_contracts: int
    observed_at: datetime

    @property
    def annualized_carry_fraction(self) -> Decimal:
        if self.front_price <= ZERO or self.second_price <= ZERO or self.days_between_contracts <= 0:
            raise ValueError("term-structure snapshot is invalid")
        raw = self.front_price / self.second_price - ONE
        return raw * Decimal("365") / Decimal(self.days_between_contracts)


class CommodityResearchEngineV2:
    version = "commodity-quant-v2.0"

    def event_signal(
        self,
        release: CommodityReleaseV2,
        *,
        price_confirmation_return: Decimal,
        threshold: Decimal = Decimal("0.65"),
    ) -> FamilySignal | None:
        surprise = release.surprise_zscore
        if abs(surprise) < Decimal("1") or surprise * price_confirmation_return <= ZERO:
            return None
        raw = min(Decimal("0.99"), Decimal("0.50") + min(abs(surprise), Decimal("4")) * Decimal("0.10"))
        return FamilySignal(
            StrategyFamily.COMMODITY_EVENT_CARRY,
            self.version,
            release.symbol.upper(),
            Side.BUY if surprise > ZERO else Side.SELL,
            raw,
            threshold,
            Decimal("0.02"),
            Decimal("0.04"),
            8 * 3600,
            f"event={release.event_type};surprise_z={surprise};confirmation={price_confirmation_return}",
            evidence_refs=(release.release_id,),
        )

    def carry_signal(
        self,
        snapshot: TermStructureSnapshotV2,
        *,
        minimum_abs_carry: Decimal = Decimal("0.05"),
        threshold: Decimal = Decimal("0.65"),
    ) -> FamilySignal | None:
        carry = snapshot.annualized_carry_fraction
        if abs(carry) < minimum_abs_carry:
            return None
        # Backwardation (front > second) is treated as positive carry for long exposure.
        side = Side.BUY if carry > ZERO else Side.SELL
        raw = min(Decimal("0.95"), Decimal("0.55") + min(abs(carry), Decimal("0.50")) * Decimal("0.60"))
        return FamilySignal(
            StrategyFamily.COMMODITY_EVENT_CARRY,
            self.version,
            snapshot.symbol.upper(),
            side,
            raw,
            threshold,
            Decimal("0.02"),
            Decimal("0.04"),
            24 * 3600,
            f"annualized_carry={carry};front={snapshot.front_price};second={snapshot.second_price}",
        )
