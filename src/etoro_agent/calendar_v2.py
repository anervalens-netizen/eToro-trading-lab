from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class SessionException:
    day: date
    closed: bool = False
    early_close: time | None = None
    late_open: time | None = None


@dataclass(frozen=True)
class SessionSpec:
    session_id: str
    timezone_name: str
    open_time: time
    close_time: time
    weekdays: frozenset[int]
    exceptions: tuple[SessionException, ...] = ()

    @property
    def version(self) -> str:
        parts = [self.session_id, self.timezone_name, str(self.open_time), str(self.close_time)]
        parts.extend(f"{x.day}:{x.closed}:{x.early_close}:{x.late_open}" for x in self.exceptions)
        import hashlib

        return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]

    def is_open(self, timestamp: datetime) -> bool:
        if timestamp.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        local = timestamp.astimezone(ZoneInfo(self.timezone_name))
        if local.weekday() not in self.weekdays:
            return False
        exception = next((item for item in self.exceptions if item.day == local.date()), None)
        if exception is not None and exception.closed:
            return False
        start = exception.late_open if exception and exception.late_open else self.open_time
        end = exception.early_close if exception and exception.early_close else self.close_time
        current = local.timetz().replace(tzinfo=None)
        return start <= current < end


US_EQUITY_REGULAR = SessionSpec(
    "US_EQUITY_REGULAR",
    "America/New_York",
    time(9, 30),
    time(16, 0),
    frozenset({0, 1, 2, 3, 4}),
)


def require_synchronized(timestamps: Iterable[datetime], tolerance_seconds: int = 2) -> datetime:
    raw = list(timestamps)
    if not raw:
        raise ValueError("at least one timestamp is required")
    if any(value.tzinfo is None for value in raw):
        raise ValueError("timestamps must be timezone-aware")
    values = [value.astimezone(UTC) for value in raw]
    if (max(values) - min(values)).total_seconds() > tolerance_seconds:
        raise ValueError("multi-leg snapshots are not synchronized")
    return max(values)
