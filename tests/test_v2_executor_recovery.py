from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from etoro_agent.config_v2 import load_config_v2
from etoro_agent.domain_v2 import (
    ExitReason,
    Fill,
    IntentEnvelope,
    OrderStatus,
    QuoteProvenance,
    Side,
)
from etoro_agent.etoro_api_current_v2 import ApiResponse, DemoCashTruth, PreparedDemoOpenV2
from etoro_agent.executor_v2 import DemoExecutionWorkerV2
from etoro_agent.kernel_v2 import UnifiedTradingKernel
from etoro_agent.risk_v2 import BrokerTruth, GlobalRiskKernel
from etoro_agent.runtime_store_v2 import RuntimeStoreV2


def setup_open(store: RuntimeStoreV2, now: datetime):
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
    risk, command = kernel.submit_open_intent(intent, quote, broker, now=now)
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
    ) -> None:
        self.prepare_error = prepare_error
        self.response = response
        self.total_cost_usd = total_cost_usd
        self.submit_calls = 0
        self.prepared_kwargs = None

    def verify_isolated_demo_execution_scope(self):
        return {"scopes": ["etoro-public:trade.demo:read", "etoro-public:trade.demo:write"]}

    def cash_truth(self) -> DemoCashTruth:
        now = datetime.now(UTC)
        return DemoCashTruth(
            Decimal("1000"),
            Decimal("1000"),
            Decimal("0"),
            Decimal("0"),
            "broker",
            now,
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

    def submit_prepared_open(self, body, *, request_id: str) -> ApiResponse:
        self.submit_calls += 1
        if self.response is None:
            raise ValueError("response parsing failed after network write")
        return self.response


class V2ExecutorRecoveryTests(unittest.TestCase):
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
            worker = DemoExecutionWorkerV2(config, store, kernel, NoCallClient())

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
            worker = DemoExecutionWorkerV2(config, store, kernel, NoCallClient())

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
            worker = DemoExecutionWorkerV2(config, store, kernel, NoCallClient())

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
            worker = DemoExecutionWorkerV2(config, store, kernel, client)

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
            worker = DemoExecutionWorkerV2(config, store, kernel, client)

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
            worker = DemoExecutionWorkerV2(config, store, kernel, client)

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
            worker = DemoExecutionWorkerV2(config, store, kernel, client)

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
            worker = DemoExecutionWorkerV2(config, store, kernel, client)

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
            worker = DemoExecutionWorkerV2(config, store, kernel, client)

            self.assertEqual(worker.run_once(), 0)

            self.assertEqual(
                store.broker_order(command.order_command_id).status,
                OrderStatus.UNKNOWN,
            )
            self.assertEqual(store.pending_outbox(), ())
            store.close()


if __name__ == "__main__":
    unittest.main()
