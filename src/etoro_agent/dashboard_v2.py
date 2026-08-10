from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from .config_v2 import load_config_v2
from .promotion_v2 import PromotionEvidenceV2, PromotionGateV2
from .runtime_store_v2 import RuntimeStoreV2


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
            open_positions = [item for item in positions if item.status.value == "OPEN"]
            closed_positions = [item for item in positions if item.status.value == "CLOSED"]
            realized = sum((item.realized_pnl for item in positions), Decimal("0"))
            unrealized = sum((item.unrealized_pnl for item in open_positions), Decimal("0"))
            fees = sum((item.fees_accrued for item in positions), Decimal("0"))
            financing = sum((item.financing_accrued for item in positions), Decimal("0"))
            events = int(store.db.execute("SELECT COUNT(*) FROM v2_events").fetchone()[0])
            fills = int(store.db.execute("SELECT COUNT(*) FROM v2_fills").fetchone()[0])
            decisions = {
                str(row[0]): int(row[1])
                for row in store.db.execute("SELECT state,COUNT(*) FROM v2_decisions GROUP BY state")
            }
            order_states: dict[str, int] = {}
            for row in store.db.execute("SELECT state_json FROM v2_broker_orders"):
                try:
                    state = str(json.loads(str(row[0])).get("status", "UNKNOWN"))
                except json.JSONDecodeError:
                    state = "UNKNOWN"
                order_states[state] = order_states.get(state, 0) + 1
            compatibility = [asdict(item) for item in config.compatibility()]
            return {
                "schema_version": 2,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "real_money": False,
                "account_mode": "DEMO",
                "trading_state": store.state_get("trading_state", "LOCKED"),
                "research_epoch": store.state_get("research_epoch_v2", ""),
                "audit": {"events": events, "chain_valid": store.verify_event_chain()},
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
                "orders": order_states,
                "ai_decisions": decisions,
                "compatibility": compatibility,
            }
        finally:
            store.close()


def create_v2_app(service: DashboardServiceV2) -> Any:
    try:
        from fastapi import FastAPI
        from fastapi.responses import JSONResponse
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("FastAPI is required for dashboard v2") from exc
    app = FastAPI(title="eToro Trading Lab v2", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    async def healthz() -> Any:
        snapshot = service.snapshot()
        status = 200 if snapshot["audit"]["chain_valid"] else 503
        return JSONResponse({"status": "ok" if status == 200 else "error", "real_money": False}, status_code=status)

    @app.get("/api/v2/snapshot")
    async def snapshot() -> Any:
        return JSONResponse(service.snapshot(), headers={"Cache-Control": "no-store"})

    return app
