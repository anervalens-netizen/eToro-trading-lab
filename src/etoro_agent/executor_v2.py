from __future__ import annotations

import hashlib
import json
import os
import socket
import time
from collections.abc import Mapping
from dataclasses import asdict, replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from .broker_truth_v2 import broker_truth_v2
from .candidates_v2 import canonical_candidate_engine
from .config_v2 import AppConfigV2
from .domain_v2 import (
    BPS,
    DomainEvent,
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
    decode_broker_rate_v2,
)
from .execution_gate_v2 import execution_gate_path, execution_gate_present
from .kernel_v2 import UnifiedTradingKernel
from .postgres_runtime_v2 import PostgresRuntimeStoreV2
from .risk_seal_v2 import RiskCommandVerifierV2
from .risk_v2 import BrokerTruth
from .runtime_store_v2 import RuntimeStoreV2
from .strategy_release_v2 import VerifiedStrategyReleaseV2, require_deployed_strategy_release
from .systemd_notify_v2 import ready, watchdog

OUTBOX_MAX_PRE_SUBMIT_ATTEMPTS = 3


class PreSubmitDispatchError(RuntimeError):
    """A classified failure that happened before any broker write was attempted."""

    def __init__(self, error_type: str, *, retryable: bool) -> None:
        super().__init__(error_type)
        self.error_type = error_type
        self.retryable = retryable


class DemoExecutionWorkerV2:
    """Idempotent DEMO outbox dispatcher.

    The worker owns broker credentials but never a risk signing key or an LLM. It
    accepts only commands already persisted by the deterministic kernel.
    """

    def __init__(
        self,
        config: AppConfigV2,
        store: RuntimeStoreV2 | PostgresRuntimeStoreV2,
        kernel: UnifiedTradingKernel,
        client: EtoroPublicApiDemoClientV2 | None = None,
        verifier: RiskCommandVerifierV2 | None = None,
        execution_gate: Path | None = None,
        require_strategy_release: bool = True,
    ) -> None:
        if not config.live_demo_execution_enabled:
            raise PermissionError("v2 live DEMO execution is disabled")
        if type(require_strategy_release) is not bool:
            raise TypeError("strategy release enforcement flag must be boolean")
        self.config = config
        self.store = store
        self.kernel = kernel
        self.client = client or EtoroPublicApiDemoClientV2()
        self.client.verify_isolated_demo_execution_scope()
        self.verifier = verifier or kernel.command_verifier()
        self.execution_gate = execution_gate or execution_gate_path()
        self.require_strategy_release = require_strategy_release
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

    def _trading_state_allows(self, *, reduce_only: bool) -> bool:
        state = self.store.state_get("trading_state", "LOCKED")
        if reduce_only:
            # LOCKED is the fail-closed lock-new state.  The independently
            # controlled execution gate is the manual emergency freeze: when
            # it is absent _gate_allows_execution rejects every broker write.
            # Keeping those two authorities separate ensures a drawdown or
            # reconciliation lock cannot strand an already-open position.
            return state in {"ACTIVE", "HALT_NEW", "REDUCE_ONLY", "LOCKED"}
        return state == "ACTIVE"

    def _reject_state_block(
        self,
        command_id: str,
        outbox_id: str,
        claim_token: str,
        *,
        reduce_only: bool,
        stage: str,
    ) -> bool:
        if self._trading_state_allows(reduce_only=reduce_only):
            return False
        current = datetime.now(UTC)
        self.kernel.reject_before_send(
            command_id,
            at=current,
            reason=f"trading state blocked DEMO command at {stage}",
        )
        self.store.mark_outbox_delivered(outbox_id, claim_token, current)
        return True

    @staticmethod
    def _validated_outbox_envelope(item: Mapping[str, Any]) -> tuple[str, str, str]:
        outbox_id = str(item.get("outbox_id", "")).strip()
        claim_token = str(item.get("claim_token", "")).strip()
        if not outbox_id or not claim_token:
            raise PreSubmitDispatchError("OutboxLeaseInvalid", retryable=False)
        if item.get("topic") != "broker.submit":
            raise PreSubmitDispatchError("OutboxTopicInvalid", retryable=False)
        payload = item.get("payload")
        if not isinstance(payload, Mapping):
            raise PreSubmitDispatchError("OutboxPayloadInvalid", retryable=False)
        if not set(payload) <= {"order_command_id", "execution_epoch"}:
            raise PreSubmitDispatchError("OutboxPayloadUnknownField", retryable=False)
        command_id = str(payload.get("order_command_id", "")).strip()
        if not command_id:
            raise PreSubmitDispatchError("OutboxCommandIdentityInvalid", retryable=False)
        execution_epoch = payload.get("execution_epoch")
        if execution_epoch is not None and (
            isinstance(execution_epoch, bool)
            or not isinstance(execution_epoch, int)
            or execution_epoch < 1
        ):
            raise PreSubmitDispatchError("OutboxExecutionEpochInvalid", retryable=False)
        return outbox_id, claim_token, command_id

    @staticmethod
    def _strict_success_identity(
        body: object,
        *,
        reduce_only: bool,
        expected_position_id: str | None,
    ) -> tuple[str, str | None]:
        if not isinstance(body, Mapping):
            raise ValueError("broker success response is not an object")

        def aliases(names: tuple[str, ...], label: str) -> str | None:
            present = [name for name in names if name in body]
            if not present:
                return None
            values: list[str] = []
            for name in present:
                raw = body[name]
                if isinstance(raw, bool) or not isinstance(raw, (str, int)):
                    raise ValueError(f"broker success {label} is invalid")
                value = str(raw).strip()
                if not value:
                    raise ValueError(f"broker success {label} is invalid")
                values.append(value)
            if any(value != values[0] for value in values[1:]):
                raise ValueError(f"broker success {label} aliases disagree")
            return values[0]

        order_id = aliases(("orderId", "orderID"), "order identity")
        position_id = aliases(("positionId", "positionID"), "position identity")
        if order_id is None:
            raise ValueError("broker success response lacks order identity")
        if reduce_only and position_id is None:
            raise ValueError("broker close success lacks position identity")
        if reduce_only and position_id != str(expected_position_id or "").strip():
            raise ValueError("broker close success position identity mismatches command")
        return order_id, position_id

    def _reject_before_quarantine(self, item: Mapping[str, Any], error_type: str) -> None:
        payload = item.get("payload")
        if not isinstance(payload, Mapping):
            return
        command_id = str(payload.get("order_command_id", "")).strip()
        if not command_id:
            return
        order = self.store.broker_order(command_id)
        if order.status is OrderStatus.RISK_APPROVED:
            self.kernel.reject_before_send(
                command_id,
                at=datetime.now(UTC),
                reason=f"outbox quarantined before broker send: {error_type}",
            )

    def _quarantine_pre_submit(
        self,
        item: Mapping[str, Any],
        error: PreSubmitDispatchError,
    ) -> None:
        outbox_id = str(item.get("outbox_id", "")).strip()
        claim_token = str(item.get("claim_token", "")).strip()
        attempt = int(item.get("attempt", 0) or 0)
        if not outbox_id or not claim_token or attempt < 1:
            raise RuntimeError("claimed outbox identity is unavailable for quarantine")
        try:
            self._reject_before_quarantine(item, error.error_type)
        except (KeyError, TypeError, ValueError):
            self.store.set_trading_state(
                "LOCKED",
                actor="v2-demo-executor",
                reason="quarantined outbox could not bind a deterministic command",
            )
        error_hash = hashlib.sha256(
            f"{outbox_id}:{attempt}:{error.error_type}".encode()
        ).hexdigest()
        quarantine = getattr(self.store, "quarantine_outbox", None)
        current = datetime.now(UTC)
        if callable(quarantine):
            quarantine(
                outbox_id,
                claim_token,
                error_type=error.error_type,
                error_hash=error_hash,
                at=current,
            )
            return

        # SQLite is a non-production compatibility store.  Preserve the same
        # terminal/audit semantics without expanding that legacy schema.
        self.store.mark_outbox_delivered(outbox_id, claim_token, current)
        key = f"outbox-quarantined:{outbox_id}:{attempt}"
        self.store.append_event(
            DomainEvent(
                event_id="evt-" + hashlib.sha256(key.encode()).hexdigest()[:24],
                event_type="OutboxQuarantined",
                schema_version=2,
                event_time=current,
                processing_time=current,
                idempotency_key=key,
                causation_id="",
                correlation_id=outbox_id,
                payload={
                    "outbox_id": outbox_id,
                    "attempt": attempt,
                    "error_type": error.error_type,
                    "error_hash": error_hash,
                    "network_write_attempted": False,
                    "manual_replay_requires_new_signed_command": True,
                },
            )
        )

    def _handle_pre_submit_failure(
        self,
        item: Mapping[str, Any],
        error: PreSubmitDispatchError,
    ) -> None:
        attempt = int(item.get("attempt", 0) or 0)
        if not error.retryable or attempt >= OUTBOX_MAX_PRE_SUBMIT_ATTEMPTS:
            self._quarantine_pre_submit(item, error)
            return
        self.store.release_outbox_claim(
            str(item["outbox_id"]),
            str(item["claim_token"]),
            error_type=error.error_type,
        )

    def _preflight_open(
        self, command_id: str
    ) -> tuple[BrokerTruth, QuoteProvenance, VerifiedStrategyReleaseV2 | None]:
        command = self.store.order_command(command_id)
        now = datetime.now(UTC)
        if now > command.expires_at:
            raise PermissionError("order/intent expired before broker send")
        if self.store.state_get("trading_state", "LOCKED") != "ACTIVE":
            raise PermissionError("trading state blocks new DEMO opens")
        strategy_release = None
        if self.require_strategy_release:
            engine = canonical_candidate_engine()
            strategy_release = require_deployed_strategy_release(
                engine,
                expected_feature_schema_hash=engine.feature_schema_hash,
                expected_cost_model_hash=engine.cost_model_hash,
                now=now,
            )
        snapshot = self.client.account_snapshot()
        broker = broker_truth_v2(
            self.store,
            self.client,
            config=self.config,
            now=now,
            snapshot=snapshot,
        )
        instrument_id = self.config.symbols[command.symbol]
        response = self.client.rates((instrument_id,))
        if not response.ok:
            raise RuntimeError("fresh eToro rate request failed")
        rate = decode_broker_rate_v2(response, instrument_id=instrument_id)
        row = rate.raw
        bid = rate.bid
        ask = rate.ask
        observed = rate.observed_at
        raw_sequence = rate.sequence_or_event_id
        if raw_sequence == "rest":
            provenance_source = "etoro-public-api-http-snapshot"
            provenance_event_id = f"http-request:{response.request_id}"
        else:
            provenance_source = "etoro-public-api-broker-sequence"
            provenance_event_id = f"broker-sequence:{raw_sequence}"
        quote = QuoteProvenance(
            command.symbol,
            bid,
            ask,
            observed,
            now,
            provenance_source,
            provenance_event_id,
            hashlib.sha256(json.dumps(dict(row), sort_keys=True, default=str).encode()).hexdigest(),
            snapshot.snapshot_hash,
        )
        intent = self.store.intent(command.intent_id)
        risk = self.kernel.risk.evaluate_open(intent, quote, broker, datetime.now(UTC))
        if not risk.approved:
            raise PermissionError("fresh full broker truth rejected DEMO OPEN")
        return broker, quote, strategy_release

    def execute_outbox_item(self, item: Mapping[str, Any]) -> bool:
        if not self._gate_allows_execution("after_claim"):
            return False
        outbox_id, claim_token, command_id = self._validated_outbox_envelope(item)
        try:
            command = self.store.order_command(command_id)
            order = self.store.broker_order(command_id)
        except (KeyError, TypeError, ValueError) as exc:
            raise PreSubmitDispatchError(type(exc).__name__, retryable=False) from exc
        except (OSError, RuntimeError) as exc:
            raise PreSubmitDispatchError(type(exc).__name__, retryable=True) from exc
        if order.status not in {OrderStatus.RISK_APPROVED, OrderStatus.SUBMITTING}:
            self.store.mark_outbox_delivered(outbox_id, claim_token, datetime.now(UTC))
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
            self.store.mark_outbox_delivered(outbox_id, claim_token, datetime.now(UTC))
            return False

        if self._reject_state_block(
            command_id,
            outbox_id,
            claim_token,
            reduce_only=command.reduce_only,
            stage="after_claim",
        ):
            return False

        current = datetime.now(UTC)
        if current > command.expires_at:
            self.kernel.reject_before_send(
                command_id,
                at=current,
                reason="expired sealed command",
            )
            self._halt_new_if_active("expired command rejected before broker send")
            self.store.mark_outbox_delivered(outbox_id, claim_token, current)
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
            self.store.mark_outbox_delivered(outbox_id, claim_token, current)
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
            self.store.mark_outbox_delivered(outbox_id, claim_token, current)
            return False

        prepared: Mapping[str, Any]
        quote: QuoteProvenance | None
        preparation: PreparedDemoOpenV2 | None = None
        close_preparation: PreparedDemoCloseV2 | None = None
        strategy_release: VerifiedStrategyReleaseV2 | None = None
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
                _, quote, strategy_release = self._preflight_open(command_id)
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
                final_snapshot = self.client.account_snapshot()
                final_broker = broker_truth_v2(
                    self.store,
                    self.client,
                    config=self.config,
                    now=datetime.now(UTC),
                    snapshot=final_snapshot,
                )
                quote = replace(quote, broker_snapshot_hash=final_snapshot.snapshot_hash)
                fresh_full_risk = self.kernel.risk.evaluate_open(
                    self.store.intent(command.intent_id),
                    quote,
                    final_broker,
                    datetime.now(UTC),
                )
                if not fresh_full_risk.approved:
                    raise PermissionError("final full broker truth rejected DEMO OPEN")
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
            self.store.mark_outbox_delivered(outbox_id, claim_token, datetime.now(UTC))
            return False
        except Exception as exc:
            raise PreSubmitDispatchError(
                type(exc).__name__,
                retryable=isinstance(exc, (OSError, RuntimeError)),
            ) from exc

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
                "broker_request_body_sha256": close_preparation.request_body_sha256,
                "broker_quantity_rules_hash": close_preparation.quantity_rules_hash,
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
                "broker_request_body_sha256": preparation.request_body_sha256,
                "broker_account_snapshot_hash": quote.broker_snapshot_hash,
                "strategy_release_id": (
                    None if strategy_release is None else strategy_release.strategy_release_id
                ),
                "strategy_release_manifest_hash": (
                    None if strategy_release is None else strategy_release.manifest_hash
                ),
                "worst_case_loss_usd": str(worst_case_loss),
                "max_loss_usd": str(command.max_loss_usd),
            }
        if not self._gate_allows_execution("before_begin_submit"):
            return False
        if self._reject_state_block(
            command_id,
            outbox_id,
            claim_token,
            reduce_only=command.reduce_only,
            stage="before_begin_submit",
        ):
            return False
        try:
            self._acquire_write_budget(close_priority=command.reduce_only)
        except Exception as exc:
            raise PreSubmitDispatchError(
                type(exc).__name__,
                retryable=isinstance(exc, (OSError, RuntimeError, TimeoutError)),
            ) from exc
        if not self._gate_allows_execution("after_rate_budget"):
            return False
        if self._reject_state_block(
            command_id,
            outbox_id,
            claim_token,
            reduce_only=command.reduce_only,
            stage="after_rate_budget",
        ):
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
            self.store.mark_outbox_delivered(outbox_id, claim_token, current)
            return False
        if self._reject_state_block(
            command_id,
            outbox_id,
            claim_token,
            reduce_only=command.reduce_only,
            stage="before_broker_request",
        ):
            return False
        try:
            if command.reduce_only:
                if command.broker_position_id is None:
                    raise ValueError("reduce command lacks broker position identity")
                response = self.client.submit_prepared_close(
                    position_id=int(command.broker_position_id),
                    body=prepared,
                    request_id=command.client_order_id,
                    write_budget_acquired=True,
                )
            else:
                response = self.client.submit_prepared_open(
                    prepared,
                    request_id=command.client_order_id,
                    write_budget_acquired=True,
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
            self.store.mark_outbox_delivered(outbox_id, claim_token, datetime.now(UTC))
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
                    outbox_id,
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
                    outbox_id,
                    claim_token,
                    datetime.now(UTC),
                )
            return False
        try:
            broker_order_id, broker_position_id = self._strict_success_identity(
                response.body,
                reduce_only=command.reduce_only,
                expected_position_id=command.broker_position_id,
            )
        except ValueError as exc:
            self.kernel.mark_unknown(
                command_id,
                at=datetime.now(UTC),
                reason=str(exc),
            )
            self.store.set_trading_state(
                "HALT_NEW",
                actor="v2-demo-executor",
                reason="broker success response requires reconciliation",
            )
            self.store.mark_outbox_delivered(outbox_id, claim_token, datetime.now(UTC))
            return False
        self.kernel.acknowledge(
            command_id,
            at=datetime.now(UTC),
            broker_order_id=broker_order_id,
            broker_position_id=broker_position_id,
        )
        self.store.mark_outbox_delivered(outbox_id, claim_token, datetime.now(UTC))
        return True

    def _halt_new_if_active(self, reason: str) -> None:
        """Never weaken REDUCE_ONLY/LOCKED while stopping new exposure."""

        if self.store.state_get("trading_state", "LOCKED") == "ACTIVE":
            self.store.set_trading_state(
                "HALT_NEW",
                actor="v2-demo-executor",
                reason=reason,
            )

    def _acquire_write_budget(self, *, close_priority: bool) -> None:
        acquire = getattr(self.client, "acquire_demo_write_budget", None)
        if callable(acquire):
            acquire(close_priority=close_priority)
            return
        # Test/compatibility clients without an embedded limiter may opt out;
        # the production client always exposes this explicit pre-submit boundary.
        if isinstance(self.client, EtoroPublicApiDemoClientV2):
            raise RuntimeError("production broker client lacks write-budget authority")

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
            try:
                processed += int(self.execute_outbox_item(item))
            except PreSubmitDispatchError as exc:
                self._handle_pre_submit_failure(item, exc)
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
            (
                "healthy"
                if trading_state in {"ACTIVE", "HALT_NEW", "REDUCE_ONLY", "LOCKED"}
                else "halted"
            ),
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
