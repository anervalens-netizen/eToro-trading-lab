from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from .audit import AuditLog
from .mcp import EtoroMCPClient
from .strategy_catalog import SHADOW_PORTFOLIO_IDS


@dataclass(frozen=True)
class PortfolioState:
    equity_usd: Decimal
    peak_equity_usd: Decimal
    daily_pnl_usd: Decimal
    gross_exposure_usd: Decimal
    symbol_exposure_usd: Decimal
    trades_today: int
    realized_pnl_usd: Decimal = Decimal("0")
    unrealized_pnl_usd: Decimal = Decimal("0")
    fees_usd: Decimal = Decimal("0")
    financing_usd: Decimal = Decimal("0")


MASTER_PORTFOLIO_ID = "master_1000"


@dataclass(frozen=True)
class ShadowPortfolioState:
    portfolio_id: str
    initial_cash_usd: Decimal
    cash_usd: Decimal
    equity_usd: Decimal
    peak_equity_usd: Decimal
    daily_pnl_usd: Decimal
    realized_pnl_usd: Decimal
    unrealized_pnl_usd: Decimal
    fees_usd: Decimal
    financing_usd: Decimal
    gross_exposure_usd: Decimal
    trades_today: int


@dataclass(frozen=True)
class BrokerOpenProjection:
    position_id: int
    instrument_id: int
    is_buy: bool
    units: Decimal
    open_rate: Decimal
    opened_at: datetime
    initial_amount_usd: Decimal
    fees_usd: Decimal
    evidence_json: str
    evidence_hash: str


class ShadowPortfolioLedger:
    """Isolated, exact-decimal shadow ledgers stored in the audit database."""

    def __init__(
        self,
        audit: AuditLog,
        *,
        initial_cash_usd: Decimal = Decimal("1000"),
        portfolio_ids: tuple[str, ...] = SHADOW_PORTFOLIO_IDS,
        reporting_timezone: str = "Europe/Bucharest",
    ) -> None:
        if initial_cash_usd <= 0:
            raise ValueError("initial shadow cash must be positive")
        if not portfolio_ids or len(set(portfolio_ids)) != len(portfolio_ids):
            raise ValueError("portfolio identifiers must be non-empty and unique")
        self.audit = audit
        self.portfolio_ids = portfolio_ids
        self.reporting_timezone = ZoneInfo(reporting_timezone)
        self._create_schema()
        now = datetime.now(UTC).isoformat()
        for portfolio_id in portfolio_ids:
            self.audit.db.execute(
                """
                INSERT OR IGNORE INTO shadow_portfolios(
                    portfolio_id,initial_cash_usd,cash_usd,realized_pnl_usd,
                    fees_usd,financing_usd,peak_equity_usd,created_at
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    portfolio_id,
                    str(initial_cash_usd),
                    str(initial_cash_usd),
                    "0",
                    "0",
                    "0",
                    str(initial_cash_usd),
                    now,
                ),
            )
            local_day = datetime.now(self.reporting_timezone).date().isoformat()
            cash_row = self.audit.db.execute(
                "SELECT cash_usd FROM shadow_portfolios WHERE portfolio_id=?", (portfolio_id,)
            ).fetchone()
            opening_equity = Decimal(cash_row[0])
            for units, last_price in self.audit.db.execute(
                "SELECT units,last_price FROM shadow_positions WHERE portfolio_id=?",
                (portfolio_id,),
            ):
                opening_equity += Decimal(units) * Decimal(last_price)
            self.audit.db.execute(
                """
                INSERT OR IGNORE INTO shadow_daily_pnl(
                    portfolio_id,day,opening_equity_usd,realized_pnl_usd,
                    unrealized_pnl_usd,fees_usd,financing_usd,daily_pnl_usd,
                    equity_usd,recorded_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    portfolio_id,
                    local_day,
                    str(opening_equity),
                    "0",
                    "0",
                    "0",
                    "0",
                    "0",
                    str(opening_equity),
                    now,
                ),
            )
        self.audit.db.commit()

    def _create_schema(self) -> None:
        self.audit.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS shadow_portfolios (
                portfolio_id TEXT PRIMARY KEY,
                initial_cash_usd TEXT NOT NULL,
                cash_usd TEXT NOT NULL,
                realized_pnl_usd TEXT NOT NULL,
                fees_usd TEXT NOT NULL,
                financing_usd TEXT NOT NULL,
                peak_equity_usd TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS shadow_positions (
                portfolio_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                units TEXT NOT NULL,
                average_price TEXT NOT NULL,
                last_price TEXT NOT NULL,
                PRIMARY KEY(portfolio_id,symbol),
                FOREIGN KEY(portfolio_id) REFERENCES shadow_portfolios(portfolio_id)
            );
            CREATE TABLE IF NOT EXISTS shadow_fills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                portfolio_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                units TEXT NOT NULL,
                price TEXT NOT NULL,
                fee_usd TEXT NOT NULL,
                realized_pnl_usd TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS shadow_daily_pnl (
                portfolio_id TEXT NOT NULL,
                day TEXT NOT NULL,
                opening_equity_usd TEXT NOT NULL,
                realized_pnl_usd TEXT NOT NULL,
                unrealized_pnl_usd TEXT NOT NULL,
                fees_usd TEXT NOT NULL,
                financing_usd TEXT NOT NULL,
                daily_pnl_usd TEXT NOT NULL,
                equity_usd TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                PRIMARY KEY(portfolio_id,day)
            );
            CREATE TABLE IF NOT EXISTS shadow_broker_close_reconciliations (
                portfolio_id TEXT NOT NULL,
                broker_position_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                instrument_id INTEGER NOT NULL,
                local_projection_json TEXT NOT NULL,
                broker_trade_json TEXT NOT NULL,
                broker_evidence_hash TEXT NOT NULL,
                reconciled_at TEXT NOT NULL,
                PRIMARY KEY(portfolio_id,broker_position_id)
            );
            CREATE TABLE IF NOT EXISTS shadow_fill_quarantine (
                fill_id INTEGER PRIMARY KEY,
                portfolio_id TEXT NOT NULL,
                reason TEXT NOT NULL,
                original_fill_json TEXT NOT NULL,
                broker_evidence_hash TEXT NOT NULL,
                quarantined_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS shadow_broker_open_reconciliations (
                portfolio_id TEXT NOT NULL,
                broker_position_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                instrument_id INTEGER NOT NULL,
                local_projection_json TEXT,
                broker_position_json TEXT NOT NULL,
                broker_evidence_hash TEXT NOT NULL,
                reconciled_at TEXT NOT NULL,
                PRIMARY KEY(portfolio_id,broker_position_id)
            );
            """
        )
        self.audit.db.commit()

    def _require_portfolio(self, portfolio_id: str) -> None:
        if portfolio_id not in self.portfolio_ids:
            raise ValueError(f"unknown shadow portfolio: {portfolio_id}")

    def _current_equity(self, portfolio_id: str) -> Decimal:
        row = self.audit.db.execute(
            "SELECT cash_usd FROM shadow_portfolios WHERE portfolio_id=?", (portfolio_id,)
        ).fetchone()
        equity = Decimal(row[0])
        for units, last_price in self.audit.db.execute(
            "SELECT units,last_price FROM shadow_positions WHERE portfolio_id=?",
            (portfolio_id,),
        ):
            equity += Decimal(units) * Decimal(last_price)
        return equity

    def _ensure_daily_opening(self, portfolio_id: str, timestamp: datetime) -> None:
        local_day = timestamp.astimezone(self.reporting_timezone).date().isoformat()
        opening = self._current_equity(portfolio_id)
        self.audit.db.execute(
            """
            INSERT OR IGNORE INTO shadow_daily_pnl(
                portfolio_id,day,opening_equity_usd,realized_pnl_usd,
                unrealized_pnl_usd,fees_usd,financing_usd,daily_pnl_usd,
                equity_usd,recorded_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                portfolio_id,
                local_day,
                str(opening),
                "0",
                "0",
                "0",
                "0",
                "0",
                str(opening),
                timestamp.astimezone(UTC).isoformat(),
            ),
        )

    def record_fill(
        self,
        portfolio_id: str,
        symbol: str,
        side: str,
        units: Decimal,
        price: Decimal,
        *,
        fee_usd: Decimal = Decimal("0"),
        executed_at: datetime | None = None,
    ) -> Decimal:
        self._require_portfolio(portfolio_id)
        normalized_side = side.lower()
        if normalized_side not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")
        if units <= 0 or price <= 0 or fee_usd < 0:
            raise ValueError("units/price must be positive and fee non-negative")
        timestamp = executed_at or datetime.now(UTC)
        if timestamp.tzinfo is None:
            raise ValueError("fill timestamp must be timezone-aware")
        self._ensure_daily_opening(portfolio_id, timestamp)
        symbol = symbol.upper()
        signed_fill = units if normalized_side == "buy" else -units

        portfolio = self.audit.db.execute(
            "SELECT cash_usd,realized_pnl_usd,fees_usd FROM shadow_portfolios WHERE portfolio_id=?",
            (portfolio_id,),
        ).fetchone()
        if portfolio is None:
            raise ValueError(f"unknown shadow portfolio: {portfolio_id}")
        cash, cumulative_realized, cumulative_fees = map(Decimal, portfolio)
        position = self.audit.db.execute(
            "SELECT units,average_price FROM shadow_positions WHERE portfolio_id=? AND symbol=?",
            (portfolio_id, symbol),
        ).fetchone()
        old_units = Decimal(position[0]) if position else Decimal("0")
        old_average = Decimal(position[1]) if position else Decimal("0")

        realized = Decimal("0")
        if old_units and old_units * signed_fill < 0:
            closing_units = min(abs(old_units), abs(signed_fill))
            realized = (
                (price - old_average) * closing_units
                if old_units > 0
                else (old_average - price) * closing_units
            )

        new_units = old_units + signed_fill
        if new_units == 0:
            new_average = Decimal("0")
        elif old_units == 0 or old_units * signed_fill > 0:
            new_average = (abs(old_units) * old_average + units * price) / abs(new_units)
        elif old_units * new_units < 0:
            new_average = price
        else:
            new_average = old_average

        new_cash = cash - signed_fill * price - fee_usd
        self.audit.db.execute(
            """
            UPDATE shadow_portfolios
            SET cash_usd=?,realized_pnl_usd=?,fees_usd=? WHERE portfolio_id=?
            """,
            (
                str(new_cash),
                str(cumulative_realized + realized),
                str(cumulative_fees + fee_usd),
                portfolio_id,
            ),
        )
        if new_units == 0:
            self.audit.db.execute(
                "DELETE FROM shadow_positions WHERE portfolio_id=? AND symbol=?",
                (portfolio_id, symbol),
            )
        else:
            self.audit.db.execute(
                """
                INSERT INTO shadow_positions(portfolio_id,symbol,units,average_price,last_price)
                VALUES(?,?,?,?,?)
                ON CONFLICT(portfolio_id,symbol) DO UPDATE SET
                    units=excluded.units,average_price=excluded.average_price,last_price=excluded.last_price
                """,
                (portfolio_id, symbol, str(new_units), str(new_average), str(price)),
            )
        self.audit.db.execute(
            """
            INSERT INTO shadow_fills(ts,portfolio_id,symbol,side,units,price,fee_usd,realized_pnl_usd)
            VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                timestamp.astimezone(UTC).isoformat(),
                portfolio_id,
                symbol,
                normalized_side,
                str(units),
                str(price),
                str(fee_usd),
                str(realized),
            ),
        )
        self.audit.db.commit()
        self.audit.append(
            "shadow_fill",
            {
                "portfolio_id": portfolio_id,
                "symbol": symbol,
                "side": normalized_side,
                "units": units,
                "price": price,
                "fee_usd": fee_usd,
                "realized_pnl_usd": realized,
            },
        )
        return realized

    @staticmethod
    def _broker_decimal(value: object, name: str, *, positive: bool = False) -> Decimal:
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"invalid broker {name}") from exc
        if not parsed.is_finite() or (positive and parsed <= 0):
            raise ValueError(f"invalid broker {name}")
        return parsed

    @staticmethod
    def _broker_timestamp(value: object, name: str) -> datetime:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"invalid broker {name}") from exc
        if parsed.tzinfo is None:
            raise ValueError(f"invalid broker {name}")
        return parsed.astimezone(UTC)

    @staticmethod
    def _reported_tolerance(value: Decimal) -> Decimal:
        return max(Decimal("0.00000001"), Decimal(1).scaleb(value.as_tuple().exponent) / 2)

    def _broker_open_projection(
        self, instrument_id: int, broker_position: Mapping[str, object]
    ) -> BrokerOpenProjection:
        required = {
            "positionID",
            "instrumentID",
            "isBuy",
            "units",
            "openRate",
            "openDateTime",
            "initialAmountInDollars",
            "totalFees",
        }
        if not required <= set(broker_position):
            raise ValueError("broker open position is incomplete")
        try:
            position_id = int(broker_position["positionID"])
            reported_instrument_id = int(broker_position["instrumentID"])
            leverage = int(broker_position.get("leverage", 1))
        except (TypeError, ValueError) as exc:
            raise ValueError("broker open identity is invalid") from exc
        if (
            position_id <= 0
            or instrument_id <= 0
            or reported_instrument_id != instrument_id
            or leverage != 1
        ):
            raise ValueError("broker open identity does not match the local instrument")
        if not isinstance(broker_position["isBuy"], bool):
            raise ValueError("broker open direction is invalid")
        units = self._broker_decimal(broker_position["units"], "units", positive=True)
        initial_units = self._broker_decimal(
            broker_position.get("initialUnits", units), "initial units", positive=True
        )
        if abs(units - initial_units) > self._reported_tolerance(initial_units):
            raise ValueError("partial broker position cannot initialize the local projection")
        open_rate = self._broker_decimal(
            broker_position["openRate"], "open rate", positive=True
        )
        initial_amount = self._broker_decimal(
            broker_position["initialAmountInDollars"],
            "initial amount",
            positive=True,
        )
        if abs(units * open_rate - initial_amount) > max(
            Decimal("0.02"), self._reported_tolerance(initial_amount)
        ):
            raise ValueError("broker open amount is inconsistent with units and rate")
        fees = self._broker_decimal(broker_position["totalFees"], "fees")
        if fees < 0:
            raise ValueError("broker fees cannot be negative")
        opened_at = self._broker_timestamp(
            broker_position["openDateTime"], "open timestamp"
        )
        evidence = {
            key: broker_position[key]
            for key in (
                "positionID",
                "orderID",
                "instrumentID",
                "isBuy",
                "units",
                "initialUnits",
                "amount",
                "initialAmountInDollars",
                "openRate",
                "openDateTime",
                "leverage",
                "stopLossRate",
                "takeProfitRate",
                "totalFees",
                "totalExternalFees",
                "totalExternalTaxes",
            )
            if key in broker_position
        }
        evidence_json = json.dumps(
            evidence, sort_keys=True, separators=(",", ":"), default=str
        )
        return BrokerOpenProjection(
            position_id=position_id,
            instrument_id=reported_instrument_id,
            is_buy=bool(broker_position["isBuy"]),
            units=units,
            open_rate=open_rate,
            opened_at=opened_at,
            initial_amount_usd=initial_amount,
            fees_usd=fees,
            evidence_json=evidence_json,
            evidence_hash=hashlib.sha256(evidence_json.encode()).hexdigest(),
        )

    def validate_broker_open(
        self,
        portfolio_id: str,
        symbol: str,
        instrument_id: int,
        broker_position: Mapping[str, object],
    ) -> None:
        self._require_portfolio(portfolio_id)
        projection = self._broker_open_projection(instrument_id, broker_position)
        position = self.audit.db.execute(
            """
            SELECT units,average_price FROM shadow_positions
            WHERE portfolio_id=? AND symbol=?
            """,
            (portfolio_id, symbol.upper()),
        ).fetchone()
        if position is None:
            raise ValueError("local broker-backed position is absent")
        local_units, local_average = map(Decimal, position)
        if (local_units > 0) != projection.is_buy:
            raise ValueError("broker open direction does not match the local position")
        if abs(abs(local_units) - projection.units) > self._reported_tolerance(
            projection.units
        ):
            raise ValueError("broker open units do not match the local position")
        if abs(local_average - projection.open_rate) > self._reported_tolerance(
            projection.open_rate
        ):
            raise ValueError("broker open rate does not match the local position")

    def reconcile_broker_open(
        self,
        portfolio_id: str,
        symbol: str,
        instrument_id: int,
        broker_position: Mapping[str, object],
        *,
        replace_local_projection: bool = False,
    ) -> bool:
        """Project current DEMO broker position truth, optionally quarantining one bad fill."""

        self._require_portfolio(portfolio_id)
        symbol = symbol.upper()
        projection = self._broker_open_projection(instrument_id, broker_position)
        reconciled_at = datetime.now(UTC).isoformat()
        with self.audit.write_transaction():
            existing = self.audit.db.execute(
                """
                SELECT broker_evidence_hash FROM shadow_broker_open_reconciliations
                WHERE portfolio_id=? AND broker_position_id=?
                """,
                (portfolio_id, projection.position_id),
            ).fetchone()
            if existing is not None:
                if str(existing[0]) != projection.evidence_hash:
                    raise ValueError("broker open identity was rebound to different evidence")
                return False
            portfolio = self.audit.db.execute(
                """
                SELECT cash_usd,realized_pnl_usd,fees_usd FROM shadow_portfolios
                WHERE portfolio_id=?
                """,
                (portfolio_id,),
            ).fetchone()
            if portfolio is None:
                raise ValueError(f"unknown shadow portfolio: {portfolio_id}")
            cash, cumulative_realized, cumulative_fees = map(Decimal, portfolio)
            local_position = self.audit.db.execute(
                """
                SELECT units,average_price,last_price FROM shadow_positions
                WHERE portfolio_id=? AND symbol=?
                """,
                (portfolio_id, symbol),
            ).fetchone()
            local_projection_json: str | None = None
            if local_position is not None:
                if not replace_local_projection:
                    raise ValueError("local master position already exists")
                local_units, local_average, local_last = map(Decimal, local_position)
                active_fills = self.audit.db.execute(
                    """
                    SELECT f.id,f.ts,f.side,f.units,f.price,f.fee_usd,
                           f.realized_pnl_usd
                    FROM shadow_fills AS f
                    LEFT JOIN shadow_fill_quarantine AS q ON q.fill_id=f.id
                    WHERE f.portfolio_id=? AND f.symbol=? AND q.fill_id IS NULL
                    ORDER BY f.id DESC LIMIT 1
                    """,
                    (portfolio_id, symbol),
                ).fetchall()
                if len(active_fills) != 1:
                    raise ValueError("local projection repair requires exactly one active fill")
                fill = active_fills[0]
                fill_units = Decimal(fill[3])
                fill_price = Decimal(fill[4])
                fill_fee = Decimal(fill[5])
                fill_realized = Decimal(fill[6])
                if (
                    fill_realized != 0
                    or fill_units != abs(local_units)
                    or abs(fill_price - local_average)
                    > self._reported_tolerance(fill_price)
                    or (str(fill[2]) == "buy") != (local_units > 0)
                ):
                    raise ValueError("local projection is not a replaceable opening fill")
                original_fill = {
                    "id": int(fill[0]),
                    "ts": str(fill[1]),
                    "portfolio_id": portfolio_id,
                    "symbol": symbol,
                    "side": str(fill[2]),
                    "units": str(fill_units),
                    "price": str(fill_price),
                    "fee_usd": str(fill_fee),
                    "realized_pnl_usd": str(fill_realized),
                }
                local_projection = {
                    "position": {
                        "units": str(local_units),
                        "average_price": str(local_average),
                        "last_price": str(local_last),
                    },
                    "fill": original_fill,
                    "cash_usd": str(cash),
                    "realized_pnl_usd": str(cumulative_realized),
                    "fees_usd": str(cumulative_fees),
                }
                local_projection_json = json.dumps(
                    local_projection, sort_keys=True, separators=(",", ":")
                )
                basis_reversal = (
                    fill_units * fill_price
                    if local_units > 0
                    else -fill_units * fill_price
                )
                restored_cash = cash + basis_reversal + fill_fee
                cumulative_fees -= fill_fee
                if cumulative_fees < 0:
                    raise ValueError("local projection fee reversal is invalid")
                self.audit.db.execute(
                    """
                    INSERT INTO shadow_fill_quarantine(
                        fill_id,portfolio_id,reason,original_fill_json,
                        broker_evidence_hash,quarantined_at
                    ) VALUES(?,?,?,?,?,?)
                    """,
                    (
                        int(fill[0]),
                        portfolio_id,
                        "local ACK projection used a later quote instead of broker open truth",
                        json.dumps(original_fill, sort_keys=True, separators=(",", ":")),
                        projection.evidence_hash,
                        reconciled_at,
                    ),
                )
                self.audit.db.execute(
                    "DELETE FROM shadow_positions WHERE portfolio_id=? AND symbol=?",
                    (portfolio_id, symbol),
                )
                cash = restored_cash
            elif replace_local_projection:
                raise ValueError("local projection selected for repair is absent")

            signed_units = projection.units if projection.is_buy else -projection.units
            cash_after = cash - signed_units * projection.open_rate - projection.fees_usd
            self._ensure_daily_opening(portfolio_id, projection.opened_at)
            self.audit.db.execute(
                """
                UPDATE shadow_portfolios SET cash_usd=?,fees_usd=? WHERE portfolio_id=?
                """,
                (
                    str(cash_after),
                    str(cumulative_fees + projection.fees_usd),
                    portfolio_id,
                ),
            )
            self.audit.db.execute(
                """
                INSERT INTO shadow_positions(
                    portfolio_id,symbol,units,average_price,last_price
                ) VALUES(?,?,?,?,?)
                """,
                (
                    portfolio_id,
                    symbol,
                    str(signed_units),
                    str(projection.open_rate),
                    str(projection.open_rate),
                ),
            )
            self.audit.db.execute(
                """
                INSERT INTO shadow_fills(
                    ts,portfolio_id,symbol,side,units,price,fee_usd,realized_pnl_usd
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    projection.opened_at.isoformat(),
                    portfolio_id,
                    symbol,
                    "buy" if projection.is_buy else "sell",
                    str(projection.units),
                    str(projection.open_rate),
                    str(projection.fees_usd),
                    "0",
                ),
            )
            self.audit.db.execute(
                """
                INSERT INTO shadow_broker_open_reconciliations(
                    portfolio_id,broker_position_id,symbol,instrument_id,
                    local_projection_json,broker_position_json,
                    broker_evidence_hash,reconciled_at
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    portfolio_id,
                    projection.position_id,
                    symbol,
                    instrument_id,
                    local_projection_json,
                    projection.evidence_json,
                    projection.evidence_hash,
                    reconciled_at,
                ),
            )
            if portfolio_id == MASTER_PORTFOLIO_ID:
                self.audit.db.execute(
                    """
                    INSERT INTO state(key,value) VALUES('master_broker_position_id',?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value
                    """,
                    (str(projection.position_id),),
                )
                self.audit.db.execute(
                    """
                    INSERT INTO state(key,value) VALUES('master_reconciliation_drift','')
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value
                    """
                )
            self.audit.append_tx(
                "master_broker_open_reconciled",
                {
                    "portfolio_id": portfolio_id,
                    "symbol": symbol,
                    "instrument_id": instrument_id,
                    "broker_position_id": projection.position_id,
                    "broker_evidence_hash": projection.evidence_hash,
                    "broker_opened_at": projection.opened_at.isoformat(),
                    "broker_open_rate": projection.open_rate,
                    "broker_units": projection.units,
                    "broker_initial_amount_usd": projection.initial_amount_usd,
                    "broker_fees_usd": projection.fees_usd,
                    "replaced_local_projection": replace_local_projection,
                    "cash_after_usd": cash_after,
                    "real_money": False,
                    "network_write_attempted": False,
                },
            )
        return True

    def reconcile_broker_close(
        self,
        portfolio_id: str,
        symbol: str,
        instrument_id: int,
        broker_trade: Mapping[str, object],
        *,
        clear_pending_execution: bool = False,
    ) -> bool:
        """Project one exact, read-only DEMO history close into the master ledger.

        eToro history rates/units are rounded for display, so cash and cumulative
        P&L use the broker's authoritative ``netProfit`` while the immutable fill
        retains the reported close rate. The reconciliation delta remains visible
        through the trade registry instead of being silently invented away.
        """

        self._require_portfolio(portfolio_id)
        symbol = symbol.upper()
        required = {
            "positionId",
            "instrumentId",
            "isBuy",
            "openRate",
            "openTimestamp",
            "closeRate",
            "closeTimestamp",
            "netProfit",
            "fees",
            "units",
            "initialInvestment",
        }
        if not required <= set(broker_trade):
            raise ValueError("broker close history is incomplete")
        try:
            position_id = int(broker_trade["positionId"])
            reported_instrument_id = int(broker_trade["instrumentId"])
        except (TypeError, ValueError) as exc:
            raise ValueError("broker close identity is invalid") from exc
        if position_id <= 0 or instrument_id <= 0 or reported_instrument_id != instrument_id:
            raise ValueError("broker close identity does not match the local instrument")
        if not isinstance(broker_trade["isBuy"], bool):
            raise ValueError("broker close direction is invalid")

        open_rate = self._broker_decimal(broker_trade["openRate"], "open rate", positive=True)
        close_rate = self._broker_decimal(broker_trade["closeRate"], "close rate", positive=True)
        broker_units = self._broker_decimal(broker_trade["units"], "units", positive=True)
        initial_investment = self._broker_decimal(
            broker_trade["initialInvestment"], "initial investment", positive=True
        )
        net_profit = self._broker_decimal(broker_trade["netProfit"], "net profit")
        fees = self._broker_decimal(broker_trade["fees"], "fees")
        if fees < 0:
            raise ValueError("broker fees cannot be negative")
        opened_at = self._broker_timestamp(broker_trade["openTimestamp"], "open timestamp")
        closed_at = self._broker_timestamp(broker_trade["closeTimestamp"], "close timestamp")
        if closed_at < opened_at:
            raise ValueError("broker close precedes the broker open")

        evidence = {
            key: broker_trade[key]
            for key in (
                "positionId",
                "instrumentId",
                "isBuy",
                "openRate",
                "openTimestamp",
                "closeRate",
                "closeTimestamp",
                "netProfit",
                "fees",
                "units",
                "initialInvestment",
                "investment",
                "orderId",
                "parentPositionId",
                "stopLossRate",
                "takeProfitRate",
            )
            if key in broker_trade
        }
        evidence_json = json.dumps(
            evidence, sort_keys=True, separators=(",", ":"), default=str
        )
        evidence_hash = hashlib.sha256(evidence_json.encode()).hexdigest()
        reconciled_at = datetime.now(UTC).isoformat()

        with self.audit.write_transaction():
            existing = self.audit.db.execute(
                """
                SELECT broker_evidence_hash FROM shadow_broker_close_reconciliations
                WHERE portfolio_id=? AND broker_position_id=?
                """,
                (portfolio_id, position_id),
            ).fetchone()
            if existing is not None:
                if str(existing[0]) != evidence_hash:
                    raise ValueError("broker close identity was rebound to different evidence")
                return False

            portfolio = self.audit.db.execute(
                """
                SELECT initial_cash_usd,cash_usd,realized_pnl_usd,fees_usd,
                       financing_usd,peak_equity_usd
                FROM shadow_portfolios WHERE portfolio_id=?
                """,
                (portfolio_id,),
            ).fetchone()
            position = self.audit.db.execute(
                """
                SELECT units,average_price,last_price FROM shadow_positions
                WHERE portfolio_id=? AND symbol=?
                """,
                (portfolio_id, symbol),
            ).fetchone()
            if portfolio is None or position is None:
                raise ValueError("local broker-backed position is absent")
            (
                initial_cash,
                cash,
                cumulative_realized,
                cumulative_fees,
                financing,
                peak_before,
            ) = map(Decimal, portfolio)
            local_units, local_average, local_last = map(Decimal, position)
            if (local_units > 0) != bool(broker_trade["isBuy"]):
                raise ValueError("broker close direction does not match the local position")
            if abs(abs(local_units) - broker_units) > self._reported_tolerance(broker_units):
                raise ValueError("broker close units do not match the local position")
            if abs(local_average - open_rate) > self._reported_tolerance(open_rate):
                raise ValueError("broker open rate does not match the local position")
            basis = abs(local_units) * local_average
            if abs(basis - initial_investment) > max(
                Decimal("0.02"), self._reported_tolerance(initial_investment)
            ):
                raise ValueError("broker investment does not match the local position basis")

            local_projection = {
                "portfolio_id": portfolio_id,
                "symbol": symbol,
                "units": str(local_units),
                "average_price": str(local_average),
                "last_price": str(local_last),
                "cash_usd": str(cash),
                "realized_pnl_usd": str(cumulative_realized),
                "fees_usd": str(cumulative_fees),
                "financing_usd": str(financing),
            }
            gross_realized = net_profit + fees
            cash_after = cash + (basis if local_units > 0 else -basis) + net_profit
            legitimate_peak = max(initial_cash, cash_after)
            for _event_ts, payload_json in self.audit.db.execute(
                """
                SELECT ts,payload FROM events
                WHERE event_type='shadow_portfolio_snapshot' AND ts<=?
                ORDER BY id
                """,
                (closed_at.isoformat(),),
            ):
                payload = json.loads(str(payload_json))
                if payload.get("portfolio_id") != portfolio_id:
                    continue
                try:
                    equity = Decimal(str(payload["equity_usd"]))
                except (InvalidOperation, KeyError, ValueError):
                    continue
                if equity.is_finite():
                    legitimate_peak = max(legitimate_peak, equity)
            self._ensure_daily_opening(portfolio_id, closed_at)
            self.audit.db.execute(
                """
                UPDATE shadow_portfolios
                SET cash_usd=?,realized_pnl_usd=?,fees_usd=?,peak_equity_usd=?
                WHERE portfolio_id=?
                """,
                (
                    str(cash_after),
                    str(cumulative_realized + gross_realized),
                    str(cumulative_fees + fees),
                    str(legitimate_peak),
                    portfolio_id,
                ),
            )
            self.audit.db.execute(
                "DELETE FROM shadow_positions WHERE portfolio_id=? AND symbol=?",
                (portfolio_id, symbol),
            )
            self.audit.db.execute(
                """
                INSERT INTO shadow_fills(
                    ts,portfolio_id,symbol,side,units,price,fee_usd,realized_pnl_usd
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    closed_at.isoformat(),
                    portfolio_id,
                    symbol,
                    "sell" if local_units > 0 else "buy",
                    str(abs(local_units)),
                    str(close_rate),
                    str(fees),
                    str(gross_realized),
                ),
            )
            self.audit.db.execute(
                """
                INSERT INTO shadow_broker_close_reconciliations(
                    portfolio_id,broker_position_id,symbol,instrument_id,
                    local_projection_json,broker_trade_json,broker_evidence_hash,
                    reconciled_at
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    portfolio_id,
                    position_id,
                    symbol,
                    instrument_id,
                    json.dumps(local_projection, sort_keys=True, separators=(",", ":")),
                    evidence_json,
                    evidence_hash,
                    reconciled_at,
                ),
            )
            if portfolio_id == MASTER_PORTFOLIO_ID:
                keys = ["master_broker_position_id", "master_reconciliation_drift"]
                if clear_pending_execution:
                    keys.append("master_pending_execution")
                self.audit.db.executemany(
                    """
                    INSERT INTO state(key,value) VALUES(?, '')
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value
                    """,
                    ((key,) for key in keys),
                )
            self.audit.append_tx(
                "master_broker_close_reconciled",
                {
                    "portfolio_id": portfolio_id,
                    "symbol": symbol,
                    "instrument_id": instrument_id,
                    "broker_position_id": position_id,
                    "broker_evidence_hash": evidence_hash,
                    "broker_opened_at": opened_at.isoformat(),
                    "broker_closed_at": closed_at.isoformat(),
                    "broker_open_rate": open_rate,
                    "broker_close_rate": close_rate,
                    "broker_net_profit_usd": net_profit,
                    "broker_fees_usd": fees,
                    "cash_before_usd": cash,
                    "cash_after_usd": cash_after,
                    "peak_before_usd": peak_before,
                    "peak_after_usd": legitimate_peak,
                    "local_units": abs(local_units),
                    "reported_units": broker_units,
                    "reported_rate_pnl_usd": abs(local_units) * (
                        close_rate - local_average
                        if local_units > 0
                        else local_average - close_rate
                    ),
                    "recorded_gross_realized_pnl_usd": gross_realized,
                    "real_money": False,
                    "network_write_attempted": False,
                },
            )
        return True

    def accrue_financing(
        self,
        portfolio_id: str,
        amount_usd: Decimal,
        *,
        accrued_at: datetime | None = None,
    ) -> None:
        self._require_portfolio(portfolio_id)
        if amount_usd < 0:
            raise ValueError("financing cost must be non-negative")
        timestamp = accrued_at or datetime.now(UTC)
        if timestamp.tzinfo is None:
            raise ValueError("financing timestamp must be timezone-aware")
        self._ensure_daily_opening(portfolio_id, timestamp)
        row = self.audit.db.execute(
            "SELECT cash_usd,financing_usd FROM shadow_portfolios WHERE portfolio_id=?",
            (portfolio_id,),
        ).fetchone()
        cash, financing = map(Decimal, row)
        self.audit.db.execute(
            "UPDATE shadow_portfolios SET cash_usd=?,financing_usd=? WHERE portfolio_id=?",
            (str(cash - amount_usd), str(financing + amount_usd), portfolio_id),
        )
        self.audit.db.commit()
        self.audit.append(
            "shadow_financing", {"portfolio_id": portfolio_id, "amount_usd": amount_usd}
        )

    def snapshot(
        self,
        portfolio_id: str,
        marks: Mapping[str, Decimal] | None = None,
        *,
        as_of: datetime | None = None,
    ) -> ShadowPortfolioState:
        self._require_portfolio(portfolio_id)
        timestamp = as_of or datetime.now(UTC)
        if timestamp.tzinfo is None:
            raise ValueError("snapshot timestamp must be timezone-aware")
        marks = {symbol.upper(): mark for symbol, mark in (marks or {}).items()}
        if any(mark <= 0 for mark in marks.values()):
            raise ValueError("position marks must be positive")

        portfolio = self.audit.db.execute(
            """
            SELECT initial_cash_usd,cash_usd,realized_pnl_usd,fees_usd,
                   financing_usd,peak_equity_usd
            FROM shadow_portfolios WHERE portfolio_id=?
            """,
            (portfolio_id,),
        ).fetchone()
        initial, cash, realized, fees, financing, peak = map(Decimal, portfolio)
        market_value = Decimal("0")
        gross = Decimal("0")
        unrealized = Decimal("0")
        for symbol, units_value, average_value, last_value in self.audit.db.execute(
            """
            SELECT symbol,units,average_price,last_price
            FROM shadow_positions WHERE portfolio_id=?
            """,
            (portfolio_id,),
        ):
            units = Decimal(units_value)
            average = Decimal(average_value)
            mark = marks.get(symbol, Decimal(last_value))
            if symbol in marks:
                self.audit.db.execute(
                    "UPDATE shadow_positions SET last_price=? WHERE portfolio_id=? AND symbol=?",
                    (str(mark), portfolio_id, symbol),
                )
            market_value += units * mark
            gross += abs(units * mark)
            unrealized += units * (mark - average)
        equity = cash + market_value
        peak = max(peak, equity)
        self.audit.db.execute(
            "UPDATE shadow_portfolios SET peak_equity_usd=? WHERE portfolio_id=?",
            (str(peak), portfolio_id),
        )

        local_day = timestamp.astimezone(self.reporting_timezone).date().isoformat()
        daily = self.audit.db.execute(
            "SELECT opening_equity_usd FROM shadow_daily_pnl WHERE portfolio_id=? AND day=?",
            (portfolio_id, local_day),
        ).fetchone()
        opening = Decimal(daily[0]) if daily else equity
        daily_pnl = equity - opening
        recorded_at = timestamp.astimezone(UTC).isoformat()
        self.audit.db.execute(
            """
            INSERT INTO shadow_daily_pnl(
                portfolio_id,day,opening_equity_usd,realized_pnl_usd,unrealized_pnl_usd,
                fees_usd,financing_usd,daily_pnl_usd,equity_usd,recorded_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(portfolio_id,day) DO UPDATE SET
                realized_pnl_usd=excluded.realized_pnl_usd,
                unrealized_pnl_usd=excluded.unrealized_pnl_usd,
                fees_usd=excluded.fees_usd,
                financing_usd=excluded.financing_usd,
                daily_pnl_usd=excluded.daily_pnl_usd,
                equity_usd=excluded.equity_usd,
                recorded_at=excluded.recorded_at
            """,
            (
                portfolio_id,
                local_day,
                str(opening),
                str(realized),
                str(unrealized),
                str(fees),
                str(financing),
                str(daily_pnl),
                str(equity),
                recorded_at,
            ),
        )
        self.audit.db.commit()
        local_start = datetime.combine(
            timestamp.astimezone(self.reporting_timezone).date(),
            datetime.min.time(),
            tzinfo=self.reporting_timezone,
        )
        local_end = local_start + timedelta(days=1)
        trades_today = int(
            self.audit.db.execute(
                """
                SELECT COUNT(*) FROM shadow_fills AS f
                WHERE f.portfolio_id=? AND f.ts>=? AND f.ts<?
                  AND NOT EXISTS(
                      SELECT 1 FROM shadow_fill_quarantine AS q WHERE q.fill_id=f.id
                  )
                """,
                (
                    portfolio_id,
                    local_start.astimezone(UTC).isoformat(),
                    local_end.astimezone(UTC).isoformat(),
                ),
            ).fetchone()[0]
        )
        state = ShadowPortfolioState(
            portfolio_id=portfolio_id,
            initial_cash_usd=initial,
            cash_usd=cash,
            equity_usd=equity,
            peak_equity_usd=peak,
            daily_pnl_usd=daily_pnl,
            realized_pnl_usd=realized,
            unrealized_pnl_usd=unrealized,
            fees_usd=fees,
            financing_usd=financing,
            gross_exposure_usd=gross,
            trades_today=trades_today,
        )
        self.audit.append("shadow_portfolio_snapshot", state.__dict__)
        return state

    def snapshot_all(
        self,
        marks: Mapping[str, Decimal] | None = None,
        *,
        as_of: datetime | None = None,
    ) -> tuple[ShadowPortfolioState, ...]:
        return tuple(
            self.snapshot(portfolio_id, marks, as_of=as_of) for portfolio_id in self.portfolio_ids
        )


def _daily_and_peak(
    audit: AuditLog, prefix: str, equity: Decimal, unrealized: Decimal
) -> tuple[Decimal, Decimal]:
    day = __import__("datetime").date.today().isoformat()
    baseline_key = f"{prefix}_opening_equity_{day}"
    opening = Decimal(audit.state_get(baseline_key, str(equity)))
    audit.state_set(baseline_key, str(opening))
    daily = equity - opening
    peak = max(equity, Decimal(audit.state_get(f"{prefix}_peak_equity", str(equity))))
    audit.state_set(f"{prefix}_peak_equity", str(peak))
    audit.record_daily_pnl(day, str(daily - unrealized), str(unrealized), str(equity))
    return daily, peak


class PaperPortfolioMonitor:
    def __init__(self, audit: AuditLog, initial_cash: Decimal) -> None:
        self.audit = audit
        if self.audit.state_get("paper_initialized", "0") != "1":
            self.audit.state_set("paper_cash_usd", str(initial_cash))
            self.audit.state_set("paper_initialized", "1")

    def snapshot(self, symbol: str, mark: Decimal) -> PortfolioState:
        self.audit.db.execute(
            "UPDATE paper_positions SET last_price=? WHERE symbol=?", (str(mark), symbol)
        )
        self.audit.db.commit()
        cash = Decimal(self.audit.state_get("paper_cash_usd", "0"))
        gross = Decimal("0")
        unrealized = Decimal("0")
        symbol_exposure = Decimal("0")
        for row_symbol, units, average, last in self.audit.db.execute(
            "SELECT symbol,units,average_price,last_price FROM paper_positions"
        ):
            exposure = abs(Decimal(units) * Decimal(last))
            gross += exposure
            unrealized += Decimal(units) * (Decimal(last) - Decimal(average))
            if row_symbol == symbol:
                symbol_exposure = exposure
        equity = cash + gross
        daily, peak = _daily_and_peak(self.audit, "paper", equity, unrealized)
        self.audit.append(
            "paper_portfolio_snapshot",
            {"equity_usd": equity, "gross_exposure_usd": gross, "unrealized_pnl_usd": unrealized},
        )
        return PortfolioState(
            equity, peak, daily, gross, symbol_exposure, self.audit.count_today(("paper_fill",))
        )


class DemoPortfolioMonitor:
    def __init__(self, client: EtoroMCPClient, audit: AuditLog) -> None:
        self.client = client
        self.audit = audit

    def snapshot(self, instrument_id: int) -> PortfolioState:
        result = self.client.execute_read("/api/v1/trading/info/demo/pnl")
        if not result.is_success or not isinstance(result.body, dict):
            raise RuntimeError("failed to synchronize DEMO portfolio/P&L")
        portfolio = result.body.get("clientPortfolio", {})
        credit = Decimal(str(portfolio.get("credit", 0)))
        gross = Decimal("0")
        unrealized = Decimal("0")
        symbol_exposure = Decimal("0")
        invested = Decimal("0")
        for position in portfolio.get("positions", []):
            pnl = position.get("unrealizedPnL") or {}
            exposure = abs(
                Decimal(str(pnl.get("exposureInAccountCurrency", position.get("amount", 0))))
            )
            position_pnl = Decimal(str(pnl.get("pnL", 0)))
            amount = Decimal(str(position.get("amount", 0)))
            gross += exposure
            invested += amount
            unrealized += position_pnl
            if int(position.get("instrumentID", -1)) == instrument_id:
                symbol_exposure += exposure
        equity = credit + invested + unrealized
        if equity <= 0:
            raise ValueError("DEMO portfolio returned invalid equity")
        daily, peak = _daily_and_peak(self.audit, "demo", equity, unrealized)
        self.audit.append(
            "demo_portfolio_snapshot",
            {"equity_usd": equity, "gross_exposure_usd": gross, "unrealized_pnl_usd": unrealized},
        )
        return PortfolioState(
            equity,
            peak,
            daily,
            gross,
            symbol_exposure,
            self.audit.count_today(("etoro_demo_execution",)),
        )
