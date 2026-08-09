from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from .data_quality import DataQualityReport, validate_candles
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
)

INSTRUMENTS_BY_SYMBOL = {item.symbol: item for item in INSTRUMENT_CATALOG}
INSTRUMENTS_BY_ID = {item.instrument_id: item for item in INSTRUMENT_CATALOG}


def resolve_instrument(symbol: str, instrument_id: int | None = None) -> InstrumentSpec:
    normalized = symbol.upper()
    try:
        instrument = INSTRUMENTS_BY_SYMBOL[normalized]
    except KeyError as exc:
        raise ValueError(f"symbol is not in the seven-instrument catalog: {normalized}") from exc
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
        object.__setattr__(self, "timestamp", self.timestamp.astimezone(timezone.utc))


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

    def __post_init__(self) -> None:
        captured_at = self.captured_at or datetime.now(timezone.utc)
        if captured_at.tzinfo is None:
            raise ValueError("snapshot captured_at must be timezone-aware")
        captured_at = captured_at.astimezone(timezone.utc)
        object.__setattr__(self, "captured_at", captured_at)
        object.__setattr__(self, "symbol", self.symbol.upper())
        if self.schema_version < 1:
            raise ValueError("snapshot schema_version must be positive")
        if self.bid <= 0 or self.ask <= 0 or self.ask < self.bid:
            raise ValueError("invalid bid/ask quote")
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


def _parse_timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        seconds = float(value) / 1000 if float(value) > 10_000_000_000 else float(value)
        parsed = datetime.fromtimestamp(seconds, tz=timezone.utc)
    elif isinstance(value, str):
        normalized = value.strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
    else:
        raise ValueError("candle timestamp is missing or invalid")
    if parsed.tzinfo is None:
        raise ValueError("candle timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


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
    ) -> MarketSnapshot:
        instrument = resolve_instrument(symbol, instrument_id)
        if not 1 <= count <= 1000:
            raise ValueError("candle count must be between 1 and 1000")
        rates = self.client.execute_read(
            "/api/v1/market-data/instruments/rates", {"instrumentIds": str(instrument_id)}
        )
        candles = self.client.execute_read(
            f"/api/v1/market-data/instruments/{instrument_id}/history/candles/asc/{interval}/{count}"
        )
        if not rates.is_success or not candles.is_success:
            raise RuntimeError("market data request failed")
        rate_rows = rates.body.get("rates", [])
        if len(rate_rows) != 1:
            raise ValueError("expected exactly one rate")
        groups = candles.body.get("candles", [])
        rows = groups[0].get("candles", []) if groups else []
        parsed_candles = tuple(_parse_candle(row) for row in rows)
        if not parsed_candles:
            raise ValueError("no candles returned")
        captured_at = now or datetime.now(timezone.utc)
        if captured_at.tzinfo is None:
            raise ValueError("collection time must be timezone-aware")
        captured_at = captured_at.astimezone(timezone.utc)
        quality = validate_candles(
            parsed_candles,
            interval,
            now=captured_at,
            max_gap_intervals=max_gap_intervals,
            max_staleness_intervals=max_staleness_intervals,
        )
        quality.require_valid()
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
        )
