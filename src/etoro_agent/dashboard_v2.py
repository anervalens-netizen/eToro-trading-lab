from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .config_v2 import load_config_v2
from .domain_v2 import PositionState
from .postgres_runtime_v2 import PostgresRuntimeStoreV2
from .runtime_store_v2 import RuntimeStoreV2


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, sort_keys=True, default=str))


def _snapshot_payload(
    *,
    config: Any,
    positions: tuple[PositionState, ...],
    trading_state: str,
    research_epoch: str,
    chain_valid: bool,
    events: int,
    fills: int,
    decisions: Mapping[str, int],
    order_states: Mapping[str, int],
) -> dict[str, Any]:
    open_positions = [item for item in positions if item.status.value == "OPEN"]
    closed_positions = [item for item in positions if item.status.value == "CLOSED"]
    realized = sum((item.realized_pnl for item in positions), Decimal("0"))
    unrealized = sum((item.unrealized_pnl for item in open_positions), Decimal("0"))
    fees = sum((item.fees_accrued for item in positions), Decimal("0"))
    financing = sum((item.financing_accrued for item in positions), Decimal("0"))
    unknown_orders = int(order_states.get("UNKNOWN", 0))
    health_status = (
        "error"
        if not chain_valid
        else "halted"
        if trading_state != "ACTIVE"
        else "degraded"
        if unknown_orders
        else "ok"
    )
    return _json_safe(
        {
            "schema_version": 2,
            "generated_at": datetime.now(UTC).isoformat(),
            "real_money": False,
            "account_mode": "DEMO",
            "trading_state": trading_state,
            "research_epoch": research_epoch,
            "health": {
                "status": health_status,
                "unknown_orders": unknown_orders,
            },
            "audit": {"events": events, "chain_valid": chain_valid},
            "portfolio": {
                "initial_cash_usd": str(config.initial_cash_usd),
                "realized_pnl_usd": str(realized),
                "unrealized_pnl_usd": str(unrealized),
                "fees_usd": str(fees),
                "financing_usd": str(financing),
                "open_positions": len(open_positions),
                "closed_positions": len(closed_positions),
                "fills": fills,
            },
            "positions": [asdict(item) for item in positions],
            "orders": dict(order_states),
            "ai_decisions": dict(decisions),
            "compatibility": [asdict(item) for item in config.compatibility()],
        }
    )


class DashboardServiceV2:
    """Read-only projection for v2 economic/runtime/research state."""

    def __init__(self, runtime_db: str | Path, config_path: str | Path) -> None:
        self.runtime_db = Path(runtime_db)
        self.config_path = Path(config_path)

    def snapshot(self) -> dict[str, Any]:
        config = load_config_v2(self.config_path)
        store = RuntimeStoreV2(self.runtime_db)
        try:
            positions = store.positions()
            events = int(store.db.execute("SELECT COUNT(*) FROM v2_events").fetchone()[0])
            fills = int(store.db.execute("SELECT COUNT(*) FROM v2_fills").fetchone()[0])
            decisions = {
                str(row[0]): int(row[1])
                for row in store.db.execute(
                    "SELECT state,COUNT(*) FROM v2_decisions GROUP BY state"
                )
            }
            order_states: dict[str, int] = {}
            for row in store.db.execute("SELECT state_json FROM v2_broker_orders"):
                try:
                    state = str(json.loads(str(row[0])).get("status", "UNKNOWN"))
                except json.JSONDecodeError:
                    state = "UNKNOWN"
                order_states[state] = order_states.get(state, 0) + 1
            return _snapshot_payload(
                config=config,
                positions=positions,
                trading_state=store.state_get("trading_state", "LOCKED"),
                research_epoch=store.state_get("research_epoch_v2", ""),
                chain_valid=store.verify_event_chain(),
                events=events,
                fills=fills,
                decisions=decisions,
                order_states=order_states,
            )
        finally:
            store.close()


class PostgresDashboardServiceV2:
    """Read-only dashboard projection from the canonical PostgreSQL runtime."""

    def __init__(self, dsn: str, config_path: str | Path) -> None:
        if not dsn.strip():
            raise ValueError("PostgreSQL DSN is required")
        self.dsn = dsn
        self.config_path = Path(config_path)

    def snapshot(self) -> dict[str, Any]:
        config = load_config_v2(self.config_path)
        store = PostgresRuntimeStoreV2.from_dsn(self.dsn)
        try:
            store.require_schema()
            positions = store.positions()
            with store.connection.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) FROM v2_events")
                events = int(cursor.fetchone()[0])
                cursor.execute("SELECT COUNT(*) FROM v2_fills")
                fills = int(cursor.fetchone()[0])
                cursor.execute("SELECT status,COUNT(*) FROM v2_broker_orders GROUP BY status")
                order_states = {str(row[0]): int(row[1]) for row in cursor.fetchall()}
                cursor.execute("SELECT to_regclass('v2_ai_packets')")
                ai_table = cursor.fetchone()[0]
                decisions: dict[str, int] = {}
                if ai_table is not None:
                    cursor.execute("SELECT state,COUNT(*) FROM v2_ai_packets GROUP BY state")
                    decisions = {str(row[0]): int(row[1]) for row in cursor.fetchall()}
            return _snapshot_payload(
                config=config,
                positions=positions,
                trading_state=store.state_get("trading_state", "LOCKED"),
                research_epoch=store.state_get("research_epoch_v2", ""),
                chain_valid=store.verify_event_chain(),
                events=events,
                fills=fills,
                decisions=decisions,
                order_states=order_states,
            )
        finally:
            store.close()


def create_v2_app(service: DashboardServiceV2 | PostgresDashboardServiceV2) -> Any:
    try:
        from fastapi import FastAPI
        from fastapi.responses import JSONResponse
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("FastAPI is required for dashboard v2") from exc
    app = FastAPI(title="eToro Trading Lab v2", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    async def healthz() -> Any:
        snapshot = service.snapshot()
        health_status = str(snapshot["health"]["status"])
        status = 200 if health_status == "ok" else 503
        return JSONResponse({"status": health_status, "real_money": False}, status_code=status)

    @app.get("/api/v2/snapshot")
    async def snapshot() -> Any:
        return JSONResponse(service.snapshot(), headers={"Cache-Control": "no-store"})

    return app
