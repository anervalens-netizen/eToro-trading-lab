from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from etoro_agent.config_v2 import load_config_v2
from etoro_agent.domain_v2 import (
    ExitReason,
    Fill,
    IntentEnvelope,
    OrderStatus,
    QuoteProvenance,
    Side,
)
from etoro_agent.etoro_api_current_v2 import ApiResponse
from etoro_agent.kernel_v2 import UnifiedTradingKernel
from etoro_agent.reconciliation_v2 import DemoReconciliationWorkerV2
from etoro_agent.risk_v2 import BrokerTruth, GlobalRiskKernel
from etoro_agent.runtime_store_v2 import RuntimeStoreV2


class PortfolioClient:
    def __init__(
        self, positions: list[dict[str, Any]], pending: list[dict[str, Any]] | None = None
    ) -> None:
        self.positions = positions
        self.pending = pending or []

    def demo_portfolio(self) -> ApiResponse:
        return ApiResponse(
            200,
            {
                "clientPortfolio": {
                    "positions": self.positions,
                    "orders": self.pending,
                    "ordersForOpen": [],
                }
            },
            "reconciliation-test",
        )


def open_command(store: RuntimeStoreV2, now: datetime):
    config = load_config_v2("config/v2-demo-execution.json")
    kernel = UnifiedTradingKernel(store, GlobalRiskKernel(config.mandate))
    intent = IntentEnvelope(
        "intent-reconcile",
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
        now + timedelta(minutes=10),
        Decimal("99.9"),
        Decimal("100"),
        Decimal("50"),
        Decimal("25"),
        "broker-snapshot",
        correlation_id="packet-reconcile",
    )
    quote = QuoteProvenance(
        "AAPL",
        Decimal("99.9"),
        Decimal("100"),
        now,
        now,
        "test",
        "quote-1",
        "market-snapshot",
        "broker-snapshot",
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
        "broker-snapshot",
        now,
    )
    risk, command = kernel.submit_open_intent(intent, quote, broker, now=now)
    assert risk.approved and command is not None
    return config, kernel, command


class V2ReconciliationTests(unittest.TestCase):
    def test_unknown_open_with_exact_position_identity_projects_fill(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = RuntimeStoreV2(Path(folder) / "runtime.sqlite3")
            old = datetime.now(UTC) - timedelta(minutes=5)
            config, kernel, command = open_command(store, old)
            kernel.begin_submit(command.order_command_id, old)
            kernel.acknowledge(
                command.order_command_id,
                at=old + timedelta(seconds=1),
                broker_order_id="bo-1",
                broker_position_id="bp-1",
            )
            kernel.mark_unknown(
                command.order_command_id,
                at=old + timedelta(seconds=2),
                reason="broker response lost",
            )
            worker = DemoReconciliationWorkerV2(
                config,
                store,
                kernel,
                PortfolioClient(
                    [
                        {
                            "instrumentID": 1001,
                            "positionID": "bp-1",
                            "units": "1",
                            "openRate": "100",
                        }
                    ]
                ),
                grace_seconds=30,
            )

            self.assertEqual(worker.run_once(), 1)
            self.assertEqual(
                store.broker_order(command.order_command_id).status,
                OrderStatus.RECONCILED_FILLED,
            )
            positions = store.positions("master_1000", open_only=True)
            self.assertEqual(len(positions), 1)
            self.assertEqual(positions[0].broker_position_id, "bp-1")
            self.assertEqual(positions[0].quantity, Decimal("1"))
            case = store.reconciliation_case(command.order_command_id)
            self.assertIsNotNone(case)
            assert case is not None
            self.assertEqual(case.status, "RESOLVED_FILLED")
            self.assertEqual(case.attempts, 1)
            self.assertTrue(store.verify_event_chain())
            store.close()

    def test_symbol_only_match_never_binds_unknown_order_to_existing_position(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = RuntimeStoreV2(Path(folder) / "runtime.sqlite3")
            old = datetime.now(UTC) - timedelta(minutes=5)
            config, kernel, command = open_command(store, old)
            kernel.begin_submit(command.order_command_id, old)
            kernel.mark_unknown(
                command.order_command_id,
                at=old + timedelta(seconds=1),
                reason="submit outcome unknown",
            )
            store.set_trading_state(
                "ACTIVE", actor="test", reason="exercise fail-closed reconciliation"
            )
            worker = DemoReconciliationWorkerV2(
                config,
                store,
                kernel,
                PortfolioClient(
                    [
                        {
                            "instrumentID": 1001,
                            "positionID": "unrelated-position",
                            "units": "1",
                            "openRate": "100",
                        }
                    ]
                ),
                grace_seconds=30,
            )

            self.assertEqual(worker.run_once(), 1)
            self.assertEqual(
                store.broker_order(command.order_command_id).status,
                OrderStatus.MANUAL_REVIEW,
            )
            self.assertEqual(store.positions(open_only=True), ())
            self.assertEqual(store.state_get("trading_state"), "LOCKED")
            case = store.reconciliation_case(command.order_command_id)
            self.assertIsNotNone(case)
            assert case is not None
            self.assertEqual(case.status, "MANUAL_REVIEW")
            store.close()

    def test_exact_open_position_stays_unprojected_while_order_is_pending(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = RuntimeStoreV2(Path(folder) / "runtime.sqlite3")
            old = datetime.now(UTC) - timedelta(minutes=5)
            config, kernel, command = open_command(store, old)
            kernel.begin_submit(command.order_command_id, old)
            kernel.acknowledge(
                command.order_command_id,
                at=old + timedelta(seconds=1),
                broker_order_id="bo-1",
                broker_position_id="bp-1",
            )
            worker = DemoReconciliationWorkerV2(
                config,
                store,
                kernel,
                PortfolioClient(
                    [
                        {
                            "instrumentID": 1001,
                            "positionID": "bp-1",
                            "units": "0.4",
                            "openRate": "100",
                        }
                    ],
                    pending=[{"orderID": "bo-1", "positionIds": ["bp-1"]}],
                ),
                grace_seconds=30,
            )

            self.assertEqual(worker.run_once(), 0)
            self.assertEqual(
                store.broker_order(command.order_command_id).status,
                OrderStatus.ACKNOWLEDGED,
            )
            self.assertEqual(store.positions(open_only=True), ())
            store.close()

    def test_unknown_close_unchanged_and_not_pending_is_reconciled_absent(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = RuntimeStoreV2(Path(folder) / "runtime.sqlite3")
            old = datetime.now(UTC) - timedelta(minutes=5)
            config, kernel, command = open_command(store, old)
            kernel.begin_submit(command.order_command_id, old)
            position = kernel.apply_fill(
                Fill(
                    "fill-open",
                    command.order_command_id,
                    command.client_order_id,
                    "bo-open",
                    "bp-1",
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
            close = kernel.create_close_command(
                position,
                now=old + timedelta(minutes=1),
                reason=ExitReason.AGENT_CLOSE,
            )
            kernel.begin_submit(close.order_command_id, old + timedelta(minutes=1))
            kernel.mark_unknown(
                close.order_command_id,
                at=old + timedelta(minutes=1, seconds=1),
                reason="close outcome unknown",
            )
            worker = DemoReconciliationWorkerV2(
                config,
                store,
                kernel,
                PortfolioClient(
                    [
                        {
                            "instrumentID": 1001,
                            "positionID": "bp-1",
                            "units": "1",
                            "openRate": "100",
                        }
                    ]
                ),
                grace_seconds=30,
            )

            self.assertEqual(worker.run_once(), 1)
            self.assertEqual(
                store.broker_order(close.order_command_id).status,
                OrderStatus.RECONCILED_ABSENT,
            )
            self.assertEqual(store.positions(open_only=True)[0].quantity, Decimal("1"))
            case = store.reconciliation_case(close.order_command_id)
            self.assertIsNotNone(case)
            assert case is not None
            self.assertEqual(case.status, "RESOLVED_ABSENT")
            store.close()

    def test_unknown_close_without_exact_fill_truth_requires_manual_review(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = RuntimeStoreV2(Path(folder) / "runtime.sqlite3")
            old = datetime.now(UTC) - timedelta(minutes=5)
            config, kernel, command = open_command(store, old)
            kernel.begin_submit(command.order_command_id, old)
            position = kernel.apply_fill(
                Fill(
                    "fill-open",
                    command.order_command_id,
                    command.client_order_id,
                    "bo-open",
                    "bp-1",
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
            close = kernel.create_close_command(
                position,
                now=old + timedelta(minutes=1),
                reason=ExitReason.AGENT_CLOSE,
                units_to_deduct=Decimal("0.4"),
            )
            kernel.begin_submit(close.order_command_id, old + timedelta(minutes=1))
            kernel.mark_unknown(
                close.order_command_id,
                at=old + timedelta(minutes=1, seconds=1),
                reason="partial close outcome unknown",
            )
            store.set_trading_state(
                "ACTIVE", actor="test", reason="exercise fail-closed reconciliation"
            )
            worker = DemoReconciliationWorkerV2(
                config,
                store,
                kernel,
                PortfolioClient(
                    [
                        {
                            "instrumentID": 1001,
                            "positionID": "bp-1",
                            "units": "0.6",
                            "openRate": "100",
                        }
                    ]
                ),
                grace_seconds=30,
            )

            self.assertEqual(worker.run_once(), 1)
            self.assertEqual(
                store.broker_order(close.order_command_id).status,
                OrderStatus.MANUAL_REVIEW,
            )
            self.assertEqual(store.positions(open_only=True)[0].quantity, Decimal("1"))
            self.assertEqual(store.state_get("trading_state"), "LOCKED")
            store.close()

    def test_pending_close_reference_prevents_false_absent_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = RuntimeStoreV2(Path(folder) / "runtime.sqlite3")
            old = datetime.now(UTC) - timedelta(minutes=5)
            config, kernel, command = open_command(store, old)
            kernel.begin_submit(command.order_command_id, old)
            position = kernel.apply_fill(
                Fill(
                    "fill-open",
                    command.order_command_id,
                    command.client_order_id,
                    "bo-open",
                    "bp-1",
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
            close = kernel.create_close_command(
                position,
                now=old + timedelta(minutes=1),
                reason=ExitReason.AGENT_CLOSE,
            )
            kernel.begin_submit(close.order_command_id, old + timedelta(minutes=1))
            kernel.mark_unknown(
                close.order_command_id,
                at=old + timedelta(minutes=1, seconds=1),
                reason="close outcome unknown",
            )
            worker = DemoReconciliationWorkerV2(
                config,
                store,
                kernel,
                PortfolioClient(
                    [
                        {
                            "instrumentID": 1001,
                            "positionID": "bp-1",
                            "units": "1",
                            "openRate": "100",
                        }
                    ],
                    pending=[{"positionIds": ["bp-1"]}],
                ),
                grace_seconds=30,
            )

            self.assertEqual(worker.run_once(), 0)
            self.assertEqual(
                store.broker_order(close.order_command_id).status,
                OrderStatus.UNKNOWN,
            )
            store.close()


if __name__ == "__main__":
    unittest.main()
