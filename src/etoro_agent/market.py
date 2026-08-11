from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from .data_quality import INTERVAL_DURATIONS, DataQualityReport, validate_candles
from .mcp import EtoroMCPClient


@dataclass(frozen=True)
class InstrumentSpec:
    symbol: str
    instrument_id: int
    asset_class: str
    quote_currency: str = "USD"


INSTRUMENT_CATALOG: tuple[InstrumentSpec, ...] = (
    InstrumentSpec("EURUSD", 1, "fx"),
    InstrumentSpec("SPX500", 27, "index"),
    InstrumentSpec("NSDQ100", 28, "index"),
    InstrumentSpec("AAPL", 1001, "equity"),
    InstrumentSpec("TSLA", 1111, "equity"),
    InstrumentSpec("BTC", 100000, "crypto"),
    InstrumentSpec("ETH", 100001, "crypto"),
    InstrumentSpec("OIL", 17, "commodity"),
    InstrumentSpec("NATGAS", 22, "commodity"),
)

INSTRUMENTS_BY_SYMBOL = {item.symbol: item for item in INSTRUMENT_CATALOG}
INSTRUMENTS_BY_ID = {item.instrument_id: item for item in INSTRUMENT_CATALOG}


def resolve_instrument(symbol: str, instrument_id: int | None = None) -> InstrumentSpec:
    normalized = symbol.upper()
    try:
        instrument = INSTRUMENTS_BY_SYMBOL[normalized]
    except KeyError as exc:
        raise ValueError(f"symbol is not in the fixed instrument catalog: {normalized}") from exc
    if instrument_id is not None and instrument.instrument_id != instrument_id:
        raise ValueError(
            f"instrument mapping mismatch for {normalized}: expected {instrument.instrument_id}, got {instrument_id}"
        )
    return instrument


@dataclass(frozen=True)
class CandleSnapshot:
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None = None

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None:
            raise ValueError("candle timestamp must be timezone-aware")
        object.__setattr__(self, "timestamp", self.timestamp.astimezone(UTC))
        prices = (self.open, self.high, self.low, self.close)
        if not all(value.is_finite() for value in prices) or min(prices) <= 0:
            raise ValueError("candle OHLC must be finite and positive")
        if self.high < max(prices) or self.low > min(prices):
            raise ValueError("candle OHLC range is invalid")
        if self.volume is not None and (not self.volume.is_finite() or self.volume < 0):
            raise ValueError("candle volume must be finite and non-negative")


@dataclass(frozen=True)
class MarketSnapshot:
    symbol: str
    instrument_id: int
    bid: Decimal
    ask: Decimal
    closes: tuple[Decimal, ...]
    candles: tuple[CandleSnapshot, ...] = ()
    interval: str = ""
    captured_at: datetime | None = None
    schema_version: int = 1
    content_hash: str = ""
    quality: DataQualityReport | None = None
    market_open: bool = True
    quote_observed_at: datetime | None = None

    def __post_init__(self) -> None:
        captured_at = self.captured_at or datetime.now(UTC)
        if captured_at.tzinfo is None:
            raise ValueError("snapshot captured_at must be timezone-aware")
        captured_at = captured_at.astimezone(UTC)
        object.__setattr__(self, "captured_at", captured_at)
        quote_observed_at = self.quote_observed_at or captured_at
        if quote_observed_at.tzinfo is None:
            raise ValueError("quote_observed_at must be timezone-aware")
        quote_observed_at = quote_observed_at.astimezone(UTC)
        object.__setattr__(self, "quote_observed_at", quote_observed_at)
        object.__setattr__(self, "symbol", self.symbol.upper())
        resolve_instrument(self.symbol, self.instrument_id)
        if self.schema_version < 1:
            raise ValueError("snapshot schema_version must be positive")
        if not all(value.is_finite() for value in (self.bid, self.ask, *self.closes)):
            raise ValueError("market snapshot contains non-finite prices")
        if self.bid <= 0 or self.ask <= 0 or self.ask < self.bid:
            raise ValueError("invalid bid/ask quote")
        if any(close <= 0 for close in self.closes):
            raise ValueError("market snapshot closes must be positive")
        if self.candles and self.closes != tuple(candle.close for candle in self.candles):
            raise ValueError("closes must match immutable candle snapshots")
        if not self.content_hash:
            canonical = {
                "schema_version": self.schema_version,
                "symbol": self.symbol,
                "instrument_id": self.instrument_id,
                "bid": str(self.bid),
                "ask": str(self.ask),
                "interval": self.interval,
                "captured_at": captured_at.isoformat(),
                "quote_observed_at": quote_observed_at.isoformat(),
                "market_open": self.market_open,
                "closes": [str(close) for close in self.closes],
                "candles": [
                    {
                        "timestamp": candle.timestamp.isoformat(),
                        "open": str(candle.open),
                        "high": str(candle.high),
                        "low": str(candle.low),
                        "close": str(candle.close),
                        "volume": None if candle.volume is None else str(candle.volume),
                    }
                    for candle in self.candles
                ],
            }
            digest = hashlib.sha256(
                json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            object.__setattr__(self, "content_hash", digest)


def market_is_open(instrument: InstrumentSpec, at: datetime) -> bool:
    """Conservative session gate used only to permit new shadow/DEMO opens."""

    normalized = at.astimezone(UTC)
    if instrument.asset_class == "crypto":
        return True
    if instrument.asset_class == "equity":
        local = normalized.astimezone(ZoneInfo("America/New_York"))
        minutes = local.hour * 60 + local.minute
        return local.weekday() < 5 and 570 <= minutes < 960
    weekday = normalized.weekday()
    minutes = normalized.hour * 60 + normalized.minute
    if instrument.asset_class == "index":
        if weekday == 5:
            return False
        if weekday == 6:
            return minutes >= 22 * 60
        if weekday == 4:
            return minutes < 20 * 60 + 30
        return not 21 * 60 <= minutes < 22 * 60
    if instrument.asset_class == "commodity":
        if weekday == 5:
            return False
        if weekday == 6:
            return minutes >= 22 * 60
        if weekday == 4:
            return minutes < 20 * 60 + 30
        maintenance_start = 21 * 60 if instrument.symbol == "OIL" else 20 * 60 + 55
        return not maintenance_start <= minutes < 22 * 60
    if weekday == 5 or (weekday == 6 and normalized.hour < 21):
        return False
    return not (weekday == 4 and normalized.hour >= 21)


def _session_adjusted_report(
    report: DataQualityReport,
    candles: tuple[CandleSnapshot, ...],
    instrument: InstrumentSpec,
    is_open: bool,
) -> DataQualityReport:
    issues = []
    for issue in report.issues:
        if issue.code == "stale_series" and not is_open:
            continue
        if (
            issue.code == "candle_gap"
            and instrument.asset_class != "crypto"
            and issue.candle_index is not None
            and issue.candle_index > 0
        ):
            previous = candles[issue.candle_index - 1].timestamp
            current = candles[issue.candle_index].timestamp
            crosses_date = previous.date() != current.date()
            maintenance_break = (
                instrument.asset_class in {"index", "commodity"}
                and previous.hour == 20
                and current.hour == 22
                and current - previous <= timedelta(hours=2)
            )
            if crosses_date or maintenance_break:
                continue
        issues.append(issue)
    return DataQualityReport(
        checked_at=report.checked_at,
        interval=report.interval,
        candle_count=report.candle_count,
        expected_interval_seconds=report.expected_interval_seconds,
        freshness_seconds=report.freshness_seconds,
        issues=tuple(issues),
    )


def _parse_timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        seconds = float(value) / 1000 if float(value) > 10_000_000_000 else float(value)
        parsed = datetime.fromtimestamp(seconds, tz=UTC)
    elif isinstance(value, str):
        normalized = value.strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
    else:
        raise ValueError("candle timestamp is missing or invalid")
    if parsed.tzinfo is None:
        raise ValueError("candle timestamp must include a timezone")
    return parsed.astimezone(UTC)


def _parse_candle(row: dict[str, Any]) -> CandleSnapshot:
    timestamp_value = next(
        (
            row[key]
            for key in (
                "from",
                "fromDate",
                "timestamp",
                "date",
                "time",
                "start",
                "datetime",
            )
            if key in row
        ),
        None,
    )
    timestamp = _parse_timestamp(timestamp_value)
    close = Decimal(str(row["close"]))
    volume_value = row.get("volume")
    return CandleSnapshot(
        timestamp=timestamp,
        open=Decimal(str(row.get("open", close))),
        high=Decimal(str(row.get("high", close))),
        low=Decimal(str(row.get("low", close))),
        close=close,
        volume=None if volume_value is None else Decimal(str(volume_value)),
    )


class MarketDataCollector:
    def __init__(self, client: EtoroMCPClient) -> None:
        self.client = client

    def collect(
        self,
        symbol: str,
        instrument_id: int,
        interval: str,
        count: int,
        *,
        now: datetime | None = None,
        max_gap_intervals: int = 1,
        max_staleness_intervals: int = 2,
        close_grace_seconds: int = 0,
    ) -> MarketSnapshot:
        instrument = resolve_instrument(symbol, instrument_id)
        if not 1 <= count <= 1000:
            raise ValueError("candle count must be between 1 and 1000")
        if not 0 <= close_grace_seconds <= 300:
            raise ValueError("close grace must be between zero and five minutes")
        rates = self.client.execute_read(
            "/api/v1/market-data/instruments/rates", {"instrumentIds": str(instrument_id)}
        )
        candles = self.client.execute_read(
            f"/api/v1/market-data/instruments/{instrument_id}/history/candles/asc/{interval}/{min(count + 1, 1000)}"
        )
        if not rates.is_success or not candles.is_success:
            raise RuntimeError("market data request failed")
        rate_rows = rates.body.get("rates", [])
        if len(rate_rows) != 1:
            raise ValueError("expected exactly one rate")
        groups = candles.body.get("candles", [])
        rows = groups[0].get("candles", []) if groups else []
        captured_at = now or datetime.now(UTC)
        if captured_at.tzinfo is None:
            raise ValueError("collection time must be timezone-aware")
        captured_at = captured_at.astimezone(UTC)
        duration = INTERVAL_DURATIONS.get(interval)
        if duration is None:
            raise ValueError(f"unsupported candle interval: {interval}")
        grace = timedelta(seconds=close_grace_seconds)
        parsed_candles = tuple(
            candle
            for candle in (_parse_candle(row) for row in rows)
            if candle.timestamp + duration + grace <= captured_at
        )[-count:]
        if not parsed_candles:
            raise ValueError("no closed candles returned")
        quality = validate_candles(
            parsed_candles,
            interval,
            now=captured_at,
            max_gap_intervals=max_gap_intervals,
            max_staleness_intervals=max_staleness_intervals,
        )
        is_open = market_is_open(instrument, captured_at)
        quality = _session_adjusted_report(quality, parsed_candles, instrument, is_open)
        quality.require_valid()
        rate_timestamp = _parse_timestamp(rate_rows[0].get("date"))
        if rate_timestamp > captured_at + timedelta(seconds=5):
            raise ValueError("market quote timestamp is in the future")
        return MarketSnapshot(
            symbol=instrument.symbol,
            instrument_id=instrument.instrument_id,
            bid=Decimal(str(rate_rows[0]["bid"])),
            ask=Decimal(str(rate_rows[0]["ask"])),
            closes=tuple(candle.close for candle in parsed_candles),
            candles=parsed_candles,
            interval=interval,
            captured_at=captured_at,
            quality=quality,
            market_open=is_open,
            quote_observed_at=rate_timestamp,
        )
