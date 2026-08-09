from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

from .audit import AuditLog
from .models import KillState
from .strategy import STRATEGY_DEFINITIONS, STRATEGY_PORTFOLIO_BY_ID


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
    return datetime.now(timezone.utc)


def _safe_json_loads(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return value


def _is_sensitive_key(key: object) -> bool:
    normalized = "".join(character for character in str(key).strip().lower() if character.isalnum())
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
            cleaned[key_text] = "[REDACTED]" if _is_sensitive_key(key) else sanitize(item, _depth=_depth + 1)
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
    ) -> None:
        self.audit_db_path = Path(audit_db_path)
        self.runtime_dir = Path(runtime_dir)
        self.strategies = tuple(dict(item) for item in strategies)
        if len(self.strategies) != 12:
            raise ValueError("dashboard requires exactly 12 shadow strategies")
        if len({item.get("id") for item in self.strategies}) != 12:
            raise ValueError("dashboard strategy identifiers must be unique")
        self.shadow_capital_usd = str(shadow_capital_usd)
        self.activity_limit = max(10, min(int(activity_limit), 200))

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

    def _read_events(self, connection: sqlite3.Connection, tables: set[str]) -> list[dict[str, Any]]:
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
        order_by = "last_updated DESC, rowid DESC" if "last_updated" in columns else "rowid DESC"
        rows = connection.execute(f"SELECT * FROM approvals ORDER BY {order_by} LIMIT 50").fetchall()
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
                "consumed" if row["consumed_at"] else "approved" if row["approved_at"] else "awaiting_owner",
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
                    "response": sanitize(_safe_json_loads(row["response_json"])) if "response_json" in row_keys else None,
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
                "ORDER BY day DESC,portfolio_id LIMIT 500"
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
                    parsed = {target: Decimal(str(row[source])) for target, source in fields.items()}
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
                        for field in ("realized_usd", "unrealized_usd", "fees_usd", "financing_usd", "equity_usd")
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
    def _read_state(connection: sqlite3.Connection, tables: set[str]) -> dict[str, str]:
        if "state" not in tables:
            return {}
        return {str(row["key"]): str(row["value"]) for row in connection.execute("SELECT key,value FROM state")}

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

    def _strategy_cards(self, events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        strategy_by_portfolio = {portfolio_id: strategy_id for strategy_id, portfolio_id in STRATEGY_PORTFOLIO_BY_ID.items()}
        cards: dict[str, dict[str, Any]] = {
            item["id"]: {
                "id": item["id"],
                "name": item.get("name", item["id"]),
                "family": item.get("family", "strategy"),
                "status": "waiting_for_data",
                "nav_usd": self.shadow_capital_usd,
                "daily_pnl_usd": "0.00",
                "total_pnl_usd": "0.00",
                "drawdown_fraction": "0",
                "trades": 0,
                "rank": None,
                "top3": False,
                "last_signal": None,
                "updated_at": None,
            }
            for item in self.strategies
        }
        relevant = {"strategy_snapshot", "shadow_portfolio_snapshot", "strategy_status", "strategy_rank"}
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
            }
            cards[strategy_id].update({key: sanitize(payload[key]) for key in allowed if key in payload})
            if "equity_usd" in payload:
                cards[strategy_id]["nav_usd"] = sanitize(payload["equity_usd"])
            if "trades_today" in payload:
                cards[strategy_id]["trades"] = sanitize(payload["trades_today"])
            if "initial_cash_usd" in payload and "equity_usd" in payload:
                try:
                    cards[strategy_id]["total_pnl_usd"] = str(
                        Decimal(str(payload["equity_usd"])) - Decimal(str(payload["initial_cash_usd"]))
                    )
                except InvalidOperation:
                    pass
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
        return sorted(orders.values(), key=lambda item: str(item.get("updated_at") or ""), reverse=True)[:50]

    def snapshot(self) -> dict[str, Any]:
        generated_at = _utc_now().isoformat()
        kill_active = (self.runtime_dir / "KILL_SWITCH").is_file()
        events: list[dict[str, Any]] = []
        approvals: list[dict[str, Any]] = []
        pnl_daily: list[dict[str, str]] = []
        state: dict[str, str] = {}
        heartbeats: list[dict[str, Any]] = []
        audit_readable = False
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
                audit_readable = "events" in tables
                if audit_readable:
                    audit_event_count = int(connection.execute("SELECT COUNT(*) FROM events").fetchone()[0])
                database_detail = "read-only connection healthy" if audit_readable else "events table unavailable"
        except (OSError, sqlite3.Error):
            pass

        persisted_kill_state = state.get("kill_state")
        if persisted_kill_state:
            kill_active = persisted_kill_state != "ACTIVE"
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
                    else str(Decimal(latest_pnl["realized_usd"]) + Decimal(latest_pnl["unrealized_usd"]))
                )
            except (InvalidOperation, TypeError, ValueError):
                daily_pnl = "0.00"

        unhealthy_heartbeat = any(
            str(item.get("status", "")).lower() not in {"ok", "healthy", "ready"}
            for item in heartbeats
        )
        health_status = "halted" if kill_active else "ok" if audit_readable and not unhealthy_heartbeat else "degraded"
        checks = [
            {"name": "audit_store", "status": "ok" if audit_readable else "error", "detail": database_detail},
            {"name": "execution_mode", "status": "ok", "detail": "DEMO-only; real-money unavailable"},
            {"name": "kill_switch", "status": "halted" if kill_active else "ok", "detail": "active" if kill_active else "inactive"},
            {"name": "credential_exposure", "status": "ok", "detail": "dashboard projection contains no credentials"},
        ]
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
                    "shadow_capital_usd": str(Decimal(self.shadow_capital_usd) * len(strategy_cards)),
                    "daily_pnl_usd": daily_pnl,
                    "pending_approvals": pending_approvals,
                    "audit_events": audit_event_count,
                },
                "kill_switch": {
                    "active": kill_active,
                    "mode": persisted_kill_state or ("HALT_NEW_ORDERS" if kill_active else "READY"),
                    "read_only": True,
                },
                "strategies": strategy_cards,
                "pnl": {"currency": "USD", "daily": pnl_daily, "latest": latest_pnl},
                "orders": self._orders(events),
                "approvals": approvals,
                "activity": events[: self.activity_limit],
                "audit": {"readable": audit_readable, "latest_event_hash": latest_hash, "events_loaded": len(events)},
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
    actual = headers.get("x-etoro-proxy-secret") or headers.get(
        "X-Etoro-Proxy-Secret"
    )
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
        raise RuntimeError("FastAPI is optional and not installed; install it only in the dashboard runtime")
    dashboard = service or DashboardService()
    static_path = Path(static_dir) if static_dir else Path(__file__).with_name("dashboard_static")
    if not (static_path / "index.html").is_file():
        raise RuntimeError("dashboard static assets are unavailable")
    policy = OwnerIdentityPolicy(owner_username or os.getenv("ETORO_DASHBOARD_OWNER"), identity_header.lower())
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
        if (
            request.url.path != "/healthz"
            and not _trusted_proxy_allows(client_host, trusted_proxy)
        ):
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

    @app.get("/api/events")
    async def events(request: Request) -> Any:
        async def stream() -> Any:
            previous_digest: str | None = None
            unchanged = 0
            while not await request.is_disconnected():
                current = dashboard.snapshot()
                digestable = dict(current)
                digestable.pop("generated_at", None)
                encoded = json.dumps(digestable, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
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
            control_audit.approve_once(
                proposal_id, envelope_hash, policy.owner_username or "owner"
            )
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
