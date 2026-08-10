from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping

from .config_v2 import AppConfigV2
from .domain_v2 import OrderStatus, QuoteProvenance, Side
from .etoro_api_v2 import EtoroPublicApiDemoClientV2
from .kernel_v2 import UnifiedTradingKernel
from .runtime_store_v2 import RuntimeStoreV2


class DemoExecutionWorkerV2:
    """Idempotent DEMO outbox dispatcher.

    The worker owns broker credentials but never a risk signing key or an LLM. It
    accepts only commands already persisted by the deterministic kernel.
    """

    def __init__(
        self,
        config: AppConfigV2,
        store: RuntimeStoreV2,
        kernel: UnifiedTradingKernel,
        client: EtoroPublicApiDemoClientV2 | None = None,
    ) -> None:
        if not config.live_demo_execution_enabled:
            raise PermissionError("v2 live DEMO execution is disabled")
        self.config = config
        self.store = store
        self.kernel = kernel
        self.client = client or EtoroPublicApiDemoClientV2()

    @staticmethod
    def _rate_row(response: object, instrument_id: int) -> Mapping[str, Any]:
        body = getattr(response, "body", None)
        rows = body.get("rates", []) if isinstance(body, dict) else []
        if len(rows) != 1 or not isinstance(rows[0], dict):
            raise RuntimeError("fresh broker quote is unavailable")
        row = rows[0]
        if int(row.get("instrumentID", row.get("instrumentId", instrument_id))) != instrument_id:
            raise RuntimeError("fresh broker quote instrument mismatch")
        return row

    @staticmethod
    def _quote_time(row: Mapping[str, Any]) -> datetime:
        raw = row.get("date", row.get("timestamp"))
        if raw is None:
            raise RuntimeError("fresh broker quote lacks provenance time")
        if isinstance(raw, (int, float)):
            seconds = float(raw) / 1000 if float(raw) > 10_000_000_000 else float(raw)
            return datetime.fromtimestamp(seconds, timezone.utc)
        value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if value.tzinfo is None:
            raise RuntimeError("fresh broker quote timestamp is not timezone-aware")
        return value.astimezone(timezone.utc)

    def _preflight_open(self, command_id: str) -> tuple[object, QuoteProvenance]:
        command = self.store.order_command(command_id)
        intent = self.store.intent(command.intent_id)
        now = datetime.now(timezone.utc)
        if now > command.expires_at or not intent.is_live(now):
            raise PermissionError("order/intent expired before broker send")
        if self.store.state_get("trading_state", "ACTIVE") != "ACTIVE":
            raise PermissionError("trading state blocks new DEMO opens")
        cash = self.client.cash_truth()
        if cash.available_cash_usd < command.amount_usd:
            raise PermissionError("fresh broker cash is below sealed order amount")
        instrument_id = self.config.symbols[command.symbol]
        response = self.client.rates((instrument_id,))
        if not response.ok:
            raise RuntimeError("fresh eToro rate request failed")
        row = self._rate_row(response, instrument_id)
        bid = Decimal(str(row["bid"]))
        ask = Decimal(str(row["ask"]))
        observed = self._quote_time(row)
        quote = QuoteProvenance(
            command.symbol,
            bid,
            ask,
            observed,
            now,
            "etoro-public-api-preflight",
            str(row.get("sequence", response.request_id)),
            hashlib.sha256(json.dumps(dict(row), sort_keys=True, default=str).encode()).hexdigest(),
            cash.snapshot_hash,
        )
        if quote.age_seconds(now) > Decimal(self.config.mandate.max_quote_age_seconds):
            raise PermissionError("fresh broker quote is stale")
        if quote.spread_bps > self.config.mandate.max_spread_bps:
            raise PermissionError("fresh broker spread exceeds mandate")
        if intent.drift_bps(quote) > min(intent.max_price_drift_bps, self.config.mandate.max_mid_drift_bps):
            raise PermissionError("fresh broker price drift exceeds intent boundary")
        return cash, quote

    def execute_outbox_item(self, item: Mapping[str, Any]) -> bool:
        if item.get("topic") != "broker.submit":
            return False
        command_id = str(item["payload"]["order_command_id"])
        command = self.store.order_command(command_id)
        order = self.store.broker_order(command_id)
        if order.status not in {OrderStatus.RISK_APPROVED, OrderStatus.SUBMITTING}:
            self.store.mark_outbox_delivered(str(item["outbox_id"]), datetime.now(timezone.utc))
            return False
        if order.status is OrderStatus.RISK_APPROVED:
            self.kernel.begin_submit(command_id, datetime.now(timezone.utc))
        try:
            if command.reduce_only:
                if command.broker_position_id is None or not command.broker_position_id.isdigit():
                    raise PermissionError("reduce-only command lacks numeric broker position id")
                response = self.client.close_position(
                    position_id=int(command.broker_position_id),
                    units_to_deduct=command.units_to_deduct,
                    request_id=command.client_order_id,
                )
            else:
                _, quote = self._preflight_open(command_id)
                instrument_id = self.config.symbols[command.symbol]
                intent = self.store.intent(command.intent_id)
                stop = quote.bid * (Decimal("1") - intent.stop_loss_fraction) if intent.side is Side.BUY else quote.ask * (Decimal("1") + intent.stop_loss_fraction)
                take = quote.ask * (Decimal("1") + intent.take_profit_fraction) if intent.side is Side.BUY else quote.bid * (Decimal("1") - intent.take_profit_fraction)
                response = self.client.open_by_amount(
                    instrument_id=instrument_id,
                    amount_usd=command.amount_usd,
                    is_buy=intent.side is Side.BUY,
                    leverage=1,
                    request_id=command.client_order_id,
                    stop_loss_rate=stop,
                    take_profit_rate=take,
                )
        except Exception as exc:
            self.kernel.mark_unknown(command_id, at=datetime.now(timezone.utc), reason=type(exc).__name__)
            self.store.state_set("trading_state", "HALT_NEW")
            raise
        if not response.ok:
            if response.status_code == 429 or response.status_code >= 500:
                self.kernel.mark_unknown(command_id, at=datetime.now(timezone.utc), reason=f"HTTP_{response.status_code}")
                self.store.state_set("trading_state", "HALT_NEW")
            else:
                current = self.store.broker_order(command_id)
                rejected = self.kernel.oms.reject(current, datetime.now(timezone.utc), f"HTTP_{response.status_code}")
                from .kernel_v2 import _event
                self.store.save_broker_order(
                    rejected,
                    _event(
                        "OrderRejected",
                        idempotency_key=f"rejected:{command_id}:{response.status_code}",
                        event_time=datetime.now(timezone.utc),
                        processing_time=datetime.now(timezone.utc),
                        correlation_id=command.correlation_id,
                        causation_id=command_id,
                        payload={"status_code": response.status_code},
                    ),
                )
                self.store.mark_outbox_delivered(str(item["outbox_id"]), datetime.now(timezone.utc))
            return False
        body = response.body if isinstance(response.body, dict) else {}
        broker_order_id = str(
            body.get("orderId", body.get("orderID", body.get("positionId", response.request_id)))
        )
        broker_position_id = body.get("positionId", body.get("positionID"))
        self.kernel.acknowledge(
            command_id,
            at=datetime.now(timezone.utc),
            broker_order_id=broker_order_id,
            broker_position_id=None if broker_position_id is None else str(broker_position_id),
        )
        self.store.mark_outbox_delivered(str(item["outbox_id"]), datetime.now(timezone.utc))
        return True

    def run_once(self, limit: int = 20) -> int:
        processed = 0
        for item in self.store.pending_outbox(limit):
            processed += int(self.execute_outbox_item(item))
        return processed

    def run_forever(self, interval_seconds: int = 2) -> None:
        if interval_seconds < 1:
            raise ValueError("executor interval must be at least one second")
        while True:
            try:
                self.run_once()
            except Exception:
                pass
            time.sleep(interval_seconds)
