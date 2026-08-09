from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Protocol, Sequence


class CandleLike(Protocol):
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


INTERVAL_DURATIONS: dict[str, timedelta] = {
    "OneMinute": timedelta(minutes=1),
    "FiveMinutes": timedelta(minutes=5),
    "TenMinutes": timedelta(minutes=10),
    "FifteenMinutes": timedelta(minutes=15),
    "ThirtyMinutes": timedelta(minutes=30),
    "OneHour": timedelta(hours=1),
    "FourHours": timedelta(hours=4),
    "OneDay": timedelta(days=1),
    "OneWeek": timedelta(weeks=1),
}


@dataclass(frozen=True)
class DataQualityIssue:
    code: str
    detail: str
    candle_index: int | None = None


@dataclass(frozen=True)
class DataQualityReport:
    checked_at: datetime
    interval: str
    candle_count: int
    expected_interval_seconds: int
    freshness_seconds: int | None
    issues: tuple[DataQualityIssue, ...]

    @property
    def is_valid(self) -> bool:
        return not self.issues

    def require_valid(self) -> None:
        if self.issues:
            codes = ",".join(issue.code for issue in self.issues)
            raise MarketDataQualityError(f"market data quality validation failed: {codes}", self)


class MarketDataQualityError(ValueError):
    def __init__(self, message: str, report: DataQualityReport) -> None:
        super().__init__(message)
        self.report = report


def validate_candles(
    candles: Sequence[CandleLike],
    interval: str,
    *,
    now: datetime | None = None,
    max_gap_intervals: int = 1,
    max_staleness_intervals: int = 2,
) -> DataQualityReport:
    """Validate an ordered, closed-candle series and return every detected defect.

    Gaps are intentionally strict by default. Callers collecting instruments with
    session closures must explicitly choose a larger allowance or provide
    session-normalized candles instead of silently accepting missing data.
    """

    if interval not in INTERVAL_DURATIONS:
        raise ValueError(f"unsupported candle interval: {interval}")
    if max_gap_intervals < 1:
        raise ValueError("max_gap_intervals must be at least 1")
    if max_staleness_intervals < 1:
        raise ValueError("max_staleness_intervals must be at least 1")

    checked_at = now or datetime.now(timezone.utc)
    if checked_at.tzinfo is None:
        raise ValueError("quality check time must be timezone-aware")
    checked_at = checked_at.astimezone(timezone.utc)
    duration = INTERVAL_DURATIONS[interval]
    issues: list[DataQualityIssue] = []
    seen: set[datetime] = set()
    previous: datetime | None = None
    last_normalized: datetime | None = None

    if not candles:
        issues.append(DataQualityIssue("empty_series", "no candles supplied"))

    for index, candle in enumerate(candles):
        timestamp = candle.timestamp
        if timestamp.tzinfo is None or timestamp.utcoffset() != timedelta(0):
            issues.append(
                DataQualityIssue("timestamp_not_utc", "candle timestamp is not explicit UTC", index)
            )
            normalized = timestamp.replace(tzinfo=timezone.utc) if timestamp.tzinfo is None else timestamp.astimezone(timezone.utc)
        else:
            normalized = timestamp.astimezone(timezone.utc)
        last_normalized = normalized

        if normalized in seen:
            issues.append(DataQualityIssue("duplicate_timestamp", normalized.isoformat(), index))
        seen.add(normalized)

        if previous is not None:
            delta = normalized - previous
            if delta <= timedelta(0):
                issues.append(DataQualityIssue("timestamps_not_ascending", normalized.isoformat(), index))
            elif delta > duration * max_gap_intervals:
                issues.append(
                    DataQualityIssue(
                        "candle_gap",
                        f"gap_seconds={int(delta.total_seconds())}",
                        index,
                    )
                )
        previous = normalized

        prices = (candle.open, candle.high, candle.low, candle.close)
        if any(price <= 0 for price in prices):
            issues.append(DataQualityIssue("non_positive_price", "OHLC must be positive", index))
        if candle.high < max(candle.open, candle.close, candle.low):
            issues.append(DataQualityIssue("invalid_high", "high is below another OHLC value", index))
        if candle.low > min(candle.open, candle.close, candle.high):
            issues.append(DataQualityIssue("invalid_low", "low is above another OHLC value", index))
        if normalized > checked_at + duration:
            issues.append(DataQualityIssue("future_timestamp", normalized.isoformat(), index))

    freshness_seconds: int | None = None
    if last_normalized is not None:
        freshness_seconds = max(0, int((checked_at - last_normalized).total_seconds()))
        if checked_at - last_normalized > duration * max_staleness_intervals:
            issues.append(
                DataQualityIssue(
                    "stale_series",
                    f"freshness_seconds={freshness_seconds}",
                    len(candles) - 1,
                )
            )

    return DataQualityReport(
        checked_at=checked_at,
        interval=interval,
        candle_count=len(candles),
        expected_interval_seconds=int(duration.total_seconds()),
        freshness_seconds=freshness_seconds,
        issues=tuple(issues),
    )
