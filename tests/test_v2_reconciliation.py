from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from etoro_agent.broker_truth_v2 import broker_truth_v2
from etoro_agent.config_v2 import load_config_v2
from etoro_agent.domain_v2 import (
    ExitReason,
    Fill,
    IntentEnvelope,
    OrderStatus,
    QuoteProvenance,
    Side,
)
from etoro_agent.etoro_api_current_v2 import ApiResponse, EtoroPublicApiDemoClientV2
from etoro_agent.kernel_v2 import UnifiedTradingKernel
from etoro_agent.reconciliation_v2 import DemoReconciliationWorkerV2
from etoro_agent.risk_v2 import BrokerTruth, GlobalRiskKernel
from etoro_agent.runtime_store_v2 import RuntimeStoreV2


class PortfolioClient:
    def __init__(
        self,
        positions: list[dict[str, Any]],
        pending: list[dict[str, Any]] | None = None,
        orders_for_open: list[dict[str, Any]] | None = None,
    ) -> None:
        self.positions = positions
        self.pending = pending or []
        self.orders_for_open = orders_for_open or []

    def demo_portfolio(self) -> ApiResponse:
        return ApiResponse(
            200,
            {
                "clientPortfolio": {
                    "positions": self.positions,
                    "orders": self.pending,
                    "ordersForOpen": self.orders_for_open,
                }
            },
            "reconciliation-test",
        )


class BrokerTruthClient(PortfolioClient):
    def __init__(
        self,
        positions: list[dict[str, Any]],
        *,
        lookup: dict[str, Any] | None = None,
        close: dict[str, Any] | None = None,
        history: list[dict[str, Any]] | None = None,
        history_pages: dict[int, list[dict[str, Any]]] | None = None,
        pending: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(positions, pending)
        self.lookup = lookup
        self.close = close
        self.history = history or []
        self.history_pages = history_pages

    def order_lookup(self, **_kwargs: object) -> ApiResponse:
        if self.lookup is None:
            return ApiResponse(404, {}, "lookup")
        return ApiResponse(200, self.lookup, "lookup")

    def close_order_information(self, _order_id: str) -> ApiResponse:
        if self.close is None:
            return ApiResponse(404, {}, "close")
        return ApiResponse(200, self.close, "close")

    def trading_history(self, **kwargs: object) -> ApiResponse:
        page = int(kwargs.get("page", 1))
        body = self.history if self.history_pages is None else self.history_pages.get(page, [])
        return ApiResponse(200, body, f"history-{page}")


def reduce_broker(now: datetime, *, reconciliation_ok: bool = True) -> BrokerTruth:
    return BrokerTruth(
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
        "reduce-broker-snapshot",
        now,
        reconciliation_ok=reconciliation_ok,
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
    def test_invalid_portfolio_identity_collections_lock_without_projection(self) -> None:
        invalid_snapshots = (
            (
                [
                    {"positionID": "bp-1"},
                    {"positionId": "bp-1"},
                ],
                [],
                [],
                "duplicated",
            ),
            ([{"instrumentID": 1001}], [], [], "missing"),
            (
                [{"positionID": "bp-1", "positionId": "bp-2"}],
                [],
                [],
                "conflict",
            ),
            (
                [],
                [{"orderID": "bo-1"}, {"orderId": "bo-1"}],
                [],
                "duplicated",
            ),
            (
                [],
                [{"orderID": "bo-1"}],
                [{"orderId": "bo-1"}],
                "overlap",
            ),
            (
                [],
                [{"orderID": "bo-1", "orderId": "bo-2"}],
                [],
                "conflict",
            ),
            (
                [],
                [{"orderID": "bo-1", "referenceID": "ref-1", "requestId": "ref-2"}],
                [],
                "conflict",
            ),
        )
        for positions, orders, orders_for_open, expected in invalid_snapshots:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as folder:
                store = RuntimeStoreV2(Path(folder) / "runtime.sqlite3")
                old = datetime.now(UTC) - timedelta(minutes=5)
                config, kernel, command = open_command(store, old)
                kernel.begin_submit(command.order_command_id, old)
                kernel.mark_unknown(
                    command.order_command_id,
                    at=old + timedelta(seconds=1),
                    reason="exercise invalid broker portfolio",
                )
                store.set_trading_state(
                    "ACTIVE", actor="test", reason="exercise strict portfolio validation"
                )
                worker = DemoReconciliationWorkerV2(
                    config,
                    store,
                    kernel,
                    PortfolioClient(positions, orders, orders_for_open),
                    grace_seconds=30,
                )

                with self.assertRaisesRegex(RuntimeError, "strict validation"):
                    worker.run_once()

                self.assertEqual(store.state_get("trading_state"), "LOCKED")
                self.assertEqual(
                    store.broker_order(command.order_command_id).status,
                    OrderStatus.UNKNOWN,
                )
                self.assertEqual(store.fills_for_order(command.order_command_id), ())
                self.assertEqual(store.positions(open_only=True), ())
                heartbeat = store.db.execute(
                    "SELECT status,details_json FROM v2_service_heartbeats "
                    "WHERE service='v2-reconciliation'"
                ).fetchone()
                self.assertIsNotNone(heartbeat)
                assert heartbeat is not None
                self.assertEqual(heartbeat["status"], "error")
                details = json.loads(heartbeat["details_json"])
                self.assertEqual(details["phase"], "portfolio_validation")
                self.assertFalse(details["broker_snapshot_valid"])
                self.assertFalse(details["fills_or_positions_mutated"])
                store.close()

    def test_distinct_broker_and_client_ids_match_reconciliation_and_broker_truth(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = RuntimeStoreV2(Path(folder) / "runtime.sqlite3")
            old = datetime.now(UTC) - timedelta(minutes=5)
            config, kernel, command = open_command(store, old)
            kernel.begin_submit(command.order_command_id, old)
            kernel.acknowledge(
                command.order_command_id,
                at=old + timedelta(seconds=1),
                broker_order_id="701",
                broker_position_id=None,
            )
            kernel.mark_unknown(
                command.order_command_id,
                at=old + timedelta(seconds=2),
                reason="exercise dual broker/client identity",
            )
            pending_row = {
                "orderID": 701,
                "orderId": 701,
                "referenceID": command.client_order_id,
                "requestId": command.client_order_id,
                "amount": "10",
            }
            portfolio_client = PortfolioClient([], [pending_row])
            worker = DemoReconciliationWorkerV2(
                config,
                store,
                kernel,
                portfolio_client,
                grace_seconds=30,
            )
            _, pending, _ = worker._portfolio()
            self.assertTrue(
                worker._pending_mentions(
                    command,
                    store.broker_order(command.order_command_id),
                    pending,
                )
            )

            account_client = EtoroPublicApiDemoClientV2()
            observed = datetime.now(UTC)
            account_client.demo_pnl = lambda: ApiResponse(  # type: ignore[method-assign]
                200,
                {
                    "clientPortfolio": {
                        "credit": "1000",
                        "positions": [],
                        "ordersForOpen": [],
                        "orders": [pending_row],
                    }
                },
                "request",
                observed,
                observed,
                observed,
            )
            truth = broker_truth_v2(store, account_client, config=config, now=observed)
            self.assertTrue(truth.reconciliation_ok)
            self.assertEqual(truth.reconciliation_detail, ())
            self.assertEqual(truth.pending_order_notional_usd, Decimal("10"))
            self.assertEqual(store.fills_for_order(command.order_command_id), ())
            self.assertEqual(store.positions(open_only=True), ())
            store.close()

    def test_ack_with_only_order_id_resolves_position_and_exact_open_fill(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = RuntimeStoreV2(Path(folder) / "runtime.sqlite3")
            old = datetime.now(UTC) - timedelta(minutes=5)
            config, kernel, command = open_command(store, old)
            kernel.begin_submit(command.order_command_id, old)
            kernel.acknowledge(
                command.order_command_id,
                at=old + timedelta(seconds=1),
                broker_order_id="701",
                broker_position_id=None,
            )
            executed = old + timedelta(seconds=2)
            lookup = {
                "orderId": 701,
                "action": "open",
                "status": {"name": "Filled"},
                "asset": {"symbol": "AAPL", "instrumentId": 1001, "side": "long"},
                "totalCosts": "0.25",
                "positionExecutions": [
                    {
                        "positionId": 9001,
                        "state": "open",
                        "openingData": {
                            "executionTime": executed.isoformat(),
                            "units": "1",
                            "avgPrice": "100",
                            "fees": "0.25",
                            "taxes": "0",
                        },
                    }
                ],
                "lastUpdate": executed.isoformat(),
            }
            worker = DemoReconciliationWorkerV2(
                config,
                store,
                kernel,
                BrokerTruthClient(
                    [
                        {
                            "instrumentID": 1001,
                            "positionID": "9001",
                            "units": "1",
                            "openRate": "100",
                            "isBuy": True,
                        }
                    ],
                    lookup=lookup,
                ),
                grace_seconds=30,
            )

            self.assertEqual(worker.run_once(), 1)
            order = store.broker_order(command.order_command_id)
            self.assertEqual(order.status, OrderStatus.FILLED)
            self.assertEqual(order.broker_position_id, "9001")
            fill = store.fills_for_order(command.order_command_id)[0]
            self.assertEqual(fill.fee_usd, Decimal("0.25"))
            self.assertEqual(fill.event_time, executed)
            self.assertEqual(fill.broker_costs_source, "order_lookup.totalCosts")
            store.close()

    def test_full_and_partial_close_lookup_project_exact_fill(self) -> None:
        for units, remaining in ((None, Decimal("0")), (Decimal("0.4"), Decimal("0.6"))):
            with self.subTest(units=units), tempfile.TemporaryDirectory() as folder:
                store = RuntimeStoreV2(Path(folder) / "runtime.sqlite3")
                old = datetime.now(UTC) - timedelta(minutes=5)
                config, kernel, opening = open_command(store, old)
                kernel.begin_submit(opening.order_command_id, old)
                position = kernel.apply_fill(
                    Fill(
                        "lookup-close-open",
                        opening.order_command_id,
                        opening.client_order_id,
                        "700",
                        "9001",
                        "AAPL",
                        Side.BUY,
                        Decimal("1"),
                        Decimal("100"),
                        Decimal("0"),
                        Decimal("0"),
                        old,
                        old,
                        "lookup-close-open",
                    ),
                    final=True,
                )
                close_command = kernel.create_close_command(
                    position,
                    now=old + timedelta(minutes=1),
                    reason=ExitReason.REDUCE_ONLY,
                    broker=reduce_broker(old + timedelta(minutes=1)),
                    units_to_deduct=units,
                )
                kernel.begin_submit(close_command.order_command_id, old + timedelta(minutes=1))
                kernel.acknowledge(
                    close_command.order_command_id,
                    at=old + timedelta(minutes=1, seconds=1),
                    broker_order_id="702",
                    broker_position_id="9001",
                )
                close_units = Decimal("1") if units is None else units
                executed = old + timedelta(minutes=1, seconds=2)
                lookup = {
                    "orderId": 702,
                    "action": "close",
                    "status": {"name": "Filled"},
                    "asset": {"symbol": "AAPL", "instrumentId": 1001, "side": "long"},
                    "totalCosts": "0.10",
                    "positionExecutions": [],
                    "lastUpdate": executed.isoformat(),
                }
                close_info = {
                    "orderID": 702,
                    "instrumentID": 1001,
                    "requestOccurred": executed.isoformat(),
                    "positions": [
                        {
                            "positionID": 9001,
                            "occurred": executed.isoformat(),
                            "rate": "105",
                            "units": str(close_units),
                        }
                    ],
                }
                broker_positions = (
                    []
                    if remaining == 0
                    else [
                        {
                            "instrumentID": 1001,
                            "positionID": "9001",
                            "units": str(remaining),
                            "openRate": "100",
                            "isBuy": True,
                        }
                    ]
                )
                worker = DemoReconciliationWorkerV2(
                    config,
                    store,
                    kernel,
                    BrokerTruthClient(
                        broker_positions,
                        lookup=lookup,
                        close=close_info,
                    ),
                    grace_seconds=30,
                )

                self.assertEqual(worker.run_once(), 1)
                projected = store.positions("master_1000")[-1]
                self.assertEqual(projected.quantity, remaining)
                fill = store.fills_for_order(close_command.order_command_id)[0]
                self.assertEqual(fill.quantity, close_units)
                self.assertEqual(fill.price, Decimal("105"))
                self.assertEqual(fill.fee_usd, Decimal("0.10"))
                self.assertEqual(fill.event_time, executed)
                store.close()

    def test_history_close_without_exact_broker_reason_is_unclassified(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = RuntimeStoreV2(Path(folder) / "runtime.sqlite3")
            old = datetime.now(UTC) - timedelta(days=1)
            config, kernel, opening = open_command(store, old)
            kernel.begin_submit(opening.order_command_id, old)
            position = kernel.apply_fill(
                Fill(
                    "history-open",
                    opening.order_command_id,
                    opening.client_order_id,
                    "700",
                    "9001",
                    "AAPL",
                    Side.BUY,
                    Decimal("1"),
                    Decimal("100"),
                    Decimal("0.20"),
                    Decimal("0"),
                    old,
                    old,
                    "history-open",
                ),
                final=True,
            )
            closed_at = old + timedelta(hours=2)
            history = [
                {
                    "netProfit": "-5.50",
                    "closeRate": "95",
                    "closeTimestamp": closed_at.isoformat(),
                    "positionId": 9001,
                    "instrumentId": 1001,
                    "isBuy": True,
                    "openRate": "100",
                    "openTimestamp": old.isoformat(),
                    "orderId": 799,
                    "fees": "0.10",
                    "units": "1",
                }
            ]
            worker = DemoReconciliationWorkerV2(
                config,
                store,
                kernel,
                BrokerTruthClient([], history=history),
                grace_seconds=30,
            )

            self.assertEqual(worker.run_once(), 1)
            projected = [
                item for item in store.positions() if item.position_id == position.position_id
            ][0]
            self.assertEqual(projected.status.value, "CLOSED")
            self.assertEqual(projected.exit_reason, ExitReason.UNCLASSIFIED_BROKER)
            self.assertEqual(projected.realized_pnl, Decimal("-5.50"))
            external_orders = [
                store.order_command(order.order_command_id)
                for order in store.broker_orders_by_status(("RECONCILED_FILLED",))
            ]
            self.assertEqual(len(external_orders), 1)
            self.assertTrue(external_orders[0].reconciliation_only)
            self.assertNotIn(
                external_orders[0].order_command_id,
                {str(item["payload"]["order_command_id"]) for item in store.pending_outbox()},
            )
            store.close()

    def test_history_paginates_until_target_on_second_page(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = RuntimeStoreV2(Path(folder) / "runtime.sqlite3")
            config = load_config_v2("config/v2-demo-execution.json")
            kernel = UnifiedTradingKernel(store, GlobalRiskKernel(config.mandate))
            first_page = [{"orderId": index} for index in range(1, 1001)]
            target = {"orderId": 1001, "positionId": 9001}
            worker = DemoReconciliationWorkerV2(
                config,
                store,
                kernel,
                BrokerTruthClient(
                    [],
                    history_pages={1: first_page, 2: [target]},
                ),
            )

            rows = worker._history(datetime.now(UTC) - timedelta(days=1))

            self.assertEqual(len(rows), 1001)
            self.assertEqual(rows[-1], target)
            evidence = store.state_get("v2_reconciliation_history_evidence")
            self.assertIsNotNone(evidence)
            self.assertIn('"pages":2', str(evidence).replace(" ", ""))
            store.close()

    def test_unknown_open_snapshot_never_fabricates_fill_economics(self) -> None:
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
                OrderStatus.MANUAL_REVIEW,
            )
            positions = store.positions("master_1000", open_only=True)
            self.assertEqual(positions, ())
            self.assertEqual(store.fills_for_order(command.order_command_id), ())
            case = store.reconciliation_case(command.order_command_id)
            self.assertIsNotNone(case)
            assert case is not None
            self.assertEqual(case.status, "MANUAL_REVIEW")
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
                broker=reduce_broker(old + timedelta(minutes=1)),
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
                broker=reduce_broker(old + timedelta(minutes=1)),
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
                broker=reduce_broker(old + timedelta(minutes=1)),
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
                    pending=[{"orderID": "pending-close-1", "positionIds": ["bp-1"]}],
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
