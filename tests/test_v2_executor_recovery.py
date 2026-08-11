from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from etoro_agent.config_v2 import load_config_v2
from etoro_agent.domain_v2 import IntentEnvelope, OrderStatus, QuoteProvenance, Side
from etoro_agent.etoro_api_current_v2 import ApiResponse, DemoCashTruth
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
    def __getattr__(self, name: str):
        raise AssertionError(f"broker client must not be called: {name}")


class PreparedWriteClient:
    def __init__(self, *, prepare_error: bool = False, response: ApiResponse | None = None) -> None:
        self.prepare_error = prepare_error
        self.response = response
        self.submit_calls = 0

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
        if self.prepare_error:
            raise PermissionError("deterministic preparation rejection")
        return {"action": "open", "instrumentId": kwargs["instrument_id"]}

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
            store.state_set("trading_state", "HALT_NEW", now)
            worker = DemoExecutionWorkerV2(config, store, kernel, NoCallClient())

            self.assertEqual(worker.run_once(), 0)

            order = store.broker_order(command.order_command_id)
            self.assertEqual(order.status, OrderStatus.REJECTED)
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
