from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

_INJECTION_PATTERNS = (
    re.compile(r"ignore\s+(?:all\s+)?previous", re.IGNORECASE),
    re.compile(r"system\s+prompt", re.IGNORECASE),
    re.compile(r"developer\s+message", re.IGNORECASE),
    re.compile(r"execute\s+(?:a\s+)?(?:command|tool|shell)", re.IGNORECASE),
    re.compile(r"api[_ -]?key|password|credential|secret", re.IGNORECASE),
)


@dataclass(frozen=True)
class StructuredMarketEvent:
    event_id: str
    event_type: str
    source: str
    publisher: str
    publication_time: datetime
    observed_at: datetime
    symbols: tuple[str, ...]
    actual: Decimal | None
    prior: Decimal | None
    consensus: Decimal | None
    surprise: Decimal | None
    revision: bool
    ttl_seconds: int
    source_credibility: Decimal
    raw_text_hash: str
    normalized_summary: str

    @property
    def expires_at(self) -> datetime:
        return self.publication_time + timedelta(seconds=self.ttl_seconds)

    def active(self, at: datetime) -> bool:
        return at.astimezone(UTC) <= self.expires_at


def normalize_external_text(text: str, *, maximum: int = 1200) -> tuple[str, str]:
    clean = " ".join(text.split())[:maximum]
    if any(pattern.search(clean) for pattern in _INJECTION_PATTERNS):
        raise ValueError("external text contains prompt-injection-like instructions")
    return clean, hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def numeric_surprise(
    actual: Decimal | None, consensus: Decimal | None, scale: Decimal | None = None
) -> Decimal | None:
    if actual is None or consensus is None:
        return None
    delta = actual - consensus
    if scale is None:
        denominator = max(abs(consensus), Decimal("0.000001"))
        return delta / denominator
    if scale <= 0:
        raise ValueError("surprise scale must be positive")
    return delta / scale
