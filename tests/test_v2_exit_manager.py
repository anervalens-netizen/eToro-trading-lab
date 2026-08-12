from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from etoro_agent.config_v2 import load_config_v2
from etoro_agent.domain_v2 import Fill, IntentEnvelope, QuoteProvenance, Side
from etoro_agent.exit_manager_v2 import DeterministicExitManagerV2
from etoro_agent.kernel_v2 import UnifiedTradingKernel
from etoro_agent.risk_v2 import BrokerTruth, GlobalRiskKernel
from etoro_agent.runtime_store_v2 import RuntimeStoreV2


class V2ExitManagerTests(unittest.TestCase):
    def test_time_stop_creates_close_in_lock_new_without_any_ai_decision(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            config = load_config_v2("config/v2-demo-execution.json")
            store = RuntimeStoreV2(Path(folder) / "runtime.sqlite3")
            kernel = UnifiedTradingKernel(store, GlobalRiskKernel(config.mandate))
            opened_at = datetime.now(UTC) - timedelta(hours=2)
            intent = IntentEnvelope(
                "intent-exit-manager",
                "master_1000",
                "D",
                "time-stop-test",
                "v2",
                "AAPL",
                Side.BUY,
                Decimal("100"),
                Decimal("0.8"),
                Decimal("0.6"),
                Decimal("0.02"),
                Decimal("0.04"),
                60,
                opened_at,
                opened_at,
                opened_at + timedelta(minutes=5),
                Decimal("99.9"),
                Decimal("100"),
                Decimal("50"),
                Decimal("25"),
                "market",
                correlation_id="exit-manager",
            )
            opening_truth = BrokerTruth(
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
                "broker-open",
                opened_at,
            )
            _, command = kernel.submit_open_intent(
                intent,
                QuoteProvenance(
                    "AAPL",
                    Decimal("99.9"),
                    Decimal("100"),
                    opened_at,
                    opened_at,
                    "test",
                    "quote-open",
                    "market",
                    "broker-open",
                ),
                opening_truth,
                now=opened_at,
            )
            assert command is not None
            kernel.begin_submit(command.order_command_id, opened_at)
            position = kernel.apply_fill(
                Fill(
                    "fill-exit-manager-open",
                    command.order_command_id,
                    command.client_order_id,
                    "broker-order-open",
                    "12345",
                    "AAPL",
                    Side.BUY,
                    Decimal("1"),
                    Decimal("100"),
                    Decimal("0"),
                    Decimal("0"),
                    opened_at,
                    opened_at,
                    "fill-exit-manager-open",
                ),
                final=True,
            )
            gate = Path(folder) / "ENABLE_DEMO_EXECUTION"
            gate.write_text("DEMO only\n", encoding="utf-8")
            store.set_trading_state(
                "LOCKED",
                actor="test",
                reason="lock new exposure while preserving deterministic exits",
            )

            def truth(now: datetime) -> BrokerTruth:
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
                    "broker-exit",
                    now,
                )

            def quote(_position, broker: BrokerTruth, now: datetime) -> QuoteProvenance:
                return QuoteProvenance(
                    "AAPL",
                    Decimal("100"),
                    Decimal("100.1"),
                    now,
                    now,
                    "test",
                    "quote-exit",
                    "market-exit",
                    broker.snapshot_hash,
                )

            worker = DeterministicExitManagerV2(
                config,
                store,
                kernel,
                truth,
                quote,
                execution_gate=gate,
            )

            self.assertEqual(worker.run_once(), 1)
            orders = store.broker_orders_by_status(("RISK_APPROVED",))
            self.assertEqual(len(orders), 1)
            close = store.order_command(orders[0].order_command_id)
            self.assertTrue(close.reduce_only)
            self.assertEqual(close.reduce_exit_reason, "TIME_STOP")
            self.assertEqual(close.correlation_id, position.position_id)
            store.close()

    def test_absent_gate_locks_without_creating_exit_command(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            config = load_config_v2("config/v2-demo-execution.json")
            store = RuntimeStoreV2(Path(folder) / "runtime.sqlite3")
            kernel = UnifiedTradingKernel(store, GlobalRiskKernel(config.mandate))
            worker = DeterministicExitManagerV2(
                config,
                store,
                kernel,
                lambda now: (_ for _ in ()).throw(AssertionError("broker read forbidden")),
                lambda position, truth, now: (_ for _ in ()).throw(
                    AssertionError("quote read forbidden")
                ),
                execution_gate=Path(folder) / "absent",
            )

            self.assertEqual(worker.run_once(), 0)
            self.assertEqual(store.state_get("trading_state"), "LOCKED")
            store.close()


if __name__ == "__main__":
    unittest.main()
