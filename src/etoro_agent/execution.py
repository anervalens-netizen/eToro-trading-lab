from __future__ import annotations

import json
from dataclasses import asdict
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from .audit import AuditLog
from .mcp import EtoroMCPClient, MCPResult
from .models import ApprovedOrder, ExecutionState, KillState
from .risk import OrderVerifier, canonical_hash


@dataclass(frozen=True)
class PaperFill:
    proposal_id: str
    symbol: str
    side: str
    amount_usd: Decimal
    price: Decimal


class PaperBroker:
    def __init__(self, audit: AuditLog) -> None:
        self.audit = audit

    def execute(self, order: ApprovedOrder, verifier: OrderVerifier, bid: Decimal, ask: Decimal) -> PaperFill:
        if not verifier.verify(order):
            raise PermissionError("risk seal invalid or expired")
        body = json.loads(order.body_json)
        if body["transaction"] != "buy":
            raise PermissionError("paper baseline supports long entries only")
        price = ask if body["transaction"] == "buy" else bid
        fill = PaperFill(order.proposal_id, body["symbol"], body["transaction"], Decimal(str(body["amount"])), price)
        cash = Decimal(self.audit.state_get("paper_cash_usd", "0"))
        if cash < fill.amount_usd:
            raise PermissionError("insufficient paper cash")
        row = self.audit.db.execute(
            "SELECT units,average_price FROM paper_positions WHERE symbol=?", (fill.symbol,)
        ).fetchone()
        old_units = Decimal(row[0]) if row else Decimal("0")
        old_average = Decimal(row[1]) if row else Decimal("0")
        new_units = fill.amount_usd / fill.price
        total_units = old_units + new_units
        average = ((old_units * old_average) + fill.amount_usd) / total_units
        self.audit.db.execute(
            "INSERT INTO paper_positions VALUES(?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET units=excluded.units, average_price=excluded.average_price, last_price=excluded.last_price",
            (fill.symbol, str(total_units), str(average), str(fill.price)),
        )
        self.audit.db.execute(
            "INSERT INTO paper_trades(ts,symbol,side,amount_usd,price) VALUES(?,?,?,?,?)",
            (datetime.now(timezone.utc).isoformat(), fill.symbol, fill.side, str(fill.amount_usd), str(fill.price)),
        )
        self.audit.db.commit()
        self.audit.state_set("paper_cash_usd", str(cash - fill.amount_usd))
        self.audit.append("paper_fill", fill.__dict__)
        return fill


class EtoroDemoBroker:
    """No raw-order API: accepts only a sealed risk approval plus one-time operator approval."""

    def __init__(
        self,
        client: EtoroMCPClient,
        audit: AuditLog,
        runtime_dir: str | Path | None = None,
    ) -> None:
        self.client = client
        self.audit = audit
        self.runtime_dir = Path(runtime_dir) if runtime_dir is not None else audit.path.parent

    def _envelope_hash(self, order: ApprovedOrder) -> str:
        return canonical_hash(asdict(order))

    def _kill_active(self) -> bool:
        return (
            (self.runtime_dir / "KILL_SWITCH").exists()
            or self.audit.kill_state() is not KillState.ACTIVE
        )

    def execute(self, order: ApprovedOrder, verifier: OrderVerifier) -> MCPResult:
        if not verifier.verify(order):
            raise PermissionError("risk seal invalid or expired")
        if self._kill_active():
            raise PermissionError("kill switch does not permit new DEMO positions")
        envelope_hash = self._envelope_hash(order)
        self.audit.require_approval(order.proposal_id, envelope_hash)
        self.client.verify_demo_scope()
        body = json.loads(order.body_json)
        eligibility = self.client.execute_read(
            "/api/v2/trading/info/demo/eligibility",
            body=json.dumps({"symbols": [body["symbol"]], "currency": "USD"}),
        )
        rows = eligibility.body.get("eligibilities", []) if isinstance(eligibility.body, dict) else []
        if not eligibility.is_success or len(rows) != 1 or not rows[0].get("allowOpenPosition"):
            raise PermissionError("instrument is not currently eligible for a DEMO open order")
        costs = self.client.execute_read("/api/v2/trading/info/demo/costs", body=order.body_json)
        if not costs.is_success:
            raise PermissionError("DEMO cost preview failed; order not sent")
        self.audit.append("demo_pretrade_validation", {"proposal_id": order.proposal_id, "eligibility": rows[0], "costs": costs.body})
        if not verifier.verify(order):
            raise PermissionError("risk seal expired during DEMO preflight")
        if self._kill_active():
            raise PermissionError("kill switch changed during DEMO preflight")
        self.audit.begin_execution(order.proposal_id, envelope_hash, order.request_id)
        try:
            result = self.client.execute_demo_order(
                order.route, order.body_json, order.request_id
            )
        except Exception as exc:
            response = {"error_type": type(exc).__name__}
            self.audit.finish_execution(
                order.proposal_id, ExecutionState.UNKNOWN, response
            )
            self.audit.append(
                "etoro_demo_execution_unknown",
                {"proposal_id": order.proposal_id, **response},
            )
            raise
        state = (
            ExecutionState.ACKNOWLEDGED
            if result.is_success
            else ExecutionState.UNKNOWN
            if result.status_code == 0 or result.status_code >= 500
            else ExecutionState.REJECTED
        )
        response = {
            "status_code": result.status_code,
            "is_success": result.is_success,
            "x_request_id": result.x_request_id,
            "body": result.body,
        }
        self.audit.finish_execution(order.proposal_id, state, response)
        self.audit.append(
            "etoro_demo_execution",
            {"proposal_id": order.proposal_id, "state": state.value, **response},
        )
        return result
