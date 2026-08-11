from __future__ import annotations

import hashlib
import json
import os
import socket
import time
from collections.abc import Mapping
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from .config_v2 import AppConfigV2
from .domain_v2 import (
    BPS,
    OrderStatus,
    QuoteProvenance,
    Side,
    canonical_hash,
    reduce_command_provenance_hash,
)
from .etoro_api_current_v2 import (
    EtoroPublicApiDemoClientV2,
    PreparedDemoCloseV2,
    PreparedDemoOpenV2,
)
from .execution_gate_v2 import execution_gate_path, execution_gate_present
from .kernel_v2 import UnifiedTradingKernel
from .risk_seal_v2 import RiskCommandVerifierV2
from .runtime_store_v2 import RuntimeStoreV2
from .systemd_notify_v2 import ready, watchdog


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
        verifier: RiskCommandVerifierV2 | None = None,
        execution_gate: Path | None = None,
    ) -> None:
        if not config.live_demo_execution_enabled:
            raise PermissionError("v2 live DEMO execution is disabled")
        self.config = config
        self.store = store
        self.kernel = kernel
        self.client = client or EtoroPublicApiDemoClientV2()
        self.client.verify_isolated_demo_execution_scope()
        self.verifier = verifier or kernel.command_verifier()
        self.execution_gate = execution_gate or execution_gate_path()
        if not execution_gate_present(self.execution_gate):
            self.store.lock_and_invalidate_unstarted(
                actor="v2-demo-executor",
                reason="execution gate absent during executor initialization",
            )
            raise PermissionError("v2 DEMO execution gate is absent")
        self.worker_id = os.getenv(
            "ETORO_V2_EXECUTOR_WORKER_ID",
            f"{socket.gethostname()}:{os.getpid()}",
        )

    def _gate_allows_execution(self, stage: str) -> bool:
        if execution_gate_present(self.execution_gate):
            return True
        self.store.lock_and_invalidate_unstarted(
            actor="v2-demo-executor",
            reason=f"execution gate absent at {stage}",
        )
        return False

    @staticmethod
    def _rate_row(response: object, instrument_id: int) -> Mapping[str, Any]:
        body = getattr(response, "body", None)
        rows = body.get("rates", []) if isinstance(body, dict) else []
        if len(rows) != 1 or not isinstance(rows[0], dict):
            raise RuntimeError("fresh broker quote is unavailable")
        row = rows[0]
        raw_instrument = row.get("instrumentID", row.get("instrumentId", instrument_id))
        if raw_instrument is None or int(raw_instrument) != instrument_id:
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
        now = datetime.now(UTC)
        if now > command.expires_at:
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
        return cash, quote

    def execute_outbox_item(self, item: Mapping[str, Any]) -> bool:
        if not self._gate_allows_execution("after_claim"):
            return False
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

        current = datetime.now(UTC)
        if current > command.expires_at:
            self.kernel.reject_before_send(
                command_id,
                at=current,
                reason="expired sealed command",
            )
            self._halt_new_if_active("expired command rejected before broker send")
            self.store.mark_outbox_delivered(str(item["outbox_id"]), claim_token, current)
            return False
        if order.client_order_id != command.client_order_id or not self.verifier.verify(
            command, now=current
        ):
            self.kernel.reject_before_send(
                command_id,
                at=current,
                reason="invalid risk seal or command identity",
            )
            self.store.set_trading_state(
                "LOCKED",
                actor="v2-demo-executor",
                reason="persisted command failed deterministic risk-seal verification",
            )
            self.store.mark_outbox_delivered(str(item["outbox_id"]), claim_token, current)
            return False
        provenance_valid = True
        provenance_reason = ""
        if command.reduce_only:
            positions = [
                position
                for position in self.store.positions(command.portfolio_id, open_only=True)
                if position.position_id == command.correlation_id
                and position.symbol == command.symbol
                and position.broker_position_id == command.broker_position_id
            ]
            if len(positions) != 1:
                provenance_valid = False
                provenance_reason = "reduce position binding mismatch"
            else:
                position = positions[0]
                current_position_hash = canonical_hash(asdict(position))
                expected_reduce_hash = reduce_command_provenance_hash(
                    position_hash=current_position_hash,
                    broker_position_id=str(command.broker_position_id),
                    quantity_before=position.quantity,
                    units=cast(Decimal, command.quantity),
                    exit_reason=command.reduce_exit_reason,
                    broker_snapshot_hash=command.reduce_broker_snapshot_hash,
                    risk_config_hash=command.risk_config_hash,
                )
                provenance_valid = (
                    command.reduce_position_hash == current_position_hash
                    and command.reduce_position_quantity == position.quantity
                    and command.reduce_provenance_hash == expected_reduce_hash
                )
                provenance_reason = "signed reduce provenance mismatch"
        else:
            intent = self.store.intent(command.intent_id)
            provenance_valid = command.intent_hash == canonical_hash(asdict(intent))
            provenance_reason = "signed intent hash mismatch"
        if not provenance_valid:
            self.kernel.reject_before_send(
                command_id,
                at=current,
                reason=provenance_reason,
            )
            self.store.set_trading_state(
                "LOCKED",
                actor="v2-demo-executor",
                reason="persisted command failed signed provenance verification",
            )
            self.store.mark_outbox_delivered(str(item["outbox_id"]), claim_token, current)
            return False

        prepared: Mapping[str, Any]
        quote: QuoteProvenance | None
        preparation: PreparedDemoOpenV2 | None = None
        close_preparation: PreparedDemoCloseV2 | None = None
        try:
            if command.reduce_only:
                if command.broker_position_id is None or not command.broker_position_id.isdigit():
                    raise PermissionError("reduce-only command lacks numeric broker position id")
                quote = None
                close_preparation = self.client.prepare_close_position(
                    position_id=int(command.broker_position_id),
                    units_to_deduct=command.units_to_deduct,
                )
                if not isinstance(close_preparation, PreparedDemoCloseV2):
                    raise TypeError("DEMO close preparation lacks broker-bound evidence")
                if (
                    close_preparation.broker_position_id != command.broker_position_id
                    or close_preparation.instrument_id != self.config.symbols[command.symbol]
                    or close_preparation.quantity_before != command.reduce_position_quantity
                ):
                    raise PermissionError("fresh broker position differs from signed reduce state")
                prepared = close_preparation.body
            else:
                _, quote = self._preflight_open(command_id)
                entry = quote.ask if command.side is Side.BUY else quote.bid
                stop_fraction = cast(Decimal, command.stop_loss_fraction)
                take_fraction = cast(Decimal, command.take_profit_fraction)
                stop = entry * (
                    Decimal("1") - stop_fraction
                    if command.side is Side.BUY
                    else Decimal("1") + stop_fraction
                )
                take = entry * (
                    Decimal("1") + take_fraction
                    if command.side is Side.BUY
                    else Decimal("1") - take_fraction
                )
                preparation = self.client.prepare_open_by_amount(
                    instrument_id=self.config.symbols[command.symbol],
                    amount_usd=command.amount_usd,
                    is_buy=command.side is Side.BUY,
                    leverage=1,
                    entry_rate=entry,
                    stop_loss_rate=stop,
                    take_profit_rate=take,
                )
                if not isinstance(preparation, PreparedDemoOpenV2):
                    raise TypeError("DEMO open preparation lacks cost-bound evidence")
                fresh_risk = self.kernel.risk.evaluate_fresh_open(
                    command,
                    quote,
                    known_cost_usd=preparation.total_cost_usd,
                    now=datetime.now(UTC),
                )
                if not fresh_risk.approved:
                    raise PermissionError("fresh deterministic risk rejected")
                prepared = preparation.body
        except (PermissionError, ValueError) as exc:
            self.kernel.reject_before_send(
                command_id,
                at=datetime.now(UTC),
                reason=type(exc).__name__,
            )
            self._halt_new_if_active("deterministic broker preflight rejected")
            self.store.mark_outbox_delivered(str(item["outbox_id"]), claim_token, datetime.now(UTC))
            return False
        except Exception as exc:
            self.store.release_outbox_claim(
                str(item["outbox_id"]),
                claim_token,
                error_type=type(exc).__name__,
            )
            raise

        preflight_evidence: Mapping[str, object] | None = None
        if command.reduce_only and close_preparation is not None:
            preflight_evidence = {
                "broker_position_id": close_preparation.broker_position_id,
                "instrument_id": close_preparation.instrument_id,
                "quantity_before": str(close_preparation.quantity_before),
                "units_to_deduct": None
                if command.units_to_deduct is None
                else str(command.units_to_deduct),
                "exit_reason": command.reduce_exit_reason,
                "broker_snapshot_hash": close_preparation.broker_snapshot_hash,
                "reduce_provenance_hash": command.reduce_provenance_hash,
            }
        elif quote is not None and preparation is not None:
            stop_fraction = cast(Decimal, command.stop_loss_fraction)
            slippage_bps = cast(Decimal, command.max_slippage_bps)
            worst_case_loss = (
                command.amount_usd * stop_fraction
                + command.amount_usd * slippage_bps / BPS
                + preparation.total_cost_usd
            )
            preflight_evidence = {
                "quote": asdict(quote),
                "entry_rate": str(preparation.entry_rate),
                "total_cost_usd": str(preparation.total_cost_usd),
                "cost_snapshot_hash": preparation.cost_snapshot_hash,
                "worst_case_loss_usd": str(worst_case_loss),
                "max_loss_usd": str(command.max_loss_usd),
            }
        if not self._gate_allows_execution("before_begin_submit"):
            return False
        self.kernel.begin_submit(
            command_id,
            datetime.now(UTC),
            preflight_evidence=preflight_evidence,
        )
        if not self._gate_allows_execution("before_broker_request"):
            current = datetime.now(UTC)
            self.kernel.reject_before_send(
                command_id,
                at=current,
                reason="execution gate removed before broker request",
            )
            self.store.mark_outbox_delivered(str(item["outbox_id"]), claim_token, current)
            return False
        try:
            if command.reduce_only:
                if command.broker_position_id is None:
                    raise ValueError("reduce command lacks broker position identity")
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
                current_order = self.store.broker_order(command_id)
                rejected = self.kernel.oms.reject(
                    current_order, datetime.now(UTC), f"HTTP_{response.status_code}"
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

    def _halt_new_if_active(self, reason: str) -> None:
        """Never weaken REDUCE_ONLY/LOCKED while stopping new exposure."""

        if self.store.state_get("trading_state", "LOCKED") == "ACTIVE":
            self.store.set_trading_state(
                "HALT_NEW",
                actor="v2-demo-executor",
                reason=reason,
            )

    def _run_once(self, limit: int = 20) -> int:
        if not self._gate_allows_execution("iteration_start"):
            return 0
        processed = 0
        for item in self.store.claim_outbox(
            self.worker_id,
            now=datetime.now(UTC),
            limit=limit,
        ):
            if not self._gate_allows_execution("claimed_item"):
                break
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
        ready()
        while True:
            try:
                self.run_once()
                watchdog()
            except Exception as exc:
                print(
                    f"V2_EXECUTOR_ERROR={type(exc).__name__}",
                    flush=True,
                )
            time.sleep(interval_seconds)
