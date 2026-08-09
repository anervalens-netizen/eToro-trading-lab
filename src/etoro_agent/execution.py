from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .audit import AuditLog
from .mcp import EtoroMCPClient, MCPResult
from .market import INSTRUMENTS_BY_SYMBOL
from .models import ApprovedOrder, ExecutionState, KillState
from .risk import OrderVerifier, canonical_hash


STANDING_DEMO_SOURCES = frozenset({"sol_master_open", "sol_master_close"})


def authorize_standing_demo(
    audit: AuditLog,
    runtime_dir: str | Path,
    verifier: OrderVerifier,
    proposal: dict[str, object],
) -> bool:
    """Consume the owner's standing DEMO mandate only for a sealed Sol order."""

    if proposal.get("state") == ExecutionState.APPROVED.value:
        return True
    if proposal.get("state") != ExecutionState.AWAITING_APPROVAL.value:
        return False
    source = str(proposal.get("source", ""))
    if source not in STANDING_DEMO_SOURCES:
        return False
    runtime = Path(runtime_dir)
    kill_active = (
        (runtime / "KILL_SWITCH").exists()
        or audit.kill_state() is not KillState.ACTIVE
    )
    if kill_active and source != "sol_master_close":
        return False
    if not audit.verify_chain():
        return False
    proposal_id = str(proposal["proposal_id"])
    order = audit.load_order(proposal_id)
    if not verifier.verify(order):
        return False
    envelope_hash = canonical_hash(asdict(order))
    if envelope_hash != str(proposal.get("envelope_hash", "")):
        return False
    audit.approve_once(
        proposal_id,
        envelope_hash,
        "standing-demo-policy",
    )
    return True


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


def select_broker_eligibility(
    body: dict[str, object], response_body: object
) -> tuple[dict[str, object], dict[str, object]]:
    rows = (
        response_body.get("eligibilities", [])
        if isinstance(response_body, dict)
        else []
    )
    if len(rows) != 1 or not isinstance(rows[0], dict):
        raise PermissionError("instrument DEMO eligibility is unavailable")
    row = rows[0]
    if not row.get("allowOpenPosition"):
        raise PermissionError("instrument is not currently eligible for a DEMO open order")
    if row.get("allowedOrderQuantityType") not in {None, "amountOnly", "all"}:
        raise PermissionError("instrument does not allow amount-sized orders")
    amount = Decimal(str(body["amount"]))
    leverage = int(body["leverage"])
    minimum_exposure = Decimal(str(row.get("minPositionExposure", "0")))
    if amount * leverage < minimum_exposure:
        raise PermissionError("order is below broker minimum exposure")
    direction = "long" if body["transaction"] == "buy" else "short"
    configurations = [
        item
        for item in row.get("leverageConfigs", [])
        if isinstance(item, dict)
        and item.get("settlementType") == body["settlementType"]
        and item.get("direction") == direction
        and leverage in item.get("leverageValues", [])
    ]
    if len(configurations) != 1:
        raise PermissionError("broker leverage configuration is not exact")
    configuration = configurations[0]
    if not configuration.get("allowStopLossTakeProfit"):
        raise PermissionError("broker configuration disallows stop-loss/take-profit")
    if amount < Decimal(str(configuration.get("minPositionAmount", "0"))):
        raise PermissionError("order is below broker minimum position amount")
    return row, configuration


class EtoroDemoBroker:
    """No raw-order API: accepts only a sealed risk approval and exact authorization."""

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

    def _broker_snapshot(self) -> dict[str, object]:
        result = self.client.execute_read("/api/v1/trading/info/demo/portfolio")
        if not result.is_success or not isinstance(result.body, dict):
            raise RuntimeError("DEMO broker reconciliation failed")
        portfolio = result.body.get("clientPortfolio", result.body)
        if not isinstance(portfolio, dict):
            raise RuntimeError("DEMO broker portfolio shape is invalid")
        positions = portfolio.get("positions", [])
        open_orders = portfolio.get("ordersForOpen", [])
        pending_orders = portfolio.get("orders", [])
        if not all(isinstance(rows, list) for rows in (positions, open_orders, pending_orders)):
            raise RuntimeError("DEMO broker collections are invalid")
        position_ids = sorted(
            int(row.get("positionID", row.get("positionId", 0)))
            for row in positions
            if isinstance(row, dict)
            and int(row.get("positionID", row.get("positionId", 0))) > 0
        )
        canonical = json.dumps(
            portfolio, sort_keys=True, separators=(",", ":"), default=str
        )
        return {
            "position_count": len(positions),
            "open_order_count": len(open_orders) + len(pending_orders),
            "broker_exposure_count": len(positions)
            + len(open_orders)
            + len(pending_orders),
            "position_ids": position_ids,
            "snapshot_hash": hashlib.sha256(canonical.encode()).hexdigest(),
        }

    def _validate_open_eligibility(
        self, body: dict[str, object], verifier: OrderVerifier
    ) -> dict[str, object]:
        symbol = str(body["symbol"])
        eligibility = self.client.execute_read(
            "/api/v2/trading/info/demo/eligibility",
            body=json.dumps({"symbols": [symbol], "currency": "USD"}),
        )
        if not eligibility.is_success:
            raise PermissionError("instrument DEMO eligibility is unavailable")
        row, configuration = select_broker_eligibility(body, eligibility.body)
        leverage = int(body["leverage"])
        direction = "long" if body["transaction"] == "buy" else "short"

        instrument = INSTRUMENTS_BY_SYMBOL.get(symbol)
        if instrument is None:
            raise PermissionError("symbol has no fixed instrument mapping")
        rates = self.client.execute_read(
            "/api/v1/market-data/instruments/rates",
            {"instrumentIds": str(instrument.instrument_id)},
        )
        rate_rows = rates.body.get("rates", []) if isinstance(rates.body, dict) else []
        if not rates.is_success or len(rate_rows) != 1 or not isinstance(rate_rows[0], dict):
            raise PermissionError("fresh broker quote is unavailable")
        entry = Decimal(
            str(rate_rows[0]["ask"] if direction == "long" else rate_rows[0]["bid"])
        )
        try:
            quote_at = datetime.fromisoformat(
                str(rate_rows[0]["date"]).replace("Z", "+00:00")
            ).astimezone(timezone.utc)
            bid = Decimal(str(rate_rows[0]["bid"]))
            ask = Decimal(str(rate_rows[0]["ask"]))
        except (KeyError, ValueError, TypeError, InvalidOperation) as exc:
            raise PermissionError("fresh broker quote metadata is incomplete") from exc
        now = datetime.now(timezone.utc)
        quote_age = (now - quote_at).total_seconds()
        if quote_age < -5 or quote_age > verifier.limits.max_quote_age_seconds:
            raise PermissionError("fresh broker quote is stale or future-dated")
        mid = (bid + ask) / Decimal("2")
        if bid <= 0 or ask < bid or mid <= 0:
            raise PermissionError("fresh broker quote has invalid prices")
        spread_fraction = (ask - bid) / mid
        if spread_fraction > verifier.limits.max_spread_fraction:
            raise PermissionError("fresh broker spread exceeds deterministic limit")
        stop_fraction = abs(entry - Decimal(str(body["stopLossRate"]))) / entry
        take_fraction = abs(Decimal(str(body["takeProfitRate"])) - entry) / entry
        stop_percentage = stop_fraction * Decimal("100")
        take_percentage = take_fraction * Decimal("100")
        try:
            minimum_stop = Decimal(str(configuration["minStopLossPercentage"]))
            maximum_stop = Decimal(str(configuration["maxStopLossPercentage"]))
            minimum_take = Decimal(str(configuration["minTakeProfitPercentage"]))
            maximum_take = Decimal(str(configuration["maxTakeProfitPercentage"]))
        except (KeyError, InvalidOperation, TypeError) as exc:
            raise PermissionError("broker stop/take bounds are incomplete") from exc
        if not minimum_stop <= stop_percentage <= maximum_stop:
            raise PermissionError("sealed stop-loss is outside broker bounds")
        if not minimum_take <= take_percentage <= maximum_take:
            raise PermissionError("sealed take-profit is outside broker bounds")
        return {
            "instrument_id": instrument.instrument_id,
            "symbol": symbol,
            "direction": direction,
            "leverage": leverage,
            "min_position_amount": str(configuration.get("minPositionAmount")),
            "stop_percentage": str(stop_percentage),
            "take_percentage": str(take_percentage),
            "quote_observed_at": quote_at.isoformat(),
            "quote_age_seconds": str(quote_age),
            "spread_fraction": str(spread_fraction),
        }

    def reconcile(self) -> dict[str, object]:
        self.client.verify_isolated_demo_execution_scope()
        return self._broker_snapshot()

    def execute(self, order: ApprovedOrder, verifier: OrderVerifier) -> MCPResult:
        if not verifier.verify(order):
            raise PermissionError("risk seal invalid or expired")
        is_close = order.route.startswith(
            "/api/v1/trading/execution/demo/market-close-orders/positions/"
        )
        if self._kill_active() and not is_close:
            raise PermissionError("kill switch does not permit new DEMO positions")
        envelope_hash = self._envelope_hash(order)
        self.audit.require_approval(order.proposal_id, envelope_hash)
        before = self.reconcile()
        body = json.loads(order.body_json)
        if is_close:
            position_id = int(order.route.rsplit("/", 1)[-1])
            if position_id not in before["position_ids"]:
                raise PermissionError("sealed close does not match broker truth")
            self.audit.append(
                "demo_pretrade_validation",
                {
                    "proposal_id": order.proposal_id,
                    "reduce_only": True,
                    "position_route_hash": canonical_hash(order.route),
                    "broker_before": before,
                },
            )
        else:
            if (
                int(before["broker_exposure_count"])
                >= verifier.limits.max_open_positions
            ):
                raise PermissionError(
                    "broker already reached the maximum position/order exposure"
                )
            eligibility_summary = self._validate_open_eligibility(body, verifier)
            costs = self.client.execute_read("/api/v2/trading/info/demo/costs", body=order.body_json)
            if not costs.is_success:
                raise PermissionError("DEMO cost preview failed; order not sent")
            self.audit.append(
                "demo_pretrade_validation",
                {
                    "proposal_id": order.proposal_id,
                    "eligibility": eligibility_summary,
                    "costs": costs.body,
                    "broker_before": before,
                },
            )
        if not verifier.verify(order):
            raise PermissionError("risk seal expired during DEMO preflight")
        if self._kill_active() and not is_close:
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
        if state is ExecutionState.ACKNOWLEDGED:
            try:
                after = self._broker_snapshot()
                self.audit.append(
                    "demo_broker_reconciled",
                    {
                        "proposal_id": order.proposal_id,
                        "before": before,
                        "after": after,
                    },
                )
            except Exception as exc:
                self.audit.set_kill_state(
                    KillState.LOCKED,
                    "demo-executor",
                    "post-write broker reconciliation failed",
                )
                self.audit.append(
                    "demo_broker_reconciliation_failed",
                    {
                        "proposal_id": order.proposal_id,
                        "error_type": type(exc).__name__,
                    },
                )
        return result
