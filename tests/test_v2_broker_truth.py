from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from etoro_agent.broker_truth_v2 import broker_truth_v2
from etoro_agent.config_v2 import load_config_v2
from etoro_agent.domain_v2 import (
    DomainEvent,
    IntentEnvelope,
    PositionState,
    QuoteProvenance,
    Side,
)
from etoro_agent.etoro_api_current_v2 import BrokerAccountSnapshotV2
from etoro_agent.kernel_v2 import UnifiedTradingKernel
from etoro_agent.risk_v2 import BrokerTruth, GlobalRiskKernel
from etoro_agent.runtime_store_v2 import RuntimeStoreV2

NOW = datetime(2026, 8, 12, 12, tzinfo=UTC)


def account_snapshot(
    *,
    positions: tuple[dict[str, object], ...] = (),
    pending: tuple[dict[str, object], ...] = (),
) -> BrokerAccountSnapshotV2:
    return BrokerAccountSnapshotV2(
        "test-broker-account-v2",
        "request-test",
        "b" * 64,
        NOW,
        NOW,
        NOW,
        NOW,
        Decimal("1000"),
        Decimal("900"),
        Decimal("100") if positions else Decimal("0"),
        Decimal("0"),
        Decimal("1000"),
        Decimal("100") if positions else Decimal("0"),
        Decimal("0"),
        sum((Decimal(str(row.get("amount", "0"))) for row in pending), Decimal("0")),
        positions,
        (),
        pending,
        (),
    )


def save_local_position(store: RuntimeStoreV2) -> None:
    position = PositionState(
        "position-local",
        "master_1000",
        "test-strategy",
        "A_deterministic",
        "v2",
        "intent-position",
        "AAPL",
        Side.BUY,
        Decimal("1"),
        Decimal("100"),
        NOW,
        NOW,
        Decimal("98"),
        Decimal("104"),
        Decimal("0.02"),
        Decimal("0.04"),
        3600,
        NOW + timedelta(hours=1),
        broker_position_id="9001",
    )
    event = DomainEvent(
        "evt-position-local",
        "PositionProjected",
        2,
        NOW,
        NOW,
        "position-local",
        "intent-position",
        "position-local",
        {},
    )
    store.save_position(position, event)


def open_intent(identity: str) -> IntentEnvelope:
    return IntentEnvelope(
        identity,
        "master_1000",
        "A_deterministic",
        "test-strategy",
        "v2",
        "AAPL",
        Side.BUY,
        Decimal("25"),
        Decimal("0.8"),
        Decimal("0.6"),
        Decimal("0.02"),
        Decimal("0.04"),
        3600,
        NOW,
        NOW,
        NOW + timedelta(minutes=5),
        Decimal("99.9"),
        Decimal("100"),
        Decimal("20"),
        Decimal("10"),
        "feature",
        correlation_id=identity,
    )


def quote() -> QuoteProvenance:
    return QuoteProvenance(
        "AAPL",
        Decimal("99.9"),
        Decimal("100"),
        NOW,
        NOW,
        "test",
        "quote-1",
        "market-1",
        "b" * 64,
    )


def clean_truth() -> BrokerTruth:
    return BrokerTruth(
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
        "b" * 64,
        NOW,
    )


class BrokerTruthAliasTests(unittest.TestCase):
    def test_all_position_economic_aliases_must_normalize_and_agree(self) -> None:
        config = load_config_v2("config/v2-demo.json")
        valid = {
            "positionID": "9001",
            "instrumentID": 1001,
            "instrumentId": "1001.0",
            "units": "1.0",
            "quantity": 1,
            "unitsOwned": Decimal("1.00"),
            "netUnits": "-1",
            "openRate": "100.0",
            "averageOpenRate": 100,
            "entryPrice": Decimal("100.00"),
            "isBuy": True,
        }
        conflicts = (
            {**valid, "instrumentId": 1002},
            {**valid, "unitsOwned": "2"},
            {**valid, "entryPrice": "101"},
            {**valid, "instrumentID": True},
            {**valid, "quantity": False},
            {**valid, "openRate": "Infinity"},
        )
        with tempfile.TemporaryDirectory() as folder:
            store = RuntimeStoreV2(Path(folder) / "runtime.sqlite3")
            save_local_position(store)
            truth = broker_truth_v2(
                store,
                object(),  # type: ignore[arg-type]
                config=config,
                now=NOW,
                snapshot=account_snapshot(positions=(valid,)),
            )
            self.assertTrue(truth.reconciliation_ok, truth.reconciliation_detail)

            for index, row in enumerate(conflicts):
                with self.subTest(index=index):
                    rejected = broker_truth_v2(
                        store,
                        object(),  # type: ignore[arg-type]
                        config=config,
                        now=NOW,
                        snapshot=account_snapshot(positions=(row,)),
                    )
                    self.assertFalse(rejected.reconciliation_ok)
                    self.assertIn("invalid_economics:9001", rejected.reconciliation_detail)

            conflicted = broker_truth_v2(
                store,
                object(),  # type: ignore[arg-type]
                config=config,
                now=NOW,
                snapshot=account_snapshot(positions=(conflicts[1],)),
            )
            kernel = UnifiedTradingKernel(store, GlobalRiskKernel(config.mandate))
            decision, command = kernel.submit_open_intent(
                open_intent("intent-conflicted-alias"), quote(), conflicted, now=NOW
            )
            self.assertFalse(decision.approved)
            self.assertIn("reconciliation_drift", decision.reasons)
            self.assertIsNone(command)
            self.assertEqual(
                store.db.execute("SELECT COUNT(*) FROM v2_outbox").fetchone()[0],
                0,
            )
            store.close()

    def test_pending_order_crossed_identity_pair_cannot_match_two_rows(self) -> None:
        config = load_config_v2("config/v2-demo.json")
        with tempfile.TemporaryDirectory() as folder:
            store = RuntimeStoreV2(Path(folder) / "runtime.sqlite3")
            kernel = UnifiedTradingKernel(store, GlobalRiskKernel(config.mandate))
            decision, command = kernel.submit_open_intent(
                open_intent("intent-pending-pair"), quote(), clean_truth(), now=NOW
            )
            self.assertTrue(decision.approved)
            assert command is not None
            kernel.begin_submit(command.order_command_id, NOW)
            kernel.acknowledge(
                command.order_command_id,
                at=NOW,
                broker_order_id="701",
                broker_position_id=None,
            )
            kernel.mark_unknown(command.order_command_id, at=NOW, reason="test pending identity")
            pending = (
                {"orderID": 701, "referenceID": "different-reference", "amount": "10"},
                {"orderID": 702, "referenceID": command.client_order_id, "amount": "10"},
            )
            truth = broker_truth_v2(
                store,
                object(),  # type: ignore[arg-type]
                config=config,
                now=NOW,
                snapshot=account_snapshot(pending=pending),
            )
            self.assertFalse(truth.reconciliation_ok)
            self.assertIn(
                f"pending_order_unresolved:{command.order_command_id}",
                truth.reconciliation_detail,
            )
            store.close()


if __name__ == "__main__":
    unittest.main()
