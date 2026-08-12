from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .strict_parsing_v2 import (
    load_strict_json_object,
    strict_int,
    strict_list,
    strict_object,
    strict_string,
)

_CLOCK = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]")


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


@dataclass(frozen=True)
class MarketCalendarReleaseV2:
    release_id: str
    source_url: str
    fetched_at: datetime
    valid_from: datetime
    valid_until: datetime
    sessions: dict[
        str,
        tuple[
            str,
            frozenset[int],
            tuple[tuple[time, time], ...],
            dict[date, tuple[tuple[time, time], ...]],
        ],
    ]
    release_hash: str

    @staticmethod
    def _session_is_open(
        session: tuple[
            str,
            frozenset[int],
            tuple[tuple[time, time], ...],
            dict[date, tuple[tuple[time, time], ...]],
        ],
        timestamp: datetime,
    ) -> bool:
        timezone_name, weekdays, windows, exceptions = session
        local = timestamp.astimezone(ZoneInfo(timezone_name))
        if local.weekday() not in weekdays:
            return False
        active_windows = exceptions.get(local.date(), windows)
        local_time = local.timetz().replace(tzinfo=None)
        return any(start <= local_time < end for start, end in active_windows)

    @staticmethod
    def _session_exists_on_date(
        session: tuple[
            str,
            frozenset[int],
            tuple[tuple[time, time], ...],
            dict[date, tuple[tuple[time, time], ...]],
        ],
        day: date,
    ) -> bool:
        timezone_name, weekdays, windows, exceptions = session
        del timezone_name
        return day.weekday() in weekdays and bool(exceptions.get(day, windows))

    def is_open(self, symbol: str, timestamp: datetime) -> bool:
        if timestamp.tzinfo is None:
            raise ValueError("calendar timestamp must be timezone-aware")
        current = timestamp.astimezone(UTC)
        if not self.valid_from <= current < self.valid_until:
            return False
        session = self.sessions.get(symbol.upper())
        if session is None:
            return False
        return self._session_is_open(session, current)

    def explains_candle_gap(
        self,
        symbol: str,
        previous: datetime,
        current: datetime,
        interval: timedelta,
    ) -> bool:
        """Return true only when every missing interval is a scheduled closure."""

        if previous.tzinfo is None or current.tzinfo is None:
            raise ValueError("calendar gap endpoints must be timezone-aware")
        previous_utc = previous.astimezone(UTC)
        current_utc = current.astimezone(UTC)
        if (
            interval <= timedelta(0)
            or current_utc <= previous_utc + interval
            or not self.valid_from <= previous_utc < self.valid_until
            or not self.valid_from <= current_utc < self.valid_until
        ):
            return False
        session = self.sessions.get(symbol.upper())
        if session is None:
            return False
        timezone_name = session[0]
        if interval >= timedelta(days=1):
            day = previous.astimezone(ZoneInfo(timezone_name)).date() + timedelta(days=1)
            end_day = current.astimezone(ZoneInfo(timezone_name)).date()
            checked = 0
            while day < end_day:
                checked += 1
                if checked > 370 or self._session_exists_on_date(session, day):
                    return False
                day += timedelta(days=1)
            return checked > 0

        probe = previous_utc + interval
        end = current_utc
        checked = 0
        while probe < end:
            checked += 1
            if checked > 10000 or self._session_is_open(session, probe):
                return False
            probe += interval
        return checked > 0


def _parse_clock(value: object, label: str) -> time:
    raw = strict_string(value, label=label)
    if _CLOCK.fullmatch(raw) is None:
        raise ValueError(f"{label} must use HH:MM")
    return time.fromisoformat(raw)


def _parse_windows(value: object, label: str) -> tuple[tuple[time, time], ...]:
    windows: list[tuple[time, time]] = []
    for index, raw in enumerate(strict_list(value, label=label)):
        item = strict_object(
            raw,
            label=f"{label}[{index}]",
            required=("open", "close"),
        )
        start = _parse_clock(item["open"], f"{label}[{index}].open")
        end = _parse_clock(item["close"], f"{label}[{index}].close")
        if end <= start:
            raise ValueError("calendar windows cannot cross midnight")
        windows.append((start, end))
    if not windows:
        raise ValueError(f"{label} requires at least one conservative window")
    if any(windows[index][0] < windows[index - 1][1] for index in range(1, len(windows))):
        raise ValueError(f"{label} windows overlap or are unsorted")
    return tuple(windows)


def load_market_calendar_release(path: str | Path) -> MarketCalendarReleaseV2:
    raw = strict_object(
        load_strict_json_object(path),
        label="market calendar release",
        required=(
            "schema_version",
            "release_id",
            "source_url",
            "fetched_at",
            "valid_from",
            "valid_until",
            "sessions",
        ),
    )
    if strict_int(raw["schema_version"], label="calendar schema") != 1:
        raise ValueError("market calendar schema is unsupported")
    source_url = strict_string(raw["source_url"], label="calendar source URL")
    if source_url != "https://www.etoro.com/trading/market-hours-and-events/":
        raise ValueError("market calendar source is not the pinned broker authority")

    def timestamp(name: str) -> datetime:
        value = datetime.fromisoformat(strict_string(raw[name], label=name).replace("Z", "+00:00"))
        if value.tzinfo is None:
            raise ValueError(f"calendar {name} must be timezone-aware")
        return value.astimezone(UTC)

    fetched_at = timestamp("fetched_at")
    valid_from = timestamp("valid_from")
    valid_until = timestamp("valid_until")
    if not valid_from <= fetched_at < valid_until or valid_until - valid_from > timedelta(days=31):
        raise ValueError("market calendar validity interval is invalid")
    raw_sessions = raw["sessions"]
    if not isinstance(raw_sessions, dict) or not raw_sessions:
        raise ValueError("market calendar sessions must be an object")
    sessions: dict[
        str,
        tuple[
            str,
            frozenset[int],
            tuple[tuple[time, time], ...],
            dict[date, tuple[tuple[time, time], ...]],
        ],
    ] = {}
    for raw_symbol, raw_session in raw_sessions.items():
        symbol = strict_string(raw_symbol, label="calendar symbol").upper()
        item = strict_object(
            raw_session,
            label=f"calendar session {symbol}",
            required=("timezone", "weekdays", "windows", "exceptions"),
        )
        timezone_name = strict_string(item["timezone"], label=f"{symbol}.timezone")
        ZoneInfo(timezone_name)
        weekdays = frozenset(
            strict_int(value, label=f"{symbol}.weekday", minimum=0, maximum=6)
            for value in strict_list(item["weekdays"], label=f"{symbol}.weekdays")
        )
        if not weekdays:
            raise ValueError(f"{symbol} calendar weekdays are empty")
        windows = _parse_windows(item["windows"], f"{symbol}.windows")
        raw_exceptions = item["exceptions"]
        if not isinstance(raw_exceptions, dict):
            raise ValueError(f"{symbol}.exceptions must be an object")
        exceptions: dict[date, tuple[tuple[time, time], ...]] = {}
        for raw_day, raw_windows in raw_exceptions.items():
            day = date.fromisoformat(strict_string(raw_day, label=f"{symbol}.exception date"))
            exceptions[day] = (
                ()
                if raw_windows == []
                else _parse_windows(raw_windows, f"{symbol}.exceptions.{day}")
            )
        sessions[symbol] = (timezone_name, weekdays, windows, exceptions)
    canonical = json.dumps(raw, sort_keys=True, separators=(",", ":"), default=str)
    return MarketCalendarReleaseV2(
        strict_string(raw["release_id"], label="calendar release id"),
        source_url,
        fetched_at,
        valid_from,
        valid_until,
        sessions,
        hashlib.sha256(canonical.encode()).hexdigest(),
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
