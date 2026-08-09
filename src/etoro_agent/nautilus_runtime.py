from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class ReplayEvent:
    timestamp_ns: int
    event_type: str
    payload_hash: str
    event_hash: str


class ReplayClock:
    """Small deterministic monotonic replay clock and hash chain."""

    def __init__(self) -> None:
        self._timestamp_ns = 0
        self._previous_hash = "0" * 64

    @property
    def timestamp_ns(self) -> int:
        return self._timestamp_ns

    def observe(self, timestamp: datetime, event_type: str, payload_hash: str) -> ReplayEvent:
        if timestamp.tzinfo is None:
            raise ValueError("replay timestamps must be timezone-aware")
        normalized = timestamp.astimezone(timezone.utc)
        timestamp_ns = int(normalized.timestamp() * 1_000_000_000)
        if timestamp_ns < self.timestamp_ns:
            raise ValueError("replay time cannot move backwards")
        self._timestamp_ns = timestamp_ns
        digest = hashlib.sha256(
            f"{self._previous_hash}:{timestamp_ns}:{event_type}:{payload_hash}".encode()
        ).hexdigest()
        event = ReplayEvent(timestamp_ns, event_type, payload_hash, digest)
        self._previous_hash = digest
        return event


# Compatibility for callers and persisted event readers during this release.
NautilusReplayClock = ReplayClock
