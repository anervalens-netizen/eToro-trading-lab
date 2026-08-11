from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import closing, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .audit import AuditLog
from .models import KillState
from .portfolio import MASTER_PORTFOLIO_ID
from .strategy_catalog import (
    STRATEGY_COUNT,
    STRATEGY_DEFINITIONS,
    STRATEGY_PORTFOLIO_BY_ID,
)
from .trade_registry import TradeRecord, TradeRegistry

try:  # FastAPI stays an optional runtime dependency.
    from fastapi import FastAPI, Request
    from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
    from fastapi.staticfiles import StaticFiles
except ImportError:  # pragma: no cover - the pure-Python service is the default test surface.
    FastAPI = None  # type: ignore[assignment,misc]
    Request = Any  # type: ignore[assignment,misc]
    FileResponse = JSONResponse = StreamingResponse = StaticFiles = None  # type: ignore[assignment]


SCHEMA_VERSION = 1
DEFAULT_STRATEGIES: tuple[dict[str, str], ...] = STRATEGY_DEFINITIONS

_SENSITIVE_KEY_PARTS = (
    "authorization",
    "api_key",
    "apikey",
    "user_key",
    "userkey",
    "bearer",
    "cookie",
    "credential",
    "password",
    "secret",
    "token",
)
_ORDER_EVENT_TYPES = frozenset(
    {
        "risk_approval",
        "operator_approval",
        "standing_demo_authorization",
        "demo_pretrade_validation",
        "etoro_demo_execution",
        "etoro_demo_execution_unknown",
        "paper_fill",
        "risk_rejection",
        "order_submitted",
        "order_acknowledged",
        "order_filled",
        "order_rejected",
        "order_cancelled",
        "order_unknown",
        "order_reconciled",
    }
)
_ORDER_STATUS = {
    "risk_approval": "awaiting_owner",
    "operator_approval": "approved",
    "standing_demo_authorization": "approved",
    "demo_pretrade_validation": "validated",
    "etoro_demo_execution": "submitted",
    "etoro_demo_execution_unknown": "unknown",
    "paper_fill": "filled",
    "risk_rejection": "rejected",
    "order_submitted": "submitted",
    "order_acknowledged": "acknowledged",
    "order_filled": "filled",
    "order_rejected": "rejected",
    "order_cancelled": "cancelled",
    "order_unknown": "unknown",
    "order_reconciled": "reconciled",
}


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _safe_json_loads(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return value


def _is_sensitive_key(key: object) -> bool:
    normalized = "".join(character for character in str(key).strip().lower() if character.isalnum())
    if normalized in {
        "inputtokens",
        "outputtokens",
        "reasoningtokens",
        "cachereadtokens",
        "cachewritetokens",
        "exacttokenruns",
        "tokencoveragefraction",
    }:
        return False
    sensitive = tuple(
        "".join(character for character in part if character.isalnum())
        for part in _SENSITIVE_KEY_PARTS
    ) + ("pwd",)
    return any(part in normalized for part in sensitive)


def sanitize(value: Any, *, _depth: int = 0) -> Any:
    """Bound and redact untrusted audit payloads before they reach the browser."""

    if _depth >= 8:
        return "[MAX_DEPTH]"
    if isinstance(value, Mapping):
        cleaned: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 100:
                cleaned["_truncated"] = True
                break
            key_text = str(key)[:120]
            cleaned[key_text] = (
                "[REDACTED]" if _is_sensitive_key(key) else sanitize(item, _depth=_depth + 1)
            )
        return cleaned
    if isinstance(value, (list, tuple)):
        items = [sanitize(item, _depth=_depth + 1) for item in value[:100]]
        if len(value) > 100:
            items.append("[TRUNCATED]")
        return items
    if isinstance(value, str):
        return value if len(value) <= 2_000 else value[:2_000] + "…[TRUNCATED]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:2_000]


@dataclass(frozen=True)
class OwnerIdentityPolicy:
    """Trusts one Authentik-injected identity header and otherwise fails closed."""

    owner_username: str | None
    header_name: str = "x-authentik-username"

    @property
    def configured(self) -> bool:
        return bool(self.owner_username and self.owner_username.strip())

    def allows(self, headers: Mapping[str, str]) -> bool:
        if not self.configured:
            return False
        actual = headers.get(self.header_name) or headers.get(self.header_name.lower())
        return bool(actual) and hmac.compare_digest(actual.strip(), self.owner_username.strip())


class DashboardService:
    """Read-only projection over operational state; it never creates tables or writes state."""

    def __init__(
        self,
        audit_db_path: str | Path = "runtime/audit.sqlite3",
        runtime_dir: str | Path = "runtime",
        strategies: Sequence[Mapping[str, str]] = DEFAULT_STRATEGIES,
        *,
        shadow_capital_usd: str = "1000.00",
        activity_limit: int = 40,
        ai_budgets: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        self.audit_db_path = Path(audit_db_path)
        self.runtime_dir = Path(runtime_dir)
        self.strategies = tuple(dict(item) for item in strategies)
        if len(self.strategies) != STRATEGY_COUNT:
            raise ValueError(f"dashboard requires exactly {STRATEGY_COUNT} shadow strategies")
        if len({item.get("id") for item in self.strategies}) != STRATEGY_COUNT:
            raise ValueError("dashboard strategy identifiers must be unique")
        self.shadow_capital_usd = str(shadow_capital_usd)
        self.activity_limit = max(10, min(int(activity_limit), 200))
        self.ai_budgets = tuple(dict(item) for item in ai_budgets)

    @property
    def _strategy_by_id(self) -> dict[str, dict[str, str]]:
        return {str(item["id"]): dict(item) for item in self.strategies}

    @property
    def _strategy_by_portfolio(self) -> dict[str, str]:
        return {
            portfolio_id: strategy_id
            for strategy_id, portfolio_id in STRATEGY_PORTFOLIO_BY_ID.items()
        }

    def _connect(self) -> sqlite3.Connection:
        if not self.audit_db_path.is_file():
            raise FileNotFoundError("audit database is unavailable")
        connection = sqlite3.connect(
            f"file:{self.audit_db_path.resolve()}?mode=ro",
            uri=True,
            timeout=1,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        return connection

    @staticmethod
    def _tables(connection: sqlite3.Connection) -> set[str]:
        return {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }

    def _read_events(
        self, connection: sqlite3.Connection, tables: set[str]
    ) -> list[dict[str, Any]]:
        if "events" not in tables:
            return []
        rows = connection.execute(
            "SELECT id,ts,event_type,payload,event_hash FROM events ORDER BY id DESC LIMIT ?",
            (max(self.activity_limit, 200),),
        ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "ts": str(row["ts"]),
                "event_type": str(row["event_type"]),
                "payload": sanitize(_safe_json_loads(row["payload"])),
                "event_hash": str(row["event_hash"]),
            }
            for row in rows
        ]

    @staticmethod
    def _read_approvals(connection: sqlite3.Connection, tables: set[str]) -> list[dict[str, Any]]:
        if "approvals" not in tables:
            return []
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(approvals)")}
        query = (
            "SELECT * FROM approvals ORDER BY last_updated DESC, rowid DESC LIMIT 50"
            if "last_updated" in columns
            else "SELECT * FROM approvals ORDER BY rowid DESC LIMIT 50"
        )
        rows = connection.execute(query).fetchall()
        state_names = {
            "AWAITING_APPROVAL": "awaiting_owner",
            "APPROVED": "approved",
            "SENDING": "sending",
            "ACKNOWLEDGED": "acknowledged",
            "UNKNOWN": "unknown",
            "REJECTED": "rejected",
            "PARTIAL": "partial",
            "FILLED": "filled",
            "CANCELLED": "cancelled",
            "RECONCILED": "reconciled",
        }
        approvals: list[dict[str, Any]] = []
        for row in rows:
            row_keys = set(row.keys())
            raw_state = str(row["state"]) if "state" in row_keys and row["state"] else ""
            status = state_names.get(
                raw_state,
                "consumed"
                if row["consumed_at"]
                else "approved"
                if row["approved_at"]
                else "awaiting_owner",
            )
            approvals.append(
                {
                    "proposal_id": str(row["proposal_id"]),
                    "status": status,
                    "request": sanitize(_safe_json_loads(row["request_json"])),
                    "approved_at": row["approved_at"],
                    "consumed_at": row["consumed_at"],
                    "actor": row["actor"] if "actor" in row_keys else None,
                    "expires_at": row["expires_at"] if "expires_at" in row_keys else None,
                    "envelope_hash": row["envelope_hash"] if "envelope_hash" in row_keys else None,
                    "x_request_id": row["x_request_id"] if "x_request_id" in row_keys else None,
                    "response": sanitize(_safe_json_loads(row["response_json"]))
                    if "response_json" in row_keys
                    else None,
                    "read_only": True,
                }
            )
        return approvals

    @staticmethod
    def _read_pnl(connection: sqlite3.Connection, tables: set[str]) -> list[dict[str, str]]:
        if "shadow_daily_pnl" in tables:
            rows = connection.execute(
                "SELECT portfolio_id,day,realized_pnl_usd,unrealized_pnl_usd,fees_usd,financing_usd,"
                "daily_pnl_usd,equity_usd,recorded_at FROM shadow_daily_pnl "
                "WHERE portfolio_id!=? ORDER BY day DESC,portfolio_id LIMIT 500",
                (MASTER_PORTFOLIO_ID,),
            ).fetchall()
            by_day: dict[str, dict[str, Any]] = {}
            fields = {
                "realized_usd": "realized_pnl_usd",
                "unrealized_usd": "unrealized_pnl_usd",
                "fees_usd": "fees_usd",
                "financing_usd": "financing_usd",
                "daily_pnl_usd": "daily_pnl_usd",
                "equity_usd": "equity_usd",
            }
            for row in rows:
                day = str(row["day"])
                aggregate = by_day.setdefault(
                    day,
                    {
                        "day": day,
                        **{target: Decimal("0") for target in fields},
                        "recorded_at": str(row["recorded_at"]),
                        "portfolio_count": 0,
                    },
                )
                try:
                    parsed = {
                        target: Decimal(str(row[source])) for target, source in fields.items()
                    }
                except InvalidOperation:
                    continue
                for field, value in parsed.items():
                    aggregate[field] += value
                aggregate["portfolio_count"] += 1
                aggregate["recorded_at"] = max(aggregate["recorded_at"], str(row["recorded_at"]))
            if by_day:
                return [
                    {key: str(value) for key, value in by_day[day].items()}
                    for day in sorted(by_day)[-31:]
                ]
        if "pnl_daily_v2" in tables:
            rows = connection.execute(
                "SELECT portfolio_id,day,realized_usd,unrealized_usd,fees_usd,financing_usd,equity_usd,recorded_at "
                "FROM pnl_daily_v2 ORDER BY day DESC,portfolio_id LIMIT 500"
            ).fetchall()
            by_day: dict[str, dict[str, Any]] = {}
            for row in rows:
                day = str(row["day"])
                aggregate = by_day.setdefault(
                    day,
                    {
                        "day": day,
                        "realized_usd": Decimal("0"),
                        "unrealized_usd": Decimal("0"),
                        "fees_usd": Decimal("0"),
                        "financing_usd": Decimal("0"),
                        "equity_usd": Decimal("0"),
                        "recorded_at": str(row["recorded_at"]),
                        "portfolio_count": 0,
                    },
                )
                try:
                    parsed = {
                        field: Decimal(str(row[field]))
                        for field in (
                            "realized_usd",
                            "unrealized_usd",
                            "fees_usd",
                            "financing_usd",
                            "equity_usd",
                        )
                    }
                except InvalidOperation:
                    continue
                for field, value in parsed.items():
                    aggregate[field] += value
                aggregate["portfolio_count"] += 1
                aggregate["recorded_at"] = max(aggregate["recorded_at"], str(row["recorded_at"]))
            if by_day:
                return [
                    {key: str(value) for key, value in by_day[day].items()}
                    for day in sorted(by_day)[-31:]
                ]
        if "pnl_daily" not in tables:
            return []
        rows = connection.execute(
            "SELECT day,realized_usd,unrealized_usd,equity_usd,recorded_at "
            "FROM pnl_daily ORDER BY day DESC LIMIT 31"
        ).fetchall()
        return [
            {
                "day": str(row["day"]),
                "realized_usd": str(row["realized_usd"]),
                "unrealized_usd": str(row["unrealized_usd"]),
                "equity_usd": str(row["equity_usd"]),
                "recorded_at": str(row["recorded_at"]),
            }
            for row in reversed(rows)
        ]

    @staticmethod
    def _read_master(connection: sqlite3.Connection, tables: set[str]) -> dict[str, Any]:
        result: dict[str, Any] = {
            "portfolio_id": MASTER_PORTFOLIO_ID,
            "equity_usd": "1000.00",
            "daily_pnl_usd": "0.00",
            "position": None,
        }
        if "shadow_daily_pnl" in tables:
            row = connection.execute(
                """
                SELECT equity_usd,daily_pnl_usd,realized_pnl_usd,unrealized_pnl_usd,
                       fees_usd,financing_usd,recorded_at
                FROM shadow_daily_pnl WHERE portfolio_id=?
                ORDER BY day DESC LIMIT 1
                """,
                (MASTER_PORTFOLIO_ID,),
            ).fetchone()
            if row is not None:
                result.update({key: str(value) for key, value in dict(row).items()})
        if "shadow_positions" in tables:
            row = connection.execute(
                "SELECT symbol,units,average_price,last_price FROM shadow_positions WHERE portfolio_id=? LIMIT 1",
                (MASTER_PORTFOLIO_ID,),
            ).fetchone()
            if row is not None:
                result["position"] = {key: str(value) for key, value in dict(row).items()}
        return result

    @staticmethod
    def _read_ai(connection: sqlite3.Connection, tables: set[str]) -> dict[str, Any]:
        result: dict[str, Any] = {"enabled": True, "pending": 0, "decided": 0, "latest": None}
        if "ai_decision_packets" not in tables:
            result["enabled"] = False
            return result
        for state, count in connection.execute(
            "SELECT state,COUNT(*) FROM ai_decision_packets GROUP BY state"
        ):
            result[str(state).lower()] = int(count)
        row = connection.execute(
            """
            SELECT packet_id,packet_hash,state,decision_json,created_at,decided_at
            FROM ai_decision_packets ORDER BY created_at DESC LIMIT 1
            """
        ).fetchone()
        if row is not None:
            result["latest"] = {
                "packet_id": str(row["packet_id"]),
                "packet_hash": str(row["packet_hash"]),
                "state": str(row["state"]),
                "decision": sanitize(_safe_json_loads(row["decision_json"])),
                "created_at": str(row["created_at"]),
                "decided_at": row["decided_at"],
            }
        return result

    @staticmethod
    def _verify_chain(connection: sqlite3.Connection, tables: set[str]) -> bool:
        if "events" not in tables:
            return False
        previous = "0" * 64
        for row in connection.execute(
            "SELECT ts,event_type,payload,previous_hash,event_hash FROM events ORDER BY id"
        ):
            if str(row["previous_hash"]) != previous:
                return False
            payload = _safe_json_loads(row["payload"])
            body = json.dumps(
                {"ts": str(row["ts"]), "event_type": str(row["event_type"]), "payload": payload},
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
            expected = hashlib.sha256((previous + body).encode()).hexdigest()
            if not hmac.compare_digest(expected, str(row["event_hash"])):
                return False
            previous = str(row["event_hash"])
        return True

    @staticmethod
    def _read_state(connection: sqlite3.Connection, tables: set[str]) -> dict[str, str]:
        if "state" not in tables:
            return {}
        return {
            str(row["key"]): str(row["value"])
            for row in connection.execute("SELECT key,value FROM state")
        }

    @staticmethod
    def _read_heartbeats(connection: sqlite3.Connection, tables: set[str]) -> list[dict[str, Any]]:
        if "service_heartbeats" not in tables:
            return []
        return [
            {
                "name": f"service:{row['service']}",
                "status": str(row["status"]).lower(),
                "detail": sanitize(_safe_json_loads(row["details"])),
                "recorded_at": str(row["recorded_at"]),
            }
            for row in connection.execute(
                "SELECT service,status,details,recorded_at FROM service_heartbeats ORDER BY service"
            )
        ]

    @staticmethod
    def _read_market_events(
        connection: sqlite3.Connection, tables: set[str]
    ) -> list[dict[str, Any]]:
        if "commodity_news_events" not in tables:
            return []
        return [
            {
                "event_hash": str(row["event_hash"]),
                "publisher": str(row["publisher"]),
                "headline": str(row["headline"]),
                "url": str(row["url"]),
                "symbols": _safe_json_loads(row["symbols_json"]),
                "direction_hint": str(row["direction_hint"]),
                "observed_at": str(row["observed_at"]),
                "expires_at": str(row["expires_at"]),
                "research_only": True,
            }
            for row in connection.execute(
                """
                SELECT event_hash,publisher,headline,url,symbols_json,direction_hint,
                       observed_at,expires_at
                FROM commodity_news_events WHERE expires_at>?
                ORDER BY observed_at DESC LIMIT 20
                """,
                (_utc_now().isoformat(),),
            )
        ]

    def _strategy_cards(self, events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        strategy_by_portfolio = {
            portfolio_id: strategy_id
            for strategy_id, portfolio_id in STRATEGY_PORTFOLIO_BY_ID.items()
        }
        cards: dict[str, dict[str, Any]] = {
            item["id"]: {
                "id": item["id"],
                "name": item.get("name", item["id"]),
                "family": item.get("family", "strategy"),
                "symbol": item.get("symbol"),
                "risk_profile": item.get("risk_profile", "standard"),
                "hypothesis": item.get("hypothesis"),
                "status": "waiting_for_data",
                "nav_usd": self.shadow_capital_usd,
                "daily_pnl_usd": "0.00",
                "total_pnl_usd": "0.00",
                "drawdown_fraction": "0",
                "trades": 0,
                "rank": None,
                "top3": False,
                "last_signal": None,
                "research_epoch": None,
                "carried_position": False,
                "eligible_for_promotion": True,
                "updated_at": None,
            }
            for item in self.strategies
        }
        relevant = {
            "strategy_snapshot",
            "shadow_portfolio_snapshot",
            "strategy_status",
            "strategy_rank",
        }
        for event in reversed(events):
            if event.get("event_type") not in relevant:
                continue
            payload = event.get("payload")
            if not isinstance(payload, Mapping):
                continue
            strategy_id = str(payload.get("strategy_id") or "")
            if strategy_id not in cards:
                strategy_id = strategy_by_portfolio.get(str(payload.get("portfolio_id") or ""), "")
            if strategy_id not in cards:
                continue
            allowed = {
                "status",
                "nav_usd",
                "daily_pnl_usd",
                "total_pnl_usd",
                "drawdown_fraction",
                "trades",
                "rank",
                "last_signal",
                "research_epoch",
                "carried_position",
                "eligible_for_promotion",
            }
            cards[strategy_id].update(
                {key: sanitize(payload[key]) for key in allowed if key in payload}
            )
            if "equity_usd" in payload:
                cards[strategy_id]["nav_usd"] = sanitize(payload["equity_usd"])
            if "trades_today" in payload:
                cards[strategy_id]["trades"] = sanitize(payload["trades_today"])
            if "initial_cash_usd" in payload and "equity_usd" in payload:
                with suppress(InvalidOperation):
                    cards[strategy_id]["total_pnl_usd"] = str(
                        Decimal(str(payload["equity_usd"]))
                        - Decimal(str(payload["initial_cash_usd"]))
                    )
            if "peak_equity_usd" in payload and "equity_usd" in payload:
                try:
                    peak = Decimal(str(payload["peak_equity_usd"]))
                    equity = Decimal(str(payload["equity_usd"]))
                    cards[strategy_id]["drawdown_fraction"] = (
                        str(max(Decimal("0"), (peak - equity) / peak)) if peak > 0 else "0"
                    )
                except InvalidOperation:
                    pass
            cards[strategy_id]["updated_at"] = event.get("ts")
        for card in cards.values():
            try:
                card["top3"] = 1 <= int(card["rank"]) <= 3
            except (TypeError, ValueError):
                card["top3"] = False
        return list(cards.values())

    @staticmethod
    def _trade_metrics(trades: Sequence[TradeRecord]) -> dict[str, Any]:
        closed = [trade for trade in trades if trade.status == "closed"]
        open_trades = [trade for trade in trades if trade.status == "open"]
        wins = [trade.net_pnl_usd for trade in closed if trade.net_pnl_usd > 0]
        losses = [trade.net_pnl_usd for trade in closed if trade.net_pnl_usd < 0]
        gross_pnl = sum((trade.gross_pnl_usd for trade in closed), Decimal("0"))
        fees = sum((trade.fees_usd for trade in closed), Decimal("0"))
        net_pnl = sum((trade.net_pnl_usd for trade in closed), Decimal("0"))
        positive = sum(wins, Decimal("0"))
        negative = abs(sum(losses, Decimal("0")))
        durations = [
            trade.duration_seconds for trade in closed if trade.duration_seconds is not None
        ]
        return {
            "trades": len(trades),
            "closed_trades": len(closed),
            "open_trades": len(open_trades),
            "wins": len(wins),
            "losses": len(losses),
            "breakeven": len(closed) - len(wins) - len(losses),
            "win_rate": str(Decimal(len(wins)) / Decimal(len(closed))) if closed else None,
            "gross_pnl_usd": str(gross_pnl),
            "fees_usd": str(fees),
            "net_pnl_usd": str(net_pnl),
            "expectancy_usd": str(net_pnl / Decimal(len(closed))) if closed else None,
            "average_win_usd": str(positive / Decimal(len(wins))) if wins else None,
            "average_loss_usd": str(-negative / Decimal(len(losses))) if losses else None,
            "profit_factor": str(positive / negative) if negative else None,
            "average_duration_seconds": (sum(durations) // len(durations) if durations else None),
            "entry_notional_usd": str(
                sum((trade.entry_notional_usd for trade in trades), Decimal("0"))
            ),
            "realized_reconciliation_delta_usd": str(
                sum(
                    (trade.realized_reconciliation_delta_usd for trade in trades),
                    Decimal("0"),
                )
            ),
        }

    def _require_strategy(self, strategy_id: str) -> tuple[dict[str, str], str]:
        definition = self._strategy_by_id.get(strategy_id)
        portfolio_id = STRATEGY_PORTFOLIO_BY_ID.get(strategy_id)
        if definition is None or portfolio_id is None:
            raise KeyError(f"unknown strategy: {strategy_id}")
        return definition, portfolio_id

    @staticmethod
    def _filter_timestamp(value: str | None, field_name: str) -> datetime | None:
        if not value:
            return None
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            timestamp = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"invalid {field_name}") from exc
        if timestamp.tzinfo is None:
            raise ValueError(f"{field_name} must include a timezone")
        return timestamp.astimezone(UTC)

    def _all_trades(self, portfolio_ids: Sequence[str] | None = None) -> list[TradeRecord]:
        try:
            with closing(self._connect()) as connection:
                return TradeRegistry(connection).trades(portfolio_ids=portfolio_ids)
        except (OSError, sqlite3.Error):
            return []

    def _trade_projection(
        self, trade: TradeRecord, *, include_fills: bool = False
    ) -> dict[str, Any]:
        item = trade.to_dict(include_fills=include_fills)
        item["strategy_id"] = self._strategy_by_portfolio.get(trade.portfolio_id, "sol_master")
        item["pricing_quality"] = (
            "BROKER_RECONCILED_MARK_ESTIMATE"
            if trade.portfolio_id == MASTER_PORTFOLIO_ID
            else "PAPER_SIMULATED_NEXT_QUOTE"
        )
        return item

    def list_trades(
        self,
        *,
        strategy_id: str | None = None,
        symbol: str | None = None,
        status: str | None = None,
        side: str | None = None,
        from_ts: str | None = None,
        to_ts: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        portfolio_ids: tuple[str, ...] | None = None
        if strategy_id is not None:
            _, portfolio_id = self._require_strategy(strategy_id)
            portfolio_ids = (portfolio_id,)
        normalized_status = status.strip().lower() if status else None
        normalized_side = side.strip().lower() if side else None
        if normalized_status not in {None, "open", "closed"}:
            raise ValueError("status must be open or closed")
        if normalized_side not in {None, "long", "short"}:
            raise ValueError("side must be long or short")
        if offset < 0:
            raise ValueError("offset cannot be negative")
        limit = max(1, min(int(limit), 100))
        start = self._filter_timestamp(from_ts, "from_ts")
        end = self._filter_timestamp(to_ts, "to_ts")
        if start and end and start > end:
            raise ValueError("from_ts cannot be after to_ts")
        normalized_symbol = symbol.strip().upper() if symbol else None
        trades = self._all_trades(portfolio_ids)
        filtered = [
            trade
            for trade in trades
            if (normalized_symbol is None or trade.symbol == normalized_symbol)
            and (normalized_status is None or trade.status == normalized_status)
            and (normalized_side is None or trade.side == normalized_side)
            and (start is None or trade.opened_at >= start)
            and (end is None or trade.opened_at <= end)
        ]
        filtered.sort(
            key=lambda trade: (trade.closed_at or trade.opened_at, trade.opening_fill_id),
            reverse=True,
        )
        items = []
        for trade in filtered[offset : offset + limit]:
            items.append(self._trade_projection(trade))
        return sanitize(
            {
                "items": items,
                "total": len(filtered),
                "limit": limit,
                "offset": offset,
                "read_only": True,
                "real_money": False,
            }
        )

    def trade_detail(self, trade_id: str) -> dict[str, Any]:
        if not trade_id or len(trade_id) > 80:
            raise KeyError("unknown trade")
        trade = next(
            (item for item in self._all_trades() if item.trade_id == trade_id),
            None,
        )
        if trade is None:
            raise KeyError(f"unknown trade: {trade_id}")
        item = self._trade_projection(trade, include_fills=True)
        reviews = self.list_reviews(trade_id=trade_id, limit=1).get("items", [])
        if reviews:
            item["review"] = reviews[0].get("review")
            item["review_id"] = reviews[0].get("review_id")
        return sanitize({"trade": item, "read_only": True, "real_money": False})

    def list_strategies(self) -> dict[str, Any]:
        cards = {item["id"]: item for item in self.snapshot()["strategies"]}
        trades = self._all_trades(tuple(self._strategy_by_portfolio))
        by_portfolio: dict[str, list[TradeRecord]] = {
            portfolio_id: [] for portfolio_id in self._strategy_by_portfolio
        }
        for trade in trades:
            by_portfolio.setdefault(trade.portfolio_id, []).append(trade)
        items: list[dict[str, Any]] = []
        for definition in self.strategies:
            strategy_id = str(definition["id"])
            portfolio_id = STRATEGY_PORTFOLIO_BY_ID[strategy_id]
            items.append(
                {
                    **cards[strategy_id],
                    "portfolio_id": portfolio_id,
                    "metrics": self._trade_metrics(by_portfolio.get(portfolio_id, [])),
                }
            )
        return sanitize(
            {
                "items": items,
                "total": len(items),
                "read_only": True,
                "real_money": False,
            }
        )

    def strategy_detail(self, strategy_id: str) -> dict[str, Any]:
        definition, portfolio_id = self._require_strategy(strategy_id)
        strategy_list = self.list_strategies()
        card = next(item for item in strategy_list["items"] if item["id"] == strategy_id)
        trades = self._all_trades((portfolio_id,))
        equity_curve: list[dict[str, str]] = []
        positions: list[dict[str, Any]] = []
        try:
            with closing(self._connect()) as connection:
                tables = self._tables(connection)
                if "shadow_daily_pnl" in tables:
                    rows = connection.execute(
                        """
                        SELECT day,equity_usd,daily_pnl_usd,realized_pnl_usd,
                               unrealized_pnl_usd,fees_usd,financing_usd,recorded_at
                        FROM shadow_daily_pnl WHERE portfolio_id=? ORDER BY day
                        """,
                        (portfolio_id,),
                    ).fetchall()
                    equity_curve = [
                        {key: str(value) for key, value in dict(row).items()} for row in rows
                    ]
                if "shadow_positions" in tables:
                    rows = connection.execute(
                        """
                        SELECT symbol,units,average_price,last_price
                        FROM shadow_positions WHERE portfolio_id=? ORDER BY symbol
                        """,
                        (portfolio_id,),
                    ).fetchall()
                    positions = [
                        {key: str(value) for key, value in dict(row).items()} for row in rows
                    ]
        except (OSError, sqlite3.Error):
            pass
        recent = sorted(
            trades,
            key=lambda trade: (trade.closed_at or trade.opened_at, trade.opening_fill_id),
            reverse=True,
        )[:20]
        return sanitize(
            {
                "strategy": {
                    **definition,
                    "portfolio_id": portfolio_id,
                    "card": card,
                    "metrics": self._trade_metrics(trades),
                    "positions": positions,
                    "equity_curve": equity_curve,
                    "recent_trades": [self._trade_projection(trade) for trade in recent],
                },
                "read_only": True,
                "real_money": False,
            }
        )

    def list_reviews(
        self,
        *,
        strategy_id: str | None = None,
        trade_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        if strategy_id is not None:
            self._require_strategy(strategy_id)
        if offset < 0:
            raise ValueError("offset cannot be negative")
        limit = max(1, min(int(limit), 100))
        items: list[dict[str, Any]] = []
        proposals: list[dict[str, Any]] = []
        total = 0
        try:
            with closing(self._connect()) as connection:
                tables = self._tables(connection)
                if "trade_ai_reviews" in tables:
                    parameters: list[Any] = [strategy_id, strategy_id, trade_id, trade_id]
                    total = int(
                        connection.execute(
                            """SELECT COUNT(*) FROM trade_ai_reviews
                               WHERE (? IS NULL OR strategy_id=?)
                                 AND (? IS NULL OR trade_id=?)""",
                            parameters,
                        ).fetchone()[0]
                    )
                    rows = connection.execute(
                        "SELECT review_id,trade_id,strategy_id,provider,model,prompt_version,"
                        "prompt_hash,packet_hash,review_hash,review_json,llm_run_id,created_at "
                        "FROM trade_ai_reviews WHERE (? IS NULL OR strategy_id=?) "
                        "AND (? IS NULL OR trade_id=?) ORDER BY created_at DESC,review_id DESC "
                        "LIMIT ? OFFSET ?",
                        [*parameters, limit, offset],
                    ).fetchall()
                    items = [
                        {
                            "review_id": str(row["review_id"]),
                            "trade_id": str(row["trade_id"]),
                            "strategy_id": str(row["strategy_id"]),
                            "provider": str(row["provider"]),
                            "model": str(row["model"]),
                            "prompt_version": str(row["prompt_version"]),
                            "prompt_hash": str(row["prompt_hash"]),
                            "packet_hash": str(row["packet_hash"]),
                            "review_hash": str(row["review_hash"]),
                            "review": _safe_json_loads(row["review_json"]),
                            "llm_run_id": str(row["llm_run_id"]),
                            "created_at": str(row["created_at"]),
                        }
                        for row in rows
                    ]
                if "strategy_change_proposals" in tables:
                    rows = connection.execute(
                        "SELECT proposal_id,proposal_hash,source_day,strategy_id,state,model,"
                        "aggregate_hash,proposal_json,llm_run_id,created_at "
                        "FROM strategy_change_proposals WHERE (? IS NULL OR strategy_id=?) "
                        "ORDER BY created_at DESC,proposal_id DESC LIMIT 50",
                        (strategy_id, strategy_id),
                    ).fetchall()
                    proposals = [
                        {
                            "proposal_id": str(row["proposal_id"]),
                            "proposal_hash": str(row["proposal_hash"]),
                            "source_day": str(row["source_day"]),
                            "strategy_id": str(row["strategy_id"]),
                            "state": str(row["state"]),
                            "model": str(row["model"]),
                            "aggregate_hash": str(row["aggregate_hash"]),
                            "proposal": _safe_json_loads(row["proposal_json"]),
                            "llm_run_id": row["llm_run_id"],
                            "created_at": str(row["created_at"]),
                        }
                        for row in rows
                    ]
        except (OSError, sqlite3.Error):
            pass
        return sanitize(
            {
                "items": items,
                "proposals": proposals,
                "total": total,
                "limit": limit,
                "offset": offset,
                "read_only": True,
                "authority": "RESEARCH_ONLY",
                "real_money": False,
            }
        )

    def ai_usage(self, *, days: int = 30) -> dict[str, Any]:
        days = max(1, min(int(days), 90))
        runs: list[dict[str, Any]] = []
        try:
            with closing(self._connect()) as connection:
                if "llm_runs" in self._tables(connection):
                    rows = connection.execute(
                        """
                        SELECT run_id,purpose,provider,model,status,input_tokens,output_tokens,
                               reasoning_tokens,cache_read_tokens,cache_write_tokens,cost_usd,
                               latency_ms,error_type,started_at,completed_at
                        FROM llm_runs ORDER BY started_at DESC,run_id DESC
                        """
                    ).fetchall()
                    runs = [dict(row) for row in rows]
        except (OSError, sqlite3.Error):
            pass

        by_day: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        token_fields = (
            "input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
        )
        total_cost = Decimal("0")
        completed = 0
        errors = 0
        exact_token_runs = 0
        for run in runs:
            day = str(run["started_at"])[:10]
            key = (day, str(run["provider"]), str(run["model"]), str(run["purpose"]))
            aggregate = by_day.setdefault(
                key,
                {
                    "day": day,
                    "provider": str(run["provider"]),
                    "model": str(run["model"]),
                    "purpose": str(run["purpose"]),
                    "runs": 0,
                    "completed": 0,
                    "errors": 0,
                    **{field: 0 for field in token_fields},
                    "cost_usd": Decimal("0"),
                    "latency_ms": 0,
                },
            )
            aggregate["runs"] += 1
            aggregate["latency_ms"] += int(run["latency_ms"] or 0)
            status = str(run["status"])
            if status == "COMPLETED":
                completed += 1
                aggregate["completed"] += 1
            else:
                errors += 1
                aggregate["errors"] += 1
            if run["input_tokens"] is not None and run["output_tokens"] is not None:
                exact_token_runs += 1
            for field in token_fields:
                aggregate[field] += int(run[field] or 0)
            if run["cost_usd"] is not None:
                try:
                    cost = Decimal(str(run["cost_usd"]))
                except InvalidOperation:
                    cost = Decimal("0")
                if cost.is_finite():
                    aggregate["cost_usd"] += cost
                    total_cost += cost
        daily = []
        selected_days = sorted({key[0] for key in by_day}, reverse=True)[:days]
        for key in sorted(by_day, reverse=True):
            if key[0] not in selected_days:
                continue
            aggregate = by_day[key]
            daily.append(
                {
                    **aggregate,
                    "cost_usd": str(aggregate["cost_usd"]),
                    "average_latency_ms": (
                        aggregate["latency_ms"] // aggregate["runs"] if aggregate["runs"] else None
                    ),
                }
            )
        recent = [
            {
                "run_id": str(run["run_id"]),
                "purpose": str(run["purpose"]),
                "provider": str(run["provider"]),
                "model": str(run["model"]),
                "status": str(run["status"]),
                **{field: run[field] for field in token_fields},
                "cost_usd": run["cost_usd"],
                "latency_ms": int(run["latency_ms"]),
                "error_type": run["error_type"],
                "started_at": str(run["started_at"]),
                "completed_at": str(run["completed_at"]),
            }
            for run in runs[:50]
        ]
        return sanitize(
            {
                "summary": {
                    "runs": len(runs),
                    "completed": completed,
                    "errors": errors,
                    "exact_token_runs": exact_token_runs,
                    "token_coverage_fraction": (
                        str(Decimal(exact_token_runs) / Decimal(len(runs))) if runs else None
                    ),
                    "cost_usd": str(total_cost),
                },
                "daily": daily,
                "recent": recent,
                "budgets": self.ai_budgets,
                "read_only": True,
                "real_money": False,
            }
        )

    @staticmethod
    def _orders(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        orders: dict[str, dict[str, Any]] = {}
        for event in reversed(events):
            event_type = str(event.get("event_type", ""))
            if event_type not in _ORDER_EVENT_TYPES:
                continue
            payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
            proposal_id = str(payload.get("proposal_id") or f"event-{event.get('id')}")
            entry = orders.setdefault(
                proposal_id,
                {
                    "proposal_id": proposal_id,
                    "strategy_id": payload.get("strategy_id"),
                    "symbol": payload.get("symbol"),
                    "status": "proposed",
                    "updated_at": event.get("ts"),
                    "lifecycle": [],
                },
            )
            request = payload.get("request")
            if isinstance(request, Mapping):
                body = request.get("body") if isinstance(request.get("body"), Mapping) else {}
                entry["symbol"] = entry.get("symbol") or body.get("symbol")
                entry["route"] = request.get("path")
            payload_state = str(payload.get("state") or "").lower()
            entry["status"] = payload_state if payload_state else _ORDER_STATUS[event_type]
            entry["updated_at"] = event.get("ts")
            entry["lifecycle"].append(
                {"event": event_type, "ts": event.get("ts"), "details": sanitize(payload)}
            )
        return sorted(
            orders.values(), key=lambda item: str(item.get("updated_at") or ""), reverse=True
        )[:50]

    def snapshot(self) -> dict[str, Any]:
        generated_at = _utc_now().isoformat()
        kill_active = (self.runtime_dir / "KILL_SWITCH").is_file()
        events: list[dict[str, Any]] = []
        approvals: list[dict[str, Any]] = []
        pnl_daily: list[dict[str, str]] = []
        state: dict[str, str] = {}
        heartbeats: list[dict[str, Any]] = []
        master: dict[str, Any] = {
            "equity_usd": "1000.00",
            "daily_pnl_usd": "0.00",
            "position": None,
        }
        ai: dict[str, Any] = {"enabled": False, "pending": 0, "decided": 0, "latest": None}
        market_events: list[dict[str, Any]] = []
        audit_readable = False
        audit_chain_valid = False
        audit_event_count = 0
        database_detail = "audit database unavailable"

        try:
            with closing(self._connect()) as connection:
                tables = self._tables(connection)
                events = self._read_events(connection, tables)
                approvals = self._read_approvals(connection, tables)
                pnl_daily = self._read_pnl(connection, tables)
                state = self._read_state(connection, tables)
                heartbeats = self._read_heartbeats(connection, tables)
                master = self._read_master(connection, tables)
                ai = self._read_ai(connection, tables)
                market_events = self._read_market_events(connection, tables)
                audit_readable = "events" in tables
                if audit_readable:
                    audit_event_count = int(
                        connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
                    )
                    audit_chain_valid = self._verify_chain(connection, tables)
                database_detail = (
                    "read-only chain valid"
                    if audit_chain_valid
                    else "audit chain invalid or unavailable"
                )
        except (OSError, sqlite3.Error):
            pass

        persisted_kill_state = state.get("kill_state")
        if persisted_kill_state:
            kill_active = kill_active or persisted_kill_state != "ACTIVE"
        elif state.get("kill_switch_active", "").lower() in {"1", "true", "active"}:
            kill_active = True

        strategy_cards = self._strategy_cards(events)
        pending_approvals = sum(item["status"] == "awaiting_owner" for item in approvals)
        top3_count = sum(bool(item["top3"]) for item in strategy_cards)
        latest_pnl = pnl_daily[-1] if pnl_daily else None
        daily_pnl = "0.00"
        if latest_pnl:
            try:
                daily_pnl = (
                    str(Decimal(latest_pnl["daily_pnl_usd"]))
                    if "daily_pnl_usd" in latest_pnl
                    else str(
                        Decimal(latest_pnl["realized_usd"]) + Decimal(latest_pnl["unrealized_usd"])
                    )
                )
            except (InvalidOperation, TypeError, ValueError):
                daily_pnl = "0.00"

        unhealthy_heartbeat = any(
            str(item.get("status", "")).lower() not in {"ok", "healthy", "ready"}
            for item in heartbeats
        )
        master_reconciliation_drift = bool(state.get("master_reconciliation_drift"))
        health_status = (
            "halted"
            if kill_active
            else "ok"
            if audit_chain_valid and not unhealthy_heartbeat and not master_reconciliation_drift
            else "degraded"
        )
        checks = [
            {
                "name": "audit_store",
                "status": "ok" if audit_chain_valid else "error",
                "detail": database_detail,
            },
            {
                "name": "execution_mode",
                "status": "ok",
                "detail": "DEMO-only; real-money unavailable",
            },
            {
                "name": "kill_switch",
                "status": "halted" if kill_active else "ok",
                "detail": "active" if kill_active else "inactive",
            },
            {
                "name": "credential_exposure",
                "status": "ok",
                "detail": "dashboard projection contains no credentials",
            },
        ]
        if master_reconciliation_drift:
            checks.append(
                {
                    "name": "master_reconciliation",
                    "status": "error",
                    "detail": "local master ledger differs from DEMO broker truth",
                }
            )
        checks.extend(heartbeats)

        latest_hash = events[0]["event_hash"] if events else None
        return sanitize(
            {
                "schema_version": SCHEMA_VERSION,
                "generated_at": generated_at,
                "overview": {
                    "account_mode": "DEMO",
                    "real_money": False,
                    "read_only": True,
                    "strategy_count": len(strategy_cards),
                    "top3_count": top3_count,
                    "shadow_capital_usd": str(
                        Decimal(self.shadow_capital_usd) * len(strategy_cards)
                    ),
                    "daily_pnl_usd": daily_pnl,
                    "pending_approvals": pending_approvals,
                    "audit_events": audit_event_count,
                    "master_equity_usd": master.get("equity_usd", "1000.00"),
                    "master_daily_pnl_usd": master.get("daily_pnl_usd", "0.00"),
                    "ai_pending": ai.get("pending", 0),
                },
                "kill_switch": {
                    "active": kill_active,
                    "mode": persisted_kill_state or ("HALT_NEW_ORDERS" if kill_active else "READY"),
                    "read_only": True,
                },
                "strategies": strategy_cards,
                "master": master,
                "ai": ai,
                "market_events": market_events,
                "pnl": {"currency": "USD", "daily": pnl_daily, "latest": latest_pnl},
                "orders": self._orders(events),
                "approvals": approvals,
                "activity": events[: self.activity_limit],
                "audit": {
                    "readable": audit_readable,
                    "chain_valid": audit_chain_valid,
                    "latest_event_hash": latest_hash,
                    "events_loaded": len(events),
                },
                "health": {"status": health_status, "checks": checks},
            }
        )


def _sse(event: str, payload: Mapping[str, Any]) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"event: {event}\ndata: {body}\n\n"


def _trusted_proxy_allows(client_host: str, trusted_proxy: str | None) -> bool:
    return not trusted_proxy or hmac.compare_digest(client_host, trusted_proxy)


def _proxy_secret_allows(headers: Mapping[str, str], proxy_secret: str | None) -> bool:
    if not proxy_secret:
        return True
    actual = headers.get("x-etoro-proxy-secret") or headers.get("X-Etoro-Proxy-Secret")
    return bool(actual) and hmac.compare_digest(actual, proxy_secret)


def create_app(
    service: DashboardService | None = None,
    *,
    control_audit: AuditLog | None = None,
    owner_username: str | None = None,
    identity_header: str = "x-authentik-username",
    trusted_proxy_ip: str | None = None,
    proxy_secret: str | None = None,
    static_dir: str | Path | None = None,
    sse_interval_seconds: float = 2.0,
) -> Any:
    """FastAPI factory for deployment behind an owner-only Authentik forward-auth route."""

    if FastAPI is None:
        raise RuntimeError(
            "FastAPI is optional and not installed; install it only in the dashboard runtime"
        )
    dashboard = service or DashboardService()
    static_path = Path(static_dir) if static_dir else Path(__file__).with_name("dashboard_static")
    if not (static_path / "index.html").is_file():
        raise RuntimeError("dashboard static assets are unavailable")
    policy = OwnerIdentityPolicy(
        owner_username or os.getenv("ETORO_DASHBOARD_OWNER"), identity_header.lower()
    )
    trusted_proxy = trusted_proxy_ip or os.getenv("ETORO_TRUSTED_PROXY_IP")
    boundary_secret = proxy_secret or os.getenv("ETORO_PROXY_SECRET")
    interval = max(1.0, min(float(sse_interval_seconds), 30.0))
    app = FastAPI(
        title="eToro DEMO Agent Dashboard",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.dashboard_service = dashboard
    app.state.owner_policy = policy
    app.state.trusted_proxy_ip = trusted_proxy
    app.state.proxy_secret_configured = bool(boundary_secret)

    @app.middleware("http")
    async def owner_gate(request: Request, call_next: Any) -> Any:
        client_host = request.client.host if request.client is not None else ""
        if request.url.path != "/healthz" and not _trusted_proxy_allows(client_host, trusted_proxy):
            response = JSONResponse(
                status_code=403,
                content={"detail": "request did not arrive through the trusted proxy"},
            )
        elif request.url.path != "/healthz" and not _proxy_secret_allows(
            request.headers, boundary_secret
        ):
            response = JSONResponse(
                status_code=403,
                content={"detail": "proxy boundary authentication failed"},
            )
        elif request.url.path != "/healthz" and not policy.allows(request.headers):
            response = JSONResponse(
                status_code=503 if not policy.configured else 403,
                content={"detail": "owner identity is not authorized"},
            )
        elif request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin", "")
            origin_host = urlparse(origin).netloc
            request_host = request.headers.get("host", "")
            if (
                request.headers.get("x-etoro-csrf") != "1"
                or not origin_host
                or not hmac.compare_digest(origin_host, request_host)
            ):
                response = JSONResponse(
                    status_code=403,
                    content={"detail": "same-origin CSRF validation failed"},
                )
            else:
                response = await call_next(request)
        else:
            response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
            "img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    app.mount("/static", StaticFiles(directory=static_path), name="dashboard_static")

    @app.get("/")
    async def index() -> Any:
        return FileResponse(static_path / "index.html", media_type="text/html")

    @app.get("/api/snapshot")
    async def snapshot() -> Any:
        return JSONResponse(dashboard.snapshot())

    @app.get("/api/strategies")
    async def strategies() -> Any:
        return JSONResponse(dashboard.list_strategies())

    @app.get("/api/strategies/{strategy_id}")
    async def strategy_detail(strategy_id: str) -> Any:
        try:
            return JSONResponse(dashboard.strategy_detail(strategy_id))
        except KeyError as exc:
            return JSONResponse(status_code=404, content={"detail": str(exc)})

    def _trade_query(request: Request, strategy_id: str | None = None) -> dict[str, Any]:
        query = request.query_params
        try:
            limit = int(query.get("limit", "50"))
            offset = int(query.get("offset", "0"))
        except ValueError as exc:
            raise ValueError("limit and offset must be integers") from exc
        return dashboard.list_trades(
            strategy_id=strategy_id or query.get("strategy_id"),
            symbol=query.get("symbol"),
            status=query.get("status"),
            side=query.get("side"),
            from_ts=query.get("from_ts"),
            to_ts=query.get("to_ts"),
            limit=limit,
            offset=offset,
        )

    @app.get("/api/trades")
    async def trades(request: Request) -> Any:
        try:
            return JSONResponse(_trade_query(request))
        except KeyError as exc:
            return JSONResponse(status_code=404, content={"detail": str(exc)})
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.get("/api/strategies/{strategy_id}/trades")
    async def strategy_trades(strategy_id: str, request: Request) -> Any:
        try:
            return JSONResponse(_trade_query(request, strategy_id))
        except KeyError as exc:
            return JSONResponse(status_code=404, content={"detail": str(exc)})
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.get("/api/trades/{trade_id}")
    async def trade_detail(trade_id: str) -> Any:
        try:
            return JSONResponse(dashboard.trade_detail(trade_id))
        except KeyError as exc:
            return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.get("/api/reviews")
    async def reviews(request: Request) -> Any:
        query = request.query_params
        try:
            return JSONResponse(
                dashboard.list_reviews(
                    strategy_id=query.get("strategy_id"),
                    trade_id=query.get("trade_id"),
                    limit=int(query.get("limit", "50")),
                    offset=int(query.get("offset", "0")),
                )
            )
        except KeyError as exc:
            return JSONResponse(status_code=404, content={"detail": str(exc)})
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.get("/api/ai/usage")
    async def ai_usage(request: Request) -> Any:
        try:
            return JSONResponse(
                dashboard.ai_usage(days=int(request.query_params.get("days", "30")))
            )
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.get("/api/events")
    async def events(request: Request) -> Any:
        async def stream() -> Any:
            previous_digest: str | None = None
            unchanged = 0
            while not await request.is_disconnected():
                current = dashboard.snapshot()
                digestable = dict(current)
                digestable.pop("generated_at", None)
                encoded = json.dumps(
                    digestable, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                )
                digest = hashlib.sha256(encoded.encode()).hexdigest()
                if digest != previous_digest:
                    yield _sse("snapshot", current)
                    previous_digest = digest
                    unchanged = 0
                else:
                    unchanged += 1
                    if unchanged * interval >= 15:
                        yield ": keepalive\n\n"
                        unchanged = 0
                await asyncio.sleep(interval)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-store", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/control/kill")
    async def kill(request: Request) -> Any:
        if control_audit is None:
            return JSONResponse(status_code=503, content={"detail": "control store unavailable"})
        payload = await request.json()
        reason = str(payload.get("reason", "dashboard manual kill"))[:240]
        dashboard.runtime_dir.mkdir(parents=True, exist_ok=True)
        (dashboard.runtime_dir / "KILL_SWITCH").touch(exist_ok=True)
        control_audit.set_kill_state(KillState.LOCKED, policy.owner_username or "owner", reason)
        return JSONResponse({"status": "LOCKED", "real_money": False})

    @app.post("/api/control/resume")
    async def resume(request: Request) -> Any:
        if control_audit is None:
            return JSONResponse(status_code=503, content={"detail": "control store unavailable"})
        payload = await request.json()
        if payload.get("confirmation") != "RESUME_DEMO":
            return JSONResponse(status_code=400, content={"detail": "confirmation mismatch"})
        if not control_audit.verify_chain():
            return JSONResponse(status_code=409, content={"detail": "audit chain is invalid"})
        unknown = [item for item in control_audit.list_pending() if item["state"] == "UNKNOWN"]
        if unknown:
            return JSONResponse(
                status_code=409,
                content={"detail": "unknown executions must be reconciled before resume"},
            )
        (dashboard.runtime_dir / "KILL_SWITCH").unlink(missing_ok=True)
        control_audit.set_kill_state(
            KillState.ACTIVE, policy.owner_username or "owner", "dashboard explicit resume"
        )
        return JSONResponse({"status": "ACTIVE", "real_money": False})

    @app.post("/api/approvals/{proposal_id}")
    async def approve(proposal_id: str, request: Request) -> Any:
        if control_audit is None:
            return JSONResponse(status_code=503, content={"detail": "control store unavailable"})
        payload = await request.json()
        envelope_hash = str(payload.get("envelope_hash", ""))
        expected_phrase = f"APPROVE {proposal_id} {envelope_hash[-8:]}"
        if not envelope_hash or payload.get("confirmation") != expected_phrase:
            return JSONResponse(status_code=400, content={"detail": "exact approval mismatch"})
        try:
            control_audit.approve_once(proposal_id, envelope_hash, policy.owner_username or "owner")
        except (PermissionError, ValueError) as exc:
            return JSONResponse(status_code=409, content={"detail": str(exc)})
        return JSONResponse(
            {
                "status": "APPROVED",
                "proposal_id": proposal_id,
                "envelope_hash": envelope_hash,
                "real_money": False,
            }
        )

    @app.get("/healthz")
    async def healthz() -> Any:
        state = dashboard.snapshot()
        return JSONResponse(
            {
                "status": state["health"]["status"],
                "account_mode": "DEMO",
                "real_money": False,
                "owner_auth_configured": policy.configured,
            }
        )

    return app
