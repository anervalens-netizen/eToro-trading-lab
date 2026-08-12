from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from etoro_agent.config_v2 import load_config_v2
from etoro_agent.domain_v2 import (
    ExitReason,
    Fill,
    IntentEnvelope,
    OrderStatus,
    QuoteProvenance,
    Side,
)
from etoro_agent.etoro_api_current_v2 import (
    ApiResponse,
    BrokerAccountSnapshotV2,
    EtoroPublicApiDemoClientV2,
    PreparedDemoCloseV2,
    PreparedDemoOpenV2,
)
from etoro_agent.executor_v2 import DemoExecutionWorkerV2
from etoro_agent.kernel_v2 import UnifiedTradingKernel
from etoro_agent.risk_v2 import BrokerTruth, GlobalRiskKernel
from etoro_agent.runtime_store_v2 import RuntimeStoreV2


def setup_open(
    store: RuntimeStoreV2,
    now: datetime,
    *,
    bind_execution_epoch: bool = True,
):
    config = load_config_v2("config/v2-demo-execution.json")
    kernel = UnifiedTradingKernel(store, GlobalRiskKernel(config.mandate))
    intent = IntentEnvelope(
        "intent-executor",
        "master_1000",
        "D",
        "test",
        "v2",
        "AAPL",
        Side.BUY,
        Decimal("100"),
        Decimal("0.8"),
        Decimal("0.6"),
        Decimal("0.02"),
        Decimal("0.04"),
        3600,
        now,
        now,
        now + timedelta(minutes=5),
        Decimal("99.9"),
        Decimal("100"),
        Decimal("50"),
        Decimal("25"),
        "market",
        correlation_id="packet-executor",
    )
    quote = QuoteProvenance(
        "AAPL",
        Decimal("99.9"),
        Decimal("100"),
        now,
        now,
        "test",
        "quote-1",
        "market",
        "broker",
    )
    broker = BrokerTruth(
        Decimal("1000"),
        Decimal("1000"),
        Decimal("1000"),
        Decimal("0"),
        Decimal("0"),
        0,
        Decimal("0"),
        Decimal("0"),
        Decimal("0"),
        Decimal("0"),
        "broker",
        now,
    )
    execution_epoch = None
    if bind_execution_epoch:
        store.set_trading_state(
            "ACTIVE",
            actor="test",
            reason="bind executor fixture to execution authority",
            at=now,
        )
        execution_epoch = int(store.trading_state_snapshot()["version"])
    risk, command = kernel.submit_open_intent(
        intent,
        quote,
        broker,
        now=now,
        required_trading_state_version=execution_epoch,
    )
    assert risk.approved and command is not None
    return config, kernel, command


class NoCallClient:
    def verify_isolated_demo_execution_scope(self):
        return {"scopes": ["etoro-public:trade.demo:read", "etoro-public:trade.demo:write"]}

    def __getattr__(self, name: str):
        raise AssertionError(f"broker client must not be called: {name}")


class PreparedWriteClient:
    def __init__(
        self,
        *,
        prepare_error: bool = False,
        response: ApiResponse | None = None,
        total_cost_usd: Decimal = Decimal("0"),
        positions: tuple[dict[str, object], ...] = (),
        open_orders: tuple[dict[str, object], ...] = (),
        pending_orders: tuple[dict[str, object], ...] = (),
        gross_exposure_usd: Decimal = Decimal("0"),
        foreign_activity: tuple[str, ...] = (),
        write_budget_error: Exception | None = None,
    ) -> None:
        self.prepare_error = prepare_error
        self.response = response
        self.total_cost_usd = total_cost_usd
        self.positions = positions
        self.open_orders = open_orders
        self.pending_orders = pending_orders
        self.gross_exposure_usd = gross_exposure_usd
        self.foreign_activity = foreign_activity
        self.write_budget_error = write_budget_error
        self.submit_calls = 0
        self.prepared_kwargs = None
        self.write_budget_calls: list[bool] = []

    def verify_isolated_demo_execution_scope(self):
        return {"scopes": ["etoro-public:trade.demo:read", "etoro-public:trade.demo:write"]}

    def acquire_demo_write_budget(self, *, close_priority: bool) -> None:
        self.write_budget_calls.append(close_priority)
        if self.write_budget_error is not None:
            raise self.write_budget_error

    def account_snapshot(self) -> BrokerAccountSnapshotV2:
        now = datetime.now(UTC)
        manual = sum((Decimal(str(item.get("amount", 0))) for item in self.open_orders), Decimal(0))
        pending = sum(
            (Decimal(str(item.get("amount", 0))) for item in self.pending_orders), Decimal(0)
        )
        return BrokerAccountSnapshotV2(
            schema_version="test-v1",
            request_id="account-request",
            snapshot_hash="a" * 64,
            requested_at=now,
            received_at=now,
            broker_observed_at=now,
            observed_at=now,
            credit_usd=Decimal("1000"),
            available_cash_usd=Decimal("1000") - manual - pending,
            invested_usd=self.gross_exposure_usd,
            unrealized_pnl_usd=Decimal("0"),
            equity_usd=Decimal("1000") + self.gross_exposure_usd,
            gross_exposure_usd=self.gross_exposure_usd,
            pending_manual_orders_usd=manual,
            pending_orders_usd=pending,
            positions=self.positions,
            open_orders=self.open_orders,
            pending_orders=self.pending_orders,
            foreign_activity=self.foreign_activity,
        )

    def rates(self, instrument_ids: tuple[int, ...]) -> ApiResponse:
        return ApiResponse(
            200,
            {
                "rates": [
                    {
                        "instrumentID": instrument_ids[0],
                        "bid": "99.9",
                        "ask": "100",
                        "date": datetime.now(UTC).isoformat(),
                    }
                ]
            },
            "00000000-0000-0000-0000-000000000001",
        )

    def prepare_open_by_amount(self, **kwargs):
        self.prepared_kwargs = kwargs
        if self.prepare_error:
            raise PermissionError("deterministic preparation rejection")
        return PreparedDemoOpenV2(
            {"action": "open", "instrumentId": kwargs["instrument_id"]},
            Decimal(str(kwargs["entry_rate"])),
            self.total_cost_usd,
            "c" * 64,
        )

    def submit_prepared_open(
        self, body, *, request_id: str, write_budget_acquired: bool = False
    ) -> ApiResponse:
        if not write_budget_acquired:
            raise AssertionError("executor must acquire write budget before SUBMITTING")
        self.submit_calls += 1
        if self.response is None:
            raise ValueError("response parsing failed after network write")
        return self.response


class StrictInvalidSnapshotClient(PreparedWriteClient):
    def __init__(self, portfolio: dict[str, object]) -> None:
        super().__init__()
        self.portfolio = portfolio

    def account_snapshot(self) -> BrokerAccountSnapshotV2:
        client = EtoroPublicApiDemoClientV2()
        now = datetime.now(UTC)
        client.demo_pnl = lambda: ApiResponse(  # type: ignore[method-assign]
            200,
            {"clientPortfolio": self.portfolio},
            "strict-invalid-snapshot",
            now,
            now,
            now,
        )
        return client.account_snapshot()


class PreparedCloseClient:
    def __init__(
        self,
        quantity: Decimal = Decimal("1"),
        response_position_id: str | None = None,
        response_reference_id: str | None = None,
    ) -> None:
        self.quantity = quantity
        self.response_position_id = response_position_id
        self.response_reference_id = response_reference_id
        self.submit_calls = 0
        self.prepared_units: Decimal | None = None
        self.submitted_body: object = None
        self.write_budget_calls: list[bool] = []

    def verify_isolated_demo_execution_scope(self):
        return {"scopes": ["etoro-public:trade.demo:read", "etoro-public:trade.demo:write"]}

    def acquire_demo_write_budget(self, *, close_priority: bool) -> None:
        self.write_budget_calls.append(close_priority)

    def prepare_close_position(
        self, *, position_id: int, units_to_deduct: Decimal | None
    ) -> PreparedDemoCloseV2:
        self.prepared_units = units_to_deduct
        return PreparedDemoCloseV2(
            {
                "InstrumentID": 1001,
                "UnitsToDeduct": None if units_to_deduct is None else float(units_to_deduct),
            },
            str(position_id),
            1001,
            self.quantity,
            "c" * 64,
        )

    def submit_prepared_close(
        self,
        *,
        position_id: int,
        body: object,
        request_id: str,
        write_budget_acquired: bool = False,
    ) -> ApiResponse:
        if not write_budget_acquired:
            raise AssertionError("executor must acquire write budget before SUBMITTING")
        self.submit_calls += 1
        self.submitted_body = body
        response_body = {
            "orderId": f"close-{position_id}",
            "positionId": self.response_position_id or str(position_id),
        }
        if self.response_reference_id is not None:
            response_body["referenceId"] = self.response_reference_id
        return ApiResponse(
            200,
            response_body,
            request_id,
        )


def execution_worker(
    folder: str,
    config: object,
    store: RuntimeStoreV2,
    kernel: UnifiedTradingKernel,
    client: object,
) -> DemoExecutionWorkerV2:
    gate = Path(folder) / "ENABLE_DEMO_EXECUTION"
    gate.write_text("DEMO only\n", encoding="utf-8")
    return DemoExecutionWorkerV2(  # type: ignore[arg-type]
        config,
        store,
        kernel,
        client,
        execution_gate=gate,
        require_strategy_release=False,
    )


class V2ExecutorRecoveryTests(unittest.TestCase):
    def test_unbound_open_epoch_is_quarantined_before_any_broker_call(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = RuntimeStoreV2(Path(folder) / "runtime.sqlite3")
            now = datetime.now(UTC)
            config, kernel, command = setup_open(
                store,
                now,
                bind_execution_epoch=False,
            )
            store.set_trading_state(
                "ACTIVE",
                actor="test",
                reason="prove shadow command cannot inherit later execution authority",
            )
            worker = execution_worker(folder, config, store, kernel, NoCallClient())

            self.assertEqual(worker.run_once(), 0)
            self.assertEqual(
                store.broker_order(command.order_command_id).status,
                OrderStatus.REJECTED,
            )
            self.assertEqual(store.pending_outbox(), ())
            quarantine = store.db.execute(
                "SELECT payload_json FROM v2_events WHERE event_type='OutboxQuarantined'"
            ).fetchone()
            self.assertIsNotNone(quarantine)
            self.assertIn('"error_type":"OutboxExecutionEpochInvalid"', str(quarantine[0]))
            store.close()

    def test_open_epoch_change_during_preflight_prevents_broker_write(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = RuntimeStoreV2(Path(folder) / "runtime.sqlite3")
            now = datetime.now(UTC)
            config, kernel, command = setup_open(store, now)
            original_epoch = command.execution_epoch
            client = PreparedWriteClient(
                response=ApiResponse(
                    200,
                    {"orderId": "must-not-submit", "positionId": "20"},
                    "00000000-0000-0000-0000-000000000001",
                )
            )
            original_prepare = client.prepare_open_by_amount

            def rotate_authority_during_preflight(**kwargs):
                prepared = original_prepare(**kwargs)
                store.set_trading_state(
                    "HALT_NEW",
                    actor="test",
                    reason="close prior execution authority",
                )
                store.set_trading_state(
                    "ACTIVE",
                    actor="test",
                    reason="open replacement execution authority",
                )
                return prepared

            client.prepare_open_by_amount = rotate_authority_during_preflight  # type: ignore[method-assign]
            worker = execution_worker(folder, config, store, kernel, client)

            self.assertEqual(worker.run_once(), 0)
            self.assertEqual(client.submit_calls, 0)
            self.assertNotEqual(store.trading_state_snapshot()["version"], original_epoch)
            self.assertEqual(
                store.broker_order(command.order_command_id).status,
                OrderStatus.REJECTED,
            )
            self.assertEqual(store.trading_state_snapshot()["state"], "HALT_NEW")
            self.assertEqual(store.pending_outbox(), ())
            store.close()

    def test_unbound_legacy_open_already_submitting_becomes_unknown_not_quarantined(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = RuntimeStoreV2(Path(folder) / "runtime.sqlite3")
            now = datetime.now(UTC)
            config, kernel, command = setup_open(
                store,
                now,
                bind_execution_epoch=False,
            )
            kernel.begin_submit(command.order_command_id, now)
            store.set_trading_state(
                "ACTIVE",
                actor="test",
                reason="legacy recovery authority",
            )
            worker = execution_worker(folder, config, store, kernel, NoCallClient())

            self.assertEqual(worker.run_once(), 0)
            self.assertEqual(
                store.broker_order(command.order_command_id).status,
                OrderStatus.UNKNOWN,
            )
            self.assertEqual(store.state_get("trading_state"), "HALT_NEW")
            self.assertEqual(store.pending_outbox(), ())
            quarantined = store.db.execute(
                "SELECT COUNT(*) FROM v2_events WHERE event_type='OutboxQuarantined'"
            ).fetchone()
            self.assertEqual(int(quarantined[0]), 0)
            store.close()

    def test_invalid_final_account_aliases_never_reach_open_submit(self) -> None:
        invalid_portfolios = (
            {
                "credit": "1000",
                "positions": [],
                "ordersForOpen": [{"orderID": 1, "amount": "10", "mirrorID": True}],
                "orders": [],
            },
            {
                "credit": "1000",
                "positions": [],
                "ordersForOpen": [{"orderID": 1, "amount": "10", "exposure": "11"}],
                "orders": [],
            },
        )
        for portfolio in invalid_portfolios:
            with self.subTest(portfolio=portfolio), tempfile.TemporaryDirectory() as folder:
                store = RuntimeStoreV2(Path(folder) / "runtime.sqlite3")
                now = datetime.now(UTC)
                config, kernel, command = setup_open(store, now)
                store.set_trading_state("ACTIVE", actor="test", reason="strict final snapshot")
                client = StrictInvalidSnapshotClient(portfolio)
                worker = execution_worker(folder, config, store, kernel, client)

                self.assertEqual(worker.run_once(), 0)
                self.assertEqual(client.submit_calls, 0)
                self.assertEqual(store.fills_for_order(command.order_command_id), ())
                self.assertEqual(store.positions(open_only=True), ())
                store.close()

    def test_rate_limit_exhaustion_is_pre_submit_and_never_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = RuntimeStoreV2(Path(folder) / "runtime.sqlite3")
            now = datetime.now(UTC)
            config, kernel, command = setup_open(store, now)
            store.set_trading_state("ACTIVE", actor="test", reason="rate budget")
            client = PreparedWriteClient(write_budget_error=TimeoutError("normal quota full"))
            worker = execution_worker(folder, config, store, kernel, client)

            self.assertEqual(worker.run_once(), 0)
            self.assertEqual(client.submit_calls, 0)
            self.assertEqual(
                store.broker_order(command.order_command_id).status,
                OrderStatus.RISK_APPROVED,
            )
            self.assertEqual(len(store.pending_outbox()), 1)
            self.assertEqual(client.write_budget_calls, [False])
            store.close()

    def test_open_revalidates_full_account_after_decision_before_broker_write(self) -> None:
        blocked_clients = (
            PreparedWriteClient(
                positions=({"positionID": "manual-1"},),
                gross_exposure_usd=Decimal("100"),
            ),
            PreparedWriteClient(foreign_activity=("mirror_position:copy-1",)),
            PreparedWriteClient(open_orders=({"orderID": "manual-order", "amount": "50"},)),
        )
        for client in blocked_clients:
            with self.subTest(client=client), tempfile.TemporaryDirectory() as folder:
                store = RuntimeStoreV2(Path(folder) / "runtime.sqlite3")
                now = datetime.now(UTC)
                config, kernel, command = setup_open(store, now)
                store.set_trading_state("ACTIVE", actor="test", reason="account race")
                worker = execution_worker(folder, config, store, kernel, client)

                self.assertEqual(worker.run_once(), 0)
                self.assertEqual(client.submit_calls, 0)
                self.assertEqual(
                    store.broker_order(command.order_command_id).status,
                    OrderStatus.REJECTED,
                )
                self.assertEqual(store.trading_state_snapshot()["state"], "HALT_NEW")
                store.close()

    def test_http_quote_request_id_is_not_mislabeled_as_broker_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = RuntimeStoreV2(Path(folder) / "runtime.sqlite3")
            now = datetime.now(UTC)
            config, kernel, command = setup_open(store, now)
            store.set_trading_state("ACTIVE", actor="test", reason="quote provenance")
            worker = execution_worker(folder, config, store, kernel, PreparedWriteClient())

            _, quote, _ = worker._preflight_open(command.order_command_id)

            self.assertEqual(quote.quote_source, "etoro-public-api-http-snapshot")
            self.assertEqual(
                quote.quote_sequence_or_event_id,
                "http-request:00000000-0000-0000-0000-000000000001",
            )
            store.close()

    @staticmethod
    def _filled_position(store: RuntimeStoreV2, now: datetime):
        config, kernel, command = setup_open(store, now)
        kernel.begin_submit(command.order_command_id, now)
        position = kernel.apply_fill(
            Fill(
                "fill-open-for-close",
                command.order_command_id,
                command.client_order_id,
                "broker-open",
                "12345",
                "AAPL",
                Side.BUY,
                Decimal("1"),
                Decimal("100"),
                Decimal("0"),
                Decimal("0"),
                now,
                now,
                "fill-open-for-close",
            ),
            final=True,
        )
        return config, kernel, position

    def test_full_and_partial_close_reach_broker_request_with_reduce_provenance(self) -> None:
        for units, expected_body_units in (
            (None, None),
            (Decimal("0.4"), 0.4),
        ):
            with self.subTest(units=units), tempfile.TemporaryDirectory() as folder:
                store = RuntimeStoreV2(Path(folder) / "runtime.sqlite3")
                now = datetime.now(UTC)
                config, kernel, position = self._filled_position(store, now)
                close = kernel.create_close_command(
                    position,
                    now=now + timedelta(seconds=1),
                    reason=ExitReason.REDUCE_ONLY,
                    broker=BrokerTruth(
                        Decimal("1000"),
                        Decimal("1000"),
                        Decimal("900"),
                        Decimal("100"),
                        Decimal("100"),
                        1,
                        Decimal("0"),
                        Decimal("0"),
                        Decimal("0"),
                        Decimal("0"),
                        "b" * 64,
                        now,
                    ),
                    units_to_deduct=units,
                )
                self.assertEqual(close.intent_hash, "")
                self.assertEqual(len(close.reduce_provenance_hash), 64)
                store.set_trading_state(
                    "ACTIVE",
                    actor="test",
                    reason="exercise permitted reduce execution",
                )
                client = PreparedCloseClient()
                worker = execution_worker(folder, config, store, kernel, client)

                self.assertEqual(worker.run_once(), 1)
                self.assertEqual(client.submit_calls, 1)
                self.assertEqual(client.prepared_units, units)
                self.assertEqual(client.submitted_body["UnitsToDeduct"], expected_body_units)
                self.assertEqual(
                    store.broker_order(close.order_command_id).status,
                    OrderStatus.ACKNOWLEDGED,
                )
                store.close()

    def test_close_success_for_different_position_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = RuntimeStoreV2(Path(folder) / "runtime.sqlite3")
            now = datetime.now(UTC)
            config, kernel, position = self._filled_position(store, now)
            close = kernel.create_close_command(
                position,
                now=now + timedelta(seconds=1),
                reason=ExitReason.REDUCE_ONLY,
                broker=BrokerTruth(
                    Decimal("1000"),
                    Decimal("1000"),
                    Decimal("900"),
                    Decimal("100"),
                    Decimal("100"),
                    1,
                    Decimal("0"),
                    Decimal("0"),
                    Decimal("0"),
                    Decimal("0"),
                    "b" * 64,
                    now,
                ),
            )
            store.set_trading_state("ACTIVE", actor="test", reason="strict close ACK")
            client = PreparedCloseClient(response_position_id="54321")
            worker = execution_worker(folder, config, store, kernel, client)

            self.assertEqual(worker.run_once(), 0)
            self.assertEqual(client.submit_calls, 1)
            self.assertEqual(
                store.broker_order(close.order_command_id).status,
                OrderStatus.UNKNOWN,
            )
            self.assertEqual(store.pending_outbox(), ())
            store.close()

    def test_close_success_for_different_client_reference_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = RuntimeStoreV2(Path(folder) / "runtime.sqlite3")
            now = datetime.now(UTC)
            config, kernel, position = self._filled_position(store, now)
            close = kernel.create_close_command(
                position,
                now=now + timedelta(seconds=1),
                reason=ExitReason.REDUCE_ONLY,
                broker=BrokerTruth(
                    Decimal("1000"),
                    Decimal("1000"),
                    Decimal("900"),
                    Decimal("100"),
                    Decimal("100"),
                    1,
                    Decimal("0"),
                    Decimal("0"),
                    Decimal("0"),
                    Decimal("0"),
                    "b" * 64,
                    now,
                ),
            )
            store.set_trading_state("ACTIVE", actor="test", reason="strict close ACK")
            client = PreparedCloseClient(response_reference_id="wrong-client-reference")
            worker = execution_worker(folder, config, store, kernel, client)

            self.assertEqual(worker.run_once(), 0)
            self.assertEqual(client.submit_calls, 1)
            self.assertEqual(
                store.broker_order(close.order_command_id).status,
                OrderStatus.UNKNOWN,
            )
            self.assertEqual(store.pending_outbox(), ())
            store.close()

    def test_reduce_only_command_crosses_lock_new_with_manual_gate_present(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = RuntimeStoreV2(Path(folder) / "runtime.sqlite3")
            now = datetime.now(UTC)
            config, kernel, position = self._filled_position(store, now)
            close_at = datetime.now(UTC)
            close = kernel.create_close_command(
                position,
                now=close_at,
                reason=ExitReason.AGENT_CLOSE,
                broker=BrokerTruth(
                    Decimal("1000"),
                    Decimal("1000"),
                    Decimal("900"),
                    Decimal("100"),
                    Decimal("100"),
                    1,
                    Decimal("0"),
                    Decimal("0"),
                    Decimal("0"),
                    Decimal("0"),
                    "b" * 64,
                    close_at,
                ),
            )
            store.set_trading_state(
                "LOCKED",
                actor="test",
                reason="lock new exposure while preserving reduce-only exit",
            )
            client = PreparedCloseClient()
            worker = execution_worker(folder, config, store, kernel, client)

            self.assertEqual(worker.run_once(), 1)
            self.assertEqual(client.submit_calls, 1)
            self.assertEqual(
                store.broker_order(close.order_command_id).status,
                OrderStatus.ACKNOWLEDGED,
            )
            self.assertEqual(store.pending_outbox(), ())
            self.assertEqual(store.trading_state_snapshot()["state"], "LOCKED")
            store.close()

    def test_retryable_pre_submit_failure_quarantines_at_attempt_ceiling(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = RuntimeStoreV2(Path(folder) / "runtime.sqlite3")
            now = datetime.now(UTC)
            config, kernel, command = setup_open(store, now)
            store.set_trading_state("ACTIVE", actor="test", reason="exercise poison retry cap")
            client = PreparedWriteClient()

            def retryable_failure(**kwargs):
                raise RuntimeError("transient pre-submit failure")

            client.prepare_open_by_amount = retryable_failure  # type: ignore[method-assign]
            worker = execution_worker(folder, config, store, kernel, client)

            self.assertEqual(worker.run_once(), 0)
            pending = store.pending_outbox()
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["attempt_count"], 1)
            self.assertIsNone(pending[0]["claimed_by"])
            self.assertIsNone(pending[0]["lease_expires_at"])
            self.assertEqual(pending[0]["last_error_type"], "RuntimeError")
            self.assertEqual(worker.run_once(), 0)
            self.assertEqual(len(store.pending_outbox()), 1)
            self.assertEqual(worker.run_once(), 0)
            self.assertEqual(store.pending_outbox(), ())
            self.assertEqual(client.submit_calls, 0)
            self.assertEqual(
                store.broker_order(command.order_command_id).status,
                OrderStatus.REJECTED,
            )
            event = store.db.execute(
                "SELECT payload_json FROM v2_events WHERE event_type='OutboxQuarantined'"
            ).fetchone()
            self.assertIsNotNone(event)
            self.assertIn('"attempt":3', str(event[0]))
            self.assertIn('"network_write_attempted":false', str(event[0]))
            store.close()

    def test_open_is_rejected_without_promoted_strategy_release(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = RuntimeStoreV2(Path(folder) / "runtime.sqlite3")
            now = datetime.now(UTC)
            config, kernel, command = setup_open(store, now)
            store.set_trading_state("ACTIVE", actor="test", reason="strategy release gate")
            client = PreparedWriteClient()
            worker = execution_worker(folder, config, store, kernel, client)
            worker.require_strategy_release = True
            with patch.dict(
                os.environ,
                {
                    "ETORO_V2_STRATEGY_RELEASE_FILE": str(Path(folder) / "missing-release"),
                    "ETORO_V2_STRATEGY_TRUST_FILE": str(Path(folder) / "missing-trust"),
                },
            ):
                self.assertEqual(worker.run_once(), 0)
            self.assertEqual(client.submit_calls, 0)
            self.assertEqual(
                store.broker_order(command.order_command_id).status,
                OrderStatus.REJECTED,
            )
            store.close()

    def test_malformed_outbox_is_quarantined_and_later_valid_row_continues(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = RuntimeStoreV2(Path(folder) / "runtime.sqlite3")
            now = datetime.now(UTC)
            config, kernel, command = setup_open(store, now)
            store.db.execute(
                """INSERT INTO v2_outbox(
                       outbox_id,topic,payload_json,idempotency_key,created_at
                   ) VALUES(?,?,?,?,?)""",
                (
                    "outbox-poison",
                    "unsupported.topic",
                    "{}",
                    "poison-idempotency",
                    (now - timedelta(seconds=1)).isoformat(),
                ),
            )
            store.db.commit()
            store.set_trading_state("ACTIVE", actor="test", reason="exercise FIFO quarantine")
            client = PreparedWriteClient(
                response=ApiResponse(
                    200,
                    {"orderId": "broker-valid", "positionId": "broker-position-valid"},
                    "00000000-0000-0000-0000-000000000001",
                )
            )
            worker = execution_worker(folder, config, store, kernel, client)

            self.assertEqual(worker.run_once(), 1)
            self.assertEqual(client.submit_calls, 1)
            self.assertEqual(store.pending_outbox(), ())
            self.assertEqual(
                store.broker_order(command.order_command_id).status,
                OrderStatus.ACKNOWLEDGED,
            )
            poison = store.db.execute(
                "SELECT delivered_at FROM v2_outbox WHERE outbox_id='outbox-poison'"
            ).fetchone()
            self.assertIsNotNone(poison[0])
            event = store.db.execute(
                "SELECT payload_json FROM v2_events WHERE event_type='OutboxQuarantined'"
            ).fetchone()
            self.assertIn('"manual_replay_requires_new_signed_command":true', str(event[0]))
            store.close()

    def test_gate_removal_after_preflight_locks_and_prevents_broker_write(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = RuntimeStoreV2(Path(folder) / "runtime.sqlite3")
            now = datetime.now(UTC)
            config, kernel, command = setup_open(store, now)
            store.set_trading_state("ACTIVE", actor="test", reason="exercise dynamic gate")
            client = PreparedWriteClient(
                response=ApiResponse(200, {"orderId": "must-not-send"}, "request")
            )
            worker = execution_worker(folder, config, store, kernel, client)
            original_prepare = client.prepare_open_by_amount

            def remove_gate_after_prepare(**kwargs):
                prepared = original_prepare(**kwargs)
                worker.execution_gate.unlink()
                return prepared

            client.prepare_open_by_amount = remove_gate_after_prepare  # type: ignore[method-assign]

            self.assertEqual(worker.run_once(), 0)
            self.assertEqual(client.submit_calls, 0)
            self.assertEqual(store.state_get("trading_state"), "LOCKED")
            self.assertEqual(
                store.broker_order(command.order_command_id).status,
                OrderStatus.REJECTED,
            )
            self.assertEqual(store.pending_outbox(), ())
            store.close()

    def test_outbox_lease_prevents_concurrent_dispatch_and_stale_completion(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = RuntimeStoreV2(Path(folder) / "runtime.sqlite3")
            now = datetime.now(UTC)
            setup_open(store, now)
            first = store.claim_outbox("worker-1", now=now, lease_seconds=10)
            self.assertEqual(len(first), 1)
            self.assertEqual(
                store.claim_outbox("worker-2", now=now + timedelta(seconds=5), lease_seconds=10),
                (),
            )
            reclaimed = store.claim_outbox(
                "worker-2", now=now + timedelta(seconds=11), lease_seconds=10
            )
            self.assertEqual(len(reclaimed), 1)
            self.assertNotEqual(first[0]["claim_token"], reclaimed[0]["claim_token"])
            with self.assertRaises(PermissionError):
                store.mark_outbox_delivered(
                    str(first[0]["outbox_id"]),
                    str(first[0]["claim_token"]),
                    now + timedelta(seconds=11),
                )
            store.mark_outbox_delivered(
                str(reclaimed[0]["outbox_id"]),
                str(reclaimed[0]["claim_token"]),
                now + timedelta(seconds=11),
            )
            self.assertEqual(store.pending_outbox(), ())
            store.close()

    def test_deterministic_preflight_failure_is_rejected_without_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = RuntimeStoreV2(Path(folder) / "runtime.sqlite3")
            now = datetime.now(UTC)
            config, kernel, command = setup_open(store, now)
            store.state_set("trading_state", "LOCKED", now)
            worker = execution_worker(folder, config, store, kernel, NoCallClient())

            self.assertEqual(worker.run_once(), 0)

            order = store.broker_order(command.order_command_id)
            self.assertEqual(order.status, OrderStatus.REJECTED)
            self.assertEqual(store.state_get("trading_state", "LOCKED"), "LOCKED")
            self.assertEqual(store.pending_outbox(), ())
            event = store.db.execute(
                "SELECT payload_json FROM v2_events WHERE event_type='OrderRejectedBeforeSend'"
            ).fetchone()
            self.assertIsNotNone(event)
            self.assertIn('"network_write_attempted":false', str(event[0]))
            store.close()

    def test_orphaned_submitting_order_is_unknown_and_never_resent(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = RuntimeStoreV2(Path(folder) / "runtime.sqlite3")
            now = datetime.now(UTC)
            config, kernel, command = setup_open(store, now)
            kernel.begin_submit(command.order_command_id, now)
            worker = execution_worker(folder, config, store, kernel, NoCallClient())

            self.assertEqual(worker.run_once(), 0)

            order = store.broker_order(command.order_command_id)
            self.assertEqual(order.status, OrderStatus.UNKNOWN)
            self.assertEqual(store.state_get("trading_state"), "HALT_NEW")
            self.assertEqual(store.pending_outbox(), ())
            store.close()

    def test_expired_reduce_only_command_is_rejected_before_broker_preparation(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = RuntimeStoreV2(Path(folder) / "runtime.sqlite3")
            old = datetime.now(UTC) - timedelta(minutes=5)
            config, kernel, open_command = setup_open(store, old)
            kernel.begin_submit(open_command.order_command_id, old)
            position = kernel.apply_fill(
                Fill(
                    "fill-open",
                    open_command.order_command_id,
                    open_command.client_order_id,
                    "broker-open",
                    "12345",
                    "AAPL",
                    Side.BUY,
                    Decimal("1"),
                    Decimal("100"),
                    Decimal("0"),
                    Decimal("0"),
                    old,
                    old,
                    "fill-open",
                ),
                final=True,
            )
            reduce_truth = BrokerTruth(
                Decimal("1000"),
                Decimal("1000"),
                Decimal("900"),
                Decimal("100"),
                Decimal("100"),
                1,
                Decimal("0"),
                Decimal("0"),
                Decimal("0"),
                Decimal("0"),
                "reduce-snapshot",
                old + timedelta(seconds=1),
            )
            close = kernel.create_close_command(
                position,
                now=old + timedelta(seconds=1),
                reason=ExitReason.AGENT_CLOSE,
                broker=reduce_truth,
            )
            worker = execution_worker(folder, config, store, kernel, NoCallClient())

            self.assertEqual(worker.run_once(), 0)
            self.assertEqual(
                store.broker_order(close.order_command_id).status,
                OrderStatus.REJECTED,
            )
            self.assertEqual(store.pending_outbox(), ())
            store.close()

    def test_tampered_persisted_command_is_locked_before_any_broker_write(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = RuntimeStoreV2(Path(folder) / "runtime.sqlite3")
            now = datetime.now(UTC)
            config, kernel, command = setup_open(store, now)
            row = store.db.execute(
                "SELECT command_json FROM v2_order_commands WHERE order_command_id=?",
                (command.order_command_id,),
            ).fetchone()
            payload = json.loads(str(row[0]))
            payload["amount_usd"] = "101"
            store.db.execute(
                "UPDATE v2_order_commands SET command_json=? WHERE order_command_id=?",
                (
                    json.dumps(payload, sort_keys=True, separators=(",", ":")),
                    command.order_command_id,
                ),
            )
            store.db.commit()
            store.state_set("trading_state", "ACTIVE", now)
            client = PreparedWriteClient()
            worker = execution_worker(folder, config, store, kernel, client)

            self.assertEqual(worker.run_once(), 0)
            self.assertEqual(client.submit_calls, 0)
            self.assertEqual(store.state_get("trading_state"), "LOCKED")
            self.assertEqual(
                store.broker_order(command.order_command_id).status,
                OrderStatus.REJECTED,
            )
            store.close()

    def test_stop_and_take_use_the_execution_side_entry_quote(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = RuntimeStoreV2(Path(folder) / "runtime.sqlite3")
            now = datetime.now(UTC)
            config, kernel, _ = setup_open(store, now)
            store.state_set("trading_state", "ACTIVE", now)
            client = PreparedWriteClient(
                response=ApiResponse(
                    200,
                    {"orderId": "broker-order-1", "positionId": "broker-position-1"},
                    "00000000-0000-0000-0000-000000000001",
                )
            )
            worker = execution_worker(folder, config, store, kernel, client)

            self.assertEqual(worker.run_once(), 1)
            assert client.prepared_kwargs is not None
            self.assertEqual(client.prepared_kwargs["stop_loss_rate"], Decimal("98.00"))
            self.assertEqual(client.prepared_kwargs["take_profit_rate"], Decimal("104.00"))
            store.close()

    def test_prepare_failure_is_terminal_before_write(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = RuntimeStoreV2(Path(folder) / "runtime.sqlite3")
            now = datetime.now(UTC)
            config, kernel, command = setup_open(store, now)
            store.set_trading_state("ACTIVE", actor="test", reason="exercise executor preparation")
            client = PreparedWriteClient(prepare_error=True)
            worker = execution_worker(folder, config, store, kernel, client)

            self.assertEqual(worker.run_once(), 0)

            self.assertEqual(client.submit_calls, 0)
            self.assertEqual(
                store.broker_order(command.order_command_id).status,
                OrderStatus.REJECTED,
            )
            self.assertEqual(store.pending_outbox(), ())
            store.close()

    def test_broker_costs_are_included_in_fresh_max_loss(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = RuntimeStoreV2(Path(folder) / "runtime.sqlite3")
            now = datetime.now(UTC)
            config, kernel, command = setup_open(store, now)
            store.set_trading_state("ACTIVE", actor="test", reason="exercise cost risk cap")
            client = PreparedWriteClient(total_cost_usd=Decimal("9"))
            worker = execution_worker(folder, config, store, kernel, client)

            self.assertEqual(worker.run_once(), 0)

            self.assertEqual(client.submit_calls, 0)
            self.assertEqual(
                store.broker_order(command.order_command_id).status,
                OrderStatus.REJECTED,
            )
            self.assertEqual(store.active_risk_reservations(), ())
            store.close()

    def test_any_exception_after_submit_boundary_is_unknown_and_not_retried(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = RuntimeStoreV2(Path(folder) / "runtime.sqlite3")
            now = datetime.now(UTC)
            config, kernel, command = setup_open(store, now)
            store.set_trading_state(
                "ACTIVE", actor="test", reason="exercise post-write uncertainty"
            )
            client = PreparedWriteClient()
            worker = execution_worker(folder, config, store, kernel, client)

            with self.assertRaisesRegex(ValueError, "after network write"):
                worker.run_once()

            self.assertEqual(client.submit_calls, 1)
            self.assertEqual(
                store.broker_order(command.order_command_id).status,
                OrderStatus.UNKNOWN,
            )
            self.assertEqual(store.state_get("trading_state"), "HALT_NEW")
            self.assertEqual(store.pending_outbox(), ())
            self.assertEqual(len(store.active_risk_reservations()), 1)
            self.assertEqual(worker.run_once(), 0)
            self.assertEqual(client.submit_calls, 1)
            store.close()

    def test_success_without_broker_identity_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = RuntimeStoreV2(Path(folder) / "runtime.sqlite3")
            now = datetime.now(UTC)
            config, kernel, command = setup_open(store, now)
            store.set_trading_state(
                "ACTIVE", actor="test", reason="exercise malformed broker success"
            )
            client = PreparedWriteClient(
                response=ApiResponse(
                    200,
                    {},
                    "00000000-0000-0000-0000-000000000001",
                )
            )
            worker = execution_worker(folder, config, store, kernel, client)

            self.assertEqual(worker.run_once(), 0)

            self.assertEqual(
                store.broker_order(command.order_command_id).status,
                OrderStatus.UNKNOWN,
            )
            self.assertEqual(store.pending_outbox(), ())
            store.close()

    def test_conflicting_or_position_only_success_identity_is_unknown(self) -> None:
        payloads = (
            {"orderId": "10", "orderID": "11", "positionId": "20"},
            {"positionId": "20"},
            {"orderId": True, "positionId": "20"},
            {"orderId": 0, "positionId": "20"},
            {"orderId": -1, "positionId": "20"},
            {"orderId": "0", "positionId": "20"},
            {"orderId": "order id", "positionId": "20"},
        )
        for payload in payloads:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as folder:
                store = RuntimeStoreV2(Path(folder) / "runtime.sqlite3")
                now = datetime.now(UTC)
                config, kernel, command = setup_open(store, now)
                store.set_trading_state("ACTIVE", actor="test", reason="strict ACK")
                client = PreparedWriteClient(
                    response=ApiResponse(
                        200,
                        payload,
                        "00000000-0000-0000-0000-000000000001",
                    )
                )
                worker = execution_worker(folder, config, store, kernel, client)

                self.assertEqual(worker.run_once(), 0)
                self.assertEqual(
                    store.broker_order(command.order_command_id).status,
                    OrderStatus.UNKNOWN,
                )
                self.assertEqual(store.pending_outbox(), ())
                store.close()

    def test_expiry_is_rechecked_before_submit_and_network_write(self) -> None:
        for clock_offsets in ((0, 61), (0, 0, 61)):
            with (
                self.subTest(clock_offsets=clock_offsets),
                tempfile.TemporaryDirectory() as folder,
            ):
                store = RuntimeStoreV2(Path(folder) / "runtime.sqlite3")
                now = datetime.now(UTC)
                config, kernel, command = setup_open(store, now)
                store.set_trading_state("ACTIVE", actor="test", reason="expiry race")
                client = PreparedWriteClient(
                    response=ApiResponse(
                        200,
                        {"orderId": "10", "positionId": "20"},
                        "00000000-0000-0000-0000-000000000001",
                    )
                )
                worker = execution_worker(folder, config, store, kernel, client)
                clock = [command.expires_at + timedelta(seconds=value) for value in clock_offsets]
                # Initial verification uses the first value; later values model
                # preflight/rate-budget latency at the two final send boundaries.
                clock[0] = command.expires_at - timedelta(seconds=1)
                with patch.object(worker, "_now", side_effect=clock):
                    self.assertEqual(worker.run_once(), 0)
                self.assertEqual(client.submit_calls, 0)
                self.assertEqual(
                    store.broker_order(command.order_command_id).status,
                    OrderStatus.REJECTED,
                )
                self.assertEqual(store.pending_outbox(), ())
                store.close()

    def test_risk_seal_is_rechecked_after_slow_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = RuntimeStoreV2(Path(folder) / "runtime.sqlite3")
            now = datetime.now(UTC)
            config, kernel, command = setup_open(store, now)
            store.set_trading_state("ACTIVE", actor="test", reason="seal race")
            client = PreparedWriteClient(
                response=ApiResponse(
                    200,
                    {"orderId": "10", "positionId": "20"},
                    "00000000-0000-0000-0000-000000000001",
                )
            )
            worker = execution_worker(folder, config, store, kernel, client)
            with patch.object(worker.verifier, "verify", side_effect=(True, False)):
                self.assertEqual(worker.run_once(), 0)
            self.assertEqual(client.submit_calls, 0)
            self.assertEqual(
                store.broker_order(command.order_command_id).status,
                OrderStatus.REJECTED,
            )
            self.assertEqual(store.trading_state_snapshot()["state"], "LOCKED")
            self.assertEqual(store.pending_outbox(), ())
            store.close()

    def test_success_with_mismatched_client_reference_is_unknown(self) -> None:
        payloads = (
            {"orderId": "10", "positionId": "20", "referenceId": "wrong"},
            {
                "orderId": "10",
                "positionId": "20",
                "referenceID": "one",
                "requestId": "two",
            },
        )
        for payload in payloads:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as folder:
                store = RuntimeStoreV2(Path(folder) / "runtime.sqlite3")
                now = datetime.now(UTC)
                config, kernel, command = setup_open(store, now)
                store.set_trading_state("ACTIVE", actor="test", reason="strict ACK")
                client = PreparedWriteClient(
                    response=ApiResponse(
                        200,
                        payload,
                        "00000000-0000-0000-0000-000000000001",
                    )
                )
                worker = execution_worker(folder, config, store, kernel, client)

                self.assertEqual(worker.run_once(), 0)
                self.assertEqual(
                    store.broker_order(command.order_command_id).status,
                    OrderStatus.UNKNOWN,
                )
                self.assertEqual(store.state_get("trading_state"), "HALT_NEW")
                self.assertEqual(store.pending_outbox(), ())
                store.close()


if __name__ == "__main__":
    unittest.main()
