from __future__ import annotations

import hashlib
import json
import os
import socket
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from .config_v2 import AppConfigV2
from .domain_v2 import OrderStatus, QuoteProvenance, Side
from .etoro_api_current_v2 import EtoroPublicApiDemoClientV2
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
        self.worker_id = os.getenv(
            "ETORO_V2_EXECUTOR_WORKER_ID",
            f"{socket.gethostname()}:{os.getpid()}",
        )

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
            return datetime.fromtimestamp(seconds, UTC)
        value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if value.tzinfo is None:
            raise RuntimeError("fresh broker quote timestamp is not timezone-aware")
        return value.astimezone(UTC)

    def _preflight_open(self, command_id: str) -> tuple[object, QuoteProvenance]:
        command = self.store.order_command(command_id)
        intent = self.store.intent(command.intent_id)
        now = datetime.now(UTC)
        if now > command.expires_at or not intent.is_live(now):
            raise PermissionError("order/intent expired before broker send")
        if self.store.state_get("trading_state", "LOCKED") != "ACTIVE":
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
        if intent.drift_bps(quote) > min(
            intent.max_price_drift_bps, self.config.mandate.max_mid_drift_bps
        ):
            raise PermissionError("fresh broker price drift exceeds intent boundary")
        return cash, quote

    def execute_outbox_item(self, item: Mapping[str, Any]) -> bool:
        if item.get("topic") != "broker.submit":
            raise ValueError("unsupported outbox topic")
        claim_token = str(item.get("claim_token", ""))
        if not claim_token:
            raise PermissionError("outbox item is not leased")
        command_id = str(item["payload"]["order_command_id"])
        command = self.store.order_command(command_id)
        order = self.store.broker_order(command_id)
        if order.status not in {OrderStatus.RISK_APPROVED, OrderStatus.SUBMITTING}:
            self.store.mark_outbox_delivered(str(item["outbox_id"]), claim_token, datetime.now(UTC))
            return False
        if order.status is OrderStatus.SUBMITTING:
            self.kernel.mark_unknown(
                command_id,
                at=datetime.now(UTC),
                reason="orphaned submitting state requires reconciliation",
            )
            self.store.set_trading_state(
                "HALT_NEW",
                actor="v2-demo-executor",
                reason="orphaned submitting order requires reconciliation",
            )
            self.store.mark_outbox_delivered(str(item["outbox_id"]), claim_token, datetime.now(UTC))
            return False

        prepared: Mapping[str, Any]
        quote: QuoteProvenance | None
        try:
            if command.reduce_only:
                if command.broker_position_id is None or not command.broker_position_id.isdigit():
                    raise PermissionError("reduce-only command lacks numeric broker position id")
                quote = None
                prepared = self.client.prepare_close_position(
                    position_id=int(command.broker_position_id),
                    units_to_deduct=command.units_to_deduct,
                )
            else:
                _, quote = self._preflight_open(command_id)
                intent = self.store.intent(command.intent_id)
                stop = (
                    quote.bid * (Decimal("1") - intent.stop_loss_fraction)
                    if intent.side is Side.BUY
                    else quote.ask * (Decimal("1") + intent.stop_loss_fraction)
                )
                take = (
                    quote.ask * (Decimal("1") + intent.take_profit_fraction)
                    if intent.side is Side.BUY
                    else quote.bid * (Decimal("1") - intent.take_profit_fraction)
                )
                prepared = self.client.prepare_open_by_amount(
                    instrument_id=self.config.symbols[command.symbol],
                    amount_usd=command.amount_usd,
                    is_buy=intent.side is Side.BUY,
                    leverage=1,
                    stop_loss_rate=stop,
                    take_profit_rate=take,
                )
        except (PermissionError, ValueError) as exc:
            self.kernel.reject_before_send(
                command_id,
                at=datetime.now(UTC),
                reason=type(exc).__name__,
            )
            self.store.set_trading_state(
                "HALT_NEW",
                actor="v2-demo-executor",
                reason="deterministic broker preflight rejected",
            )
            self.store.mark_outbox_delivered(str(item["outbox_id"]), claim_token, datetime.now(UTC))
            return False
        except Exception as exc:
            self.store.release_outbox_claim(
                str(item["outbox_id"]),
                claim_token,
                error_type=type(exc).__name__,
            )
            raise

        self.kernel.begin_submit(command_id, datetime.now(UTC))
        try:
            if command.reduce_only:
                response = self.client.submit_prepared_close(
                    position_id=int(command.broker_position_id),
                    body=prepared,
                    request_id=command.client_order_id,
                )
            else:
                response = self.client.submit_prepared_open(
                    prepared,
                    request_id=command.client_order_id,
                )
        except Exception as exc:
            self.kernel.mark_unknown(
                command_id,
                at=datetime.now(UTC),
                reason=type(exc).__name__,
            )
            self.store.set_trading_state(
                "HALT_NEW",
                actor="v2-demo-executor",
                reason="broker write outcome is unknown",
            )
            self.store.mark_outbox_delivered(str(item["outbox_id"]), claim_token, datetime.now(UTC))
            raise
        if not response.ok:
            if response.status_code == 429 or response.status_code >= 500:
                self.kernel.mark_unknown(
                    command_id, at=datetime.now(UTC), reason=f"HTTP_{response.status_code}"
                )
                self.store.set_trading_state(
                    "HALT_NEW",
                    actor="v2-demo-executor",
                    reason="broker HTTP response requires reconciliation",
                )
                self.store.mark_outbox_delivered(
                    str(item["outbox_id"]),
                    claim_token,
                    datetime.now(UTC),
                )
            else:
                current = self.store.broker_order(command_id)
                rejected = self.kernel.oms.reject(
                    current, datetime.now(UTC), f"HTTP_{response.status_code}"
                )
                from .kernel_v2 import _event

                self.store.save_broker_order(
                    rejected,
                    _event(
                        "OrderRejected",
                        idempotency_key=f"rejected:{command_id}:{response.status_code}",
                        event_time=datetime.now(UTC),
                        processing_time=datetime.now(UTC),
                        correlation_id=command.correlation_id,
                        causation_id=command_id,
                        payload={"status_code": response.status_code},
                    ),
                )
                self.store.mark_outbox_delivered(
                    str(item["outbox_id"]),
                    claim_token,
                    datetime.now(UTC),
                )
            return False
        body = response.body if isinstance(response.body, dict) else {}
        raw_order_id = body.get("orderId", body.get("orderID"))
        broker_position_id = body.get("positionId", body.get("positionID"))
        broker_order_id = str(raw_order_id or broker_position_id or "").strip()
        if not broker_order_id:
            self.kernel.mark_unknown(
                command_id,
                at=datetime.now(UTC),
                reason="broker success response lacks execution identity",
            )
            self.store.set_trading_state(
                "HALT_NEW",
                actor="v2-demo-executor",
                reason="broker success response requires reconciliation",
            )
            self.store.mark_outbox_delivered(str(item["outbox_id"]), claim_token, datetime.now(UTC))
            return False
        self.kernel.acknowledge(
            command_id,
            at=datetime.now(UTC),
            broker_order_id=broker_order_id,
            broker_position_id=None if broker_position_id is None else str(broker_position_id),
        )
        self.store.mark_outbox_delivered(str(item["outbox_id"]), claim_token, datetime.now(UTC))
        return True

    def _run_once(self, limit: int = 20) -> int:
        processed = 0
        for item in self.store.claim_outbox(
            self.worker_id,
            now=datetime.now(UTC),
            limit=limit,
        ):
            processed += int(self.execute_outbox_item(item))
        return processed

    def run_once(self, limit: int = 20) -> int:
        try:
            processed = self._run_once(limit)
        except Exception as exc:
            self.store.heartbeat(
                "v2-demo-executor",
                "error",
                {"error_type": type(exc).__name__, "real_money": False},
            )
            raise
        trading_state = self.store.state_get("trading_state", "LOCKED")
        self.store.heartbeat(
            "v2-demo-executor",
            "healthy" if trading_state == "ACTIVE" else "halted",
            {
                "orders_acknowledged": processed,
                "trading_state": trading_state,
                "real_money": False,
            },
        )
        return processed

    def run_forever(self, interval_seconds: int = 2) -> None:
        if interval_seconds < 1:
            raise ValueError("executor interval must be at least one second")
        while True:
            try:
                self.run_once()
            except Exception as exc:
                print(
                    f"V2_EXECUTOR_ERROR={type(exc).__name__}",
                    flush=True,
                )
            time.sleep(interval_seconds)
