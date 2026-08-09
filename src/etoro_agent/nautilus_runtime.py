from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone

import nautilus_trader
from nautilus_trader.common.component import TestClock


PINNED_NAUTILUS_VERSION = "1.231.0"


@dataclass(frozen=True)
class ReplayEvent:
    timestamp_ns: int
    event_type: str
    payload_hash: str
    event_hash: str


class NautilusReplayClock:
    """Offline Nautilus clock used to order and hash shadow/replay events."""

    def __init__(self) -> None:
        if nautilus_trader.__version__ != PINNED_NAUTILUS_VERSION:
            raise RuntimeError(
                f"NautilusTrader version drift: expected {PINNED_NAUTILUS_VERSION}, "
                f"got {nautilus_trader.__version__}"
            )
        self._clock = TestClock()
        self._previous_hash = "0" * 64

    @property
    def timestamp_ns(self) -> int:
        return int(self._clock.timestamp_ns())

    def observe(self, timestamp: datetime, event_type: str, payload_hash: str) -> ReplayEvent:
        if timestamp.tzinfo is None:
            raise ValueError("Nautilus replay timestamps must be timezone-aware")
        normalized = timestamp.astimezone(timezone.utc)
        timestamp_ns = int(normalized.timestamp() * 1_000_000_000)
        if timestamp_ns < self.timestamp_ns:
            raise ValueError("Nautilus replay time cannot move backwards")
        self._clock.set_time(timestamp_ns)
        digest = hashlib.sha256(
            f"{self._previous_hash}:{timestamp_ns}:{event_type}:{payload_hash}".encode()
        ).hexdigest()
        event = ReplayEvent(timestamp_ns, event_type, payload_hash, digest)
        self._previous_hash = digest
        return event

