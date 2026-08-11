from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

ZERO = Decimal("0")


def _utc_timestamp(value: object) -> datetime:
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        timestamp = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"invalid fill timestamp: {value}") from exc
    if timestamp.tzinfo is None:
        raise ValueError("fill timestamp must be timezone-aware")
    return timestamp.astimezone(UTC)


def _decimal(value: object, field_name: str, *, positive: bool = False) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid {field_name}") from exc
    if not parsed.is_finite() or (positive and parsed <= ZERO):
        raise ValueError(f"invalid {field_name}")
    return parsed


def _trade_id(portfolio_id: str, symbol: str, opening_fill_id: int, side: str) -> str:
    source = f"{portfolio_id}\0{symbol}\0{opening_fill_id}\0{side}".encode()
    return f"trade_{hashlib.sha256(source).hexdigest()[:24]}"


@dataclass(frozen=True)
class FillRecord:
    fill_id: int
    timestamp: datetime
    portfolio_id: str
    symbol: str
    side: str
    units: Decimal
    price: Decimal
    fee_usd: Decimal
    recorded_realized_pnl_usd: Decimal

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> FillRecord:
        try:
            fill_id = int(row["id"])
            portfolio_id = str(row["portfolio_id"]).strip()
            symbol = str(row["symbol"]).strip().upper()
            side = str(row["side"]).strip().lower()
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("invalid shadow fill row") from exc
        if fill_id <= 0 or not portfolio_id or not symbol or side not in {"buy", "sell"}:
            raise ValueError("invalid shadow fill row")
        fee = _decimal(row["fee_usd"], "fill fee")
        if fee < ZERO:
            raise ValueError("invalid fill fee")
        return cls(
            fill_id=fill_id,
            timestamp=_utc_timestamp(row["ts"]),
            portfolio_id=portfolio_id,
            symbol=symbol,
            side=side,
            units=_decimal(row["units"], "fill units", positive=True),
            price=_decimal(row["price"], "fill price", positive=True),
            fee_usd=fee,
            recorded_realized_pnl_usd=_decimal(row["realized_pnl_usd"], "recorded realized P&L"),
        )


@dataclass(frozen=True)
class TradeRecord:
    trade_id: str
    portfolio_id: str
    symbol: str
    side: str
    status: str
    opened_at: datetime
    closed_at: datetime | None
    entry_units: Decimal
    exit_units: Decimal
    open_units: Decimal
    entry_average_price: Decimal
    exit_average_price: Decimal | None
    current_average_price: Decimal | None
    entry_notional_usd: Decimal
    exit_notional_usd: Decimal
    gross_pnl_usd: Decimal
    fees_usd: Decimal
    net_pnl_usd: Decimal
    recorded_realized_pnl_usd: Decimal
    realized_reconciliation_delta_usd: Decimal
    duration_seconds: int | None
    opening_fill_id: int
    closing_fill_id: int | None
    fills: tuple[dict[str, str | int], ...]

    def to_dict(self, *, include_fills: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {
            "trade_id": self.trade_id,
            "portfolio_id": self.portfolio_id,
            "symbol": self.symbol,
            "side": self.side,
            "status": self.status,
            "opened_at": self.opened_at.isoformat(),
            "closed_at": self.closed_at.isoformat() if self.closed_at else None,
            "entry_units": str(self.entry_units),
            "exit_units": str(self.exit_units),
            "open_units": str(self.open_units),
            "entry_average_price": str(self.entry_average_price),
            "exit_average_price": (
                str(self.exit_average_price) if self.exit_average_price is not None else None
            ),
            "current_average_price": (
                str(self.current_average_price) if self.current_average_price is not None else None
            ),
            "entry_notional_usd": str(self.entry_notional_usd),
            "exit_notional_usd": str(self.exit_notional_usd),
            "gross_pnl_usd": str(self.gross_pnl_usd),
            "fees_usd": str(self.fees_usd),
            "net_pnl_usd": str(self.net_pnl_usd),
            "recorded_realized_pnl_usd": str(self.recorded_realized_pnl_usd),
            "realized_reconciliation_delta_usd": str(self.realized_reconciliation_delta_usd),
            "duration_seconds": self.duration_seconds,
            "opening_fill_id": self.opening_fill_id,
            "closing_fill_id": self.closing_fill_id,
        }
        if include_fills:
            result["fills"] = list(self.fills)
        return result


@dataclass
class _TradeBuilder:
    trade_id: str
    portfolio_id: str
    symbol: str
    direction: int
    opened_at: datetime
    opening_fill_id: int
    position_units: Decimal = ZERO
    average_price: Decimal = ZERO
    entry_units: Decimal = ZERO
    exit_units: Decimal = ZERO
    entry_notional: Decimal = ZERO
    exit_notional: Decimal = ZERO
    gross_pnl: Decimal = ZERO
    fees: Decimal = ZERO
    recorded_realized: Decimal = ZERO
    closed_at: datetime | None = None
    closing_fill_id: int | None = None
    fills: list[dict[str, str | int]] = field(default_factory=list)

    @property
    def side(self) -> str:
        return "long" if self.direction > 0 else "short"

    def add_leg(
        self,
        fill: FillRecord,
        *,
        role: str,
        units: Decimal,
        fee: Decimal,
        realized: Decimal = ZERO,
    ) -> None:
        self.fills.append(
            {
                "fill_id": fill.fill_id,
                "timestamp": fill.timestamp.isoformat(),
                "side": fill.side,
                "role": role,
                "units": str(units),
                "price": str(fill.price),
                "fee_usd": str(fee),
                "realized_pnl_usd": str(realized),
            }
        )

    def open(self, fill: FillRecord, units: Decimal, fee: Decimal) -> None:
        current_abs = abs(self.position_units)
        new_abs = current_abs + units
        self.average_price = (current_abs * self.average_price + units * fill.price) / new_abs
        self.position_units += Decimal(self.direction) * units
        self.entry_units += units
        self.entry_notional += units * fill.price
        self.fees += fee
        self.add_leg(fill, role="entry", units=units, fee=fee)

    def close(self, fill: FillRecord, units: Decimal, fee: Decimal) -> None:
        realized = (
            (fill.price - self.average_price) * units
            if self.direction > 0
            else (self.average_price - fill.price) * units
        )
        self.position_units -= Decimal(self.direction) * units
        self.exit_units += units
        self.exit_notional += units * fill.price
        self.gross_pnl += realized
        self.fees += fee
        self.recorded_realized += fill.recorded_realized_pnl_usd
        self.add_leg(fill, role="exit", units=units, fee=fee, realized=realized)
        if self.position_units == ZERO:
            self.average_price = ZERO
            self.closed_at = fill.timestamp
            self.closing_fill_id = fill.fill_id

    def freeze(self) -> TradeRecord:
        closed = self.closed_at is not None
        entry_average = self.entry_notional / self.entry_units
        exit_average = self.exit_notional / self.exit_units if self.exit_units else None
        return TradeRecord(
            trade_id=self.trade_id,
            portfolio_id=self.portfolio_id,
            symbol=self.symbol,
            side=self.side,
            status="closed" if closed else "open",
            opened_at=self.opened_at,
            closed_at=self.closed_at,
            entry_units=self.entry_units,
            exit_units=self.exit_units,
            open_units=abs(self.position_units),
            entry_average_price=entry_average,
            exit_average_price=exit_average,
            current_average_price=self.average_price if self.position_units else None,
            entry_notional_usd=self.entry_notional,
            exit_notional_usd=self.exit_notional,
            gross_pnl_usd=self.gross_pnl,
            fees_usd=self.fees,
            net_pnl_usd=self.gross_pnl - self.fees,
            recorded_realized_pnl_usd=self.recorded_realized,
            realized_reconciliation_delta_usd=self.gross_pnl - self.recorded_realized,
            duration_seconds=(
                int((self.closed_at - self.opened_at).total_seconds())
                if self.closed_at is not None
                else None
            ),
            opening_fill_id=self.opening_fill_id,
            closing_fill_id=self.closing_fill_id,
            fills=tuple(self.fills),
        )


def reconstruct_trades(rows: Iterable[Mapping[str, Any]]) -> list[TradeRecord]:
    """Reconstruct contiguous position episodes from immutable shadow fills.

    The ledger uses average-cost accounting. A fill that crosses through zero closes
    one trade and opens the next; its fee is split exactly in proportion to units.
    """

    fills = sorted(
        (FillRecord.from_row(row) for row in rows),
        key=lambda item: (item.timestamp, item.fill_id),
    )
    active: dict[tuple[str, str], _TradeBuilder] = {}
    completed: list[_TradeBuilder] = []
    for fill in fills:
        key = (fill.portfolio_id, fill.symbol)
        builder = active.get(key)
        direction = 1 if fill.side == "buy" else -1
        remaining = fill.units
        if builder is not None and builder.direction != direction:
            closing_units = min(abs(builder.position_units), remaining)
            closing_fee = fill.fee_usd * closing_units / fill.units
            builder.close(fill, closing_units, closing_fee)
            remaining -= closing_units
            if builder.position_units == ZERO:
                completed.append(builder)
                del active[key]
                builder = None
        if remaining:
            opening_fee = fill.fee_usd * remaining / fill.units
            if builder is None:
                side = "long" if direction > 0 else "short"
                builder = _TradeBuilder(
                    trade_id=_trade_id(fill.portfolio_id, fill.symbol, fill.fill_id, side),
                    portfolio_id=fill.portfolio_id,
                    symbol=fill.symbol,
                    direction=direction,
                    opened_at=fill.timestamp,
                    opening_fill_id=fill.fill_id,
                )
                active[key] = builder
            builder.open(fill, remaining, opening_fee)

    records = [builder.freeze() for builder in completed]
    records.extend(builder.freeze() for builder in active.values())
    return sorted(records, key=lambda item: (item.opened_at, item.opening_fill_id))


class TradeRegistry:
    """Read-only trade projection over an existing SQLite connection."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def trades(self, *, portfolio_ids: Sequence[str] | None = None) -> list[TradeRecord]:
        tables = {
            str(row[0])
            for row in self.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "shadow_fills" not in tables:
            return []
        if portfolio_ids is not None:
            normalized = tuple(dict.fromkeys(str(item) for item in portfolio_ids if item))
            if not normalized:
                return []
            include_all = 0
            encoded_portfolios = json.dumps(normalized, separators=(",", ":"))
        else:
            include_all = 1
            encoded_portfolios = "[]"
        parameters = (include_all, encoded_portfolios)
        if "shadow_fill_quarantine" in tables:
            rows = self.connection.execute(
                "SELECT f.id,f.ts,f.portfolio_id,f.symbol,f.side,f.units,f.price,"
                "f.fee_usd,f.realized_pnl_usd FROM shadow_fills AS f "
                "WHERE (?=1 OR f.portfolio_id IN (SELECT value FROM json_each(?))) "
                "AND NOT EXISTS ("
                "SELECT 1 FROM shadow_fill_quarantine AS q WHERE q.fill_id=f.id"
                ") ORDER BY f.ts,f.id",
                parameters,
            ).fetchall()
            return reconstruct_trades(rows)
        rows = self.connection.execute(
            "SELECT f.id,f.ts,f.portfolio_id,f.symbol,f.side,f.units,f.price,"
            "f.fee_usd,f.realized_pnl_usd FROM shadow_fills AS f "
            "WHERE (?=1 OR f.portfolio_id IN (SELECT value FROM json_each(?))) "
            "ORDER BY f.ts,f.id",
            parameters,
        ).fetchall()
        return reconstruct_trades(rows)
