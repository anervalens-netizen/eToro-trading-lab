from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Mapping
from zoneinfo import ZoneInfo

from .audit import AuditLog
from .mcp import EtoroMCPClient


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


SHADOW_PORTFOLIO_IDS: tuple[str, ...] = tuple(f"strategy_{index:02d}" for index in range(1, 13))
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


class ShadowPortfolioLedger:
    """Twelve isolated, exact-decimal shadow ledgers stored in the audit database."""

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
        now = datetime.now(timezone.utc).isoformat()
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
                timestamp.astimezone(timezone.utc).isoformat(),
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
        timestamp = executed_at or datetime.now(timezone.utc)
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
                timestamp.astimezone(timezone.utc).isoformat(),
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
        timestamp = accrued_at or datetime.now(timezone.utc)
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
        timestamp = as_of or datetime.now(timezone.utc)
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
        recorded_at = timestamp.astimezone(timezone.utc).isoformat()
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
                "SELECT COUNT(*) FROM shadow_fills WHERE portfolio_id=? AND ts>=? AND ts<?",
                (
                    portfolio_id,
                    local_start.astimezone(timezone.utc).isoformat(),
                    local_end.astimezone(timezone.utc).isoformat(),
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
        return tuple(self.snapshot(portfolio_id, marks, as_of=as_of) for portfolio_id in self.portfolio_ids)


def _daily_and_peak(audit: AuditLog, prefix: str, equity: Decimal, unrealized: Decimal) -> tuple[Decimal, Decimal]:
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
        self.audit.db.execute("UPDATE paper_positions SET last_price=? WHERE symbol=?", (str(mark), symbol))
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
        self.audit.append("paper_portfolio_snapshot", {"equity_usd": equity, "gross_exposure_usd": gross, "unrealized_pnl_usd": unrealized})
        return PortfolioState(equity, peak, daily, gross, symbol_exposure, self.audit.count_today(("paper_fill",)))


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
            exposure = abs(Decimal(str(pnl.get("exposureInAccountCurrency", position.get("amount", 0)))))
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
        self.audit.append("demo_portfolio_snapshot", {"equity_usd": equity, "gross_exposure_usd": gross, "unrealized_pnl_usd": unrealized})
        return PortfolioState(equity, peak, daily, gross, symbol_exposure, self.audit.count_today(("etoro_demo_execution",)))
