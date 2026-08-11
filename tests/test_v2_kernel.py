from __future__ import annotations

import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from etoro_agent.compatibility_v2 import (
    BrokerInstrumentRules,
    CompatibilityValidator,
    StrategyExecutionProfile,
)
from etoro_agent.domain_v2 import (
    AuditIntegrityError,
    CompatibilityStatus,
    DomainEvent,
    ExitReason,
    Fill,
    IntentEnvelope,
    OrderStatus,
    PositionState,
    QuoteProvenance,
    Side,
)
from etoro_agent.exits_v2 import BarObservation, ExitContext, ExitEvaluator
from etoro_agent.kernel_v2 import UnifiedTradingKernel
from etoro_agent.risk_v2 import BrokerTruth, CapitalMandate, GlobalRiskKernel
from etoro_agent.runtime_store_v2 import RuntimeStoreV2

NOW = datetime(2026, 8, 10, 12, tzinfo=UTC)


def mandate() -> CapitalMandate:
    return CapitalMandate(
        frozenset({"AAPL", "OIL"}),
        Decimal("1000"),
        Decimal("20"),
        Decimal("1000"),
        Decimal("1000"),
        1,
        Decimal("25"),
        Decimal("40"),
        Decimal("50"),
        Decimal("0.06"),
        Decimal("0.10"),
        30,
        Decimal("50"),
        Decimal("50"),
        0,
        1,
    )


def quote(
    symbol: str = "AAPL", bid: str = "99.4", ask: str = "99.6", broker_hash: str = "broker"
) -> QuoteProvenance:
    return QuoteProvenance(
        symbol, Decimal(bid), Decimal(ask), NOW, NOW, "test", "q1", "market", broker_hash
    )


def intent(symbol: str = "AAPL", amount: str = "100", confidence: str = "0.7") -> IntentEnvelope:
    return IntentEnvelope(
        "intent-1",
        "master",
        "A",
        "trend",
        "2",
        symbol,
        Side.BUY,
        Decimal(amount),
        Decimal(confidence),
        Decimal("0.6"),
        Decimal("0.02"),
        Decimal("0.04"),
        3600,
        NOW,
        NOW,
        NOW + timedelta(minutes=10),
        Decimal("99"),
        Decimal("100"),
        Decimal("200"),
        Decimal("20"),
        "market",
        correlation_id="corr-1",
    )


def broker(hash_: str = "broker", available: str = "1000") -> BrokerTruth:
    return BrokerTruth(
        Decimal("1000"),
        Decimal("1000"),
        Decimal(available),
        Decimal("0"),
        Decimal("0"),
        0,
        Decimal("0"),
        Decimal("0"),
        Decimal("0"),
        Decimal("0"),
        hash_,
        NOW,
    )


class V2KernelTests(unittest.TestCase):
    def test_event_idempotency_body_conflict_rolls_back_state_and_locks(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = RuntimeStoreV2(Path(folder) / "runtime.sqlite3")
            store.set_trading_state("ACTIVE", actor="test", reason="integrity test")
            original = DomainEvent(
                "evt-integrity-original",
                "IntegrityProbe",
                2,
                NOW,
                NOW,
                "integrity-idempotency",
                "",
                "integrity",
                {"value": "original"},
            )
            store.append_event(original)
            projected = PositionState(
                "position-integrity",
                "master",
                "trend",
                "A",
                "2",
                "intent-integrity",
                "AAPL",
                Side.BUY,
                Decimal("1"),
                Decimal("100"),
                NOW,
                NOW,
                Decimal("95"),
                Decimal("105"),
                Decimal("0.05"),
                Decimal("0.05"),
                3600,
                NOW + timedelta(hours=1),
            )
            conflicting = replace(original, payload={"value": "different"})

            with self.assertRaises(AuditIntegrityError):
                store.save_position(projected, conflicting)

            self.assertEqual(store.positions(), ())
            self.assertEqual(store.state_get("trading_state"), "LOCKED")
            row = store.db.execute(
                """SELECT canonical_body,canonical_body_hash FROM v2_events
                   WHERE idempotency_key='integrity-idempotency'"""
            ).fetchone()
            self.assertIsNotNone(row)
            assert row is not None
            self.assertIn('"original"', str(row[0]))
            self.assertEqual(len(str(row[1])), 64)
            self.assertTrue(store.verify_event_chain())
            store.close()

    def test_subthreshold_signal_cannot_be_promoted_by_flooring(self) -> None:
        with self.assertRaisesRegex(ValueError, "sub-threshold"):
            intent(confidence="0.31")

    def test_quote_provenance_is_mandatory_and_future_rejected(self) -> None:
        with self.assertRaises(ValueError):
            QuoteProvenance("AAPL", Decimal("99"), Decimal("100"), NOW, NOW, "", "q", "m", "b")
        with self.assertRaisesRegex(ValueError, "future"):
            QuoteProvenance(
                "AAPL",
                Decimal("99"),
                Decimal("100"),
                NOW + timedelta(seconds=10),
                NOW,
                "test",
                "q",
                "m",
                "b",
            )

    def test_exit_evaluator_gap_stop_and_stop_priority(self) -> None:
        position = PositionState(
            "p",
            "master",
            "trend",
            "A",
            "2",
            "i",
            "AAPL",
            Side.BUY,
            Decimal("1"),
            Decimal("100"),
            NOW,
            NOW,
            Decimal("95"),
            Decimal("105"),
            Decimal("0.05"),
            Decimal("0.05"),
            3600,
            NOW + timedelta(hours=1),
        )
        gap = ExitEvaluator().evaluate(
            position,
            ExitContext(
                NOW + timedelta(minutes=15),
                quote(bid="89", ask="90"),
                BarObservation(
                    NOW + timedelta(minutes=15),
                    Decimal("90"),
                    Decimal("106"),
                    Decimal("89"),
                    Decimal("100"),
                ),
            ),
        )
        self.assertEqual(gap.reason, ExitReason.GAP_STOP)
        both = ExitEvaluator().evaluate(
            position,
            ExitContext(
                NOW + timedelta(minutes=15),
                quote(bid="99", ask="100"),
                BarObservation(
                    NOW + timedelta(minutes=15),
                    Decimal("100"),
                    Decimal("106"),
                    Decimal("94"),
                    Decimal("100"),
                ),
            ),
        )
        self.assertEqual(both.reason, ExitReason.STOP_LOSS)
        self.assertTrue(both.conservative_intrabar)

    def test_commodity_incompatibility_is_detected_before_preflight(self) -> None:
        validator = CompatibilityValidator(
            max_order_usd=Decimal("1000"),
            max_trade_risk_usd=Decimal("20"),
            max_gross_exposure_usd=Decimal("1000"),
        )
        profile = StrategyExecutionProfile(
            "oil_balanced",
            "OIL",
            Decimal("100"),
            Decimal("100"),
            Decimal("0.03"),
            Decimal("0.03"),
        )
        rules = BrokerInstrumentRules(
            "OIL", Decimal("1000"), None, Decimal("0.01"), Decimal("0.10")
        )
        result = validator.validate(profile, rules)
        self.assertEqual(result.status, CompatibilityStatus.INVALID)
        self.assertIn("amount_range_empty", result.reasons)

    def test_decision_claim_is_crash_safe_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = RuntimeStoreV2(Path(folder) / "runtime.sqlite3")
            self.assertTrue(
                store.enqueue_decision(
                    "d1",
                    "h" * 64,
                    {"action": "HOLD"},
                    created_at=NOW,
                    expires_at=NOW + timedelta(minutes=10),
                )
            )
            first = store.claim_decision("w1", now=NOW, lease_seconds=10)
            self.assertIsNotNone(first)
            self.assertIsNone(
                store.claim_decision("w2", now=NOW + timedelta(seconds=5), lease_seconds=10)
            )
            reclaimed = store.claim_decision(
                "w2", now=NOW + timedelta(seconds=11), lease_seconds=10
            )
            self.assertIsNotNone(reclaimed)
            self.assertNotEqual(first["claim_token"], reclaimed["claim_token"])
            store.close()

    def test_ack_is_not_fill_and_partial_fill_updates_position_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = RuntimeStoreV2(Path(folder) / "runtime.sqlite3")
            kernel = UnifiedTradingKernel(store, GlobalRiskKernel(mandate()))
            risk, command = kernel.submit_open_intent(intent(), quote(), broker(), now=NOW)
            self.assertTrue(risk.approved)
            assert command is not None
            kernel.begin_submit(command.order_command_id, NOW)
            ack = kernel.acknowledge(
                command.order_command_id, at=NOW, broker_order_id="bo-1", broker_position_id="bp-1"
            )
            self.assertEqual(ack.status, OrderStatus.ACKNOWLEDGED)
            self.assertEqual(store.positions(open_only=True), ())
            position = kernel.apply_fill(
                Fill(
                    "f1",
                    command.order_command_id,
                    command.client_order_id,
                    "bo-1",
                    "bp-1",
                    "AAPL",
                    Side.BUY,
                    Decimal("0.5"),
                    Decimal("100"),
                    Decimal("0.1"),
                    Decimal("0"),
                    NOW,
                    NOW,
                    "fill-1",
                ),
                final=False,
            )
            self.assertEqual(position.quantity, Decimal("0.5"))
            self.assertEqual(
                store.broker_order(command.order_command_id).status, OrderStatus.PARTIALLY_FILLED
            )
            duplicate = kernel.apply_fill(
                Fill(
                    "f1",
                    command.order_command_id,
                    command.client_order_id,
                    "bo-1",
                    "bp-1",
                    "AAPL",
                    Side.BUY,
                    Decimal("0.5"),
                    Decimal("100"),
                    Decimal("0.1"),
                    Decimal("0"),
                    NOW,
                    NOW,
                    "fill-1",
                ),
                final=False,
            )
            self.assertEqual(duplicate.quantity, Decimal("0.5"))
            self.assertTrue(store.verify_event_chain())
            store.close()

    def test_open_and_close_commands_are_idempotent_across_retries(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = RuntimeStoreV2(Path(folder) / "runtime.sqlite3")
            kernel = UnifiedTradingKernel(store, GlobalRiskKernel(mandate()))
            first_risk, first = kernel.submit_open_intent(intent(), quote(), broker(), now=NOW)
            second_risk, second = kernel.submit_open_intent(
                intent(), quote(), broker(), now=NOW + timedelta(seconds=1)
            )
            self.assertTrue(first_risk.approved and second_risk.approved)
            assert first is not None and second is not None
            self.assertEqual(first, second)
            self.assertEqual(first.proposal_source, "sol_master_open")
            self.assertTrue(first.risk_seal)
            self.assertEqual(first.reference_entry, quote().ask)
            self.assertEqual(first.max_loss_usd, Decimal("20"))
            self.assertEqual(len(store.active_risk_reservations()), 1)
            self.assertTrue(kernel.command_verifier().verify(first, now=NOW))
            self.assertFalse(
                kernel.command_verifier().verify(replace(first, amount_usd=Decimal("101")), now=NOW)
            )
            self.assertEqual(
                store.db.execute("SELECT COUNT(*) FROM v2_order_commands").fetchone()[0],
                1,
            )
            self.assertEqual(
                store.db.execute("SELECT COUNT(*) FROM v2_outbox").fetchone()[0],
                1,
            )

            kernel.begin_submit(first.order_command_id, NOW)
            position = kernel.apply_fill(
                Fill(
                    "f-open",
                    first.order_command_id,
                    first.client_order_id,
                    "bo-1",
                    "bp-1",
                    "AAPL",
                    Side.BUY,
                    Decimal("1"),
                    Decimal("100"),
                    Decimal("0"),
                    Decimal("0"),
                    NOW,
                    NOW,
                    "fill-open",
                ),
                final=True,
            )
            self.assertEqual(store.active_risk_reservations(), ())
            with self.assertRaisesRegex(PermissionError, "reduce risk"):
                kernel.create_close_command(
                    position,
                    now=NOW + timedelta(seconds=30),
                    reason=ExitReason.AGENT_CLOSE,
                    broker=replace(broker(), reconciliation_ok=False),
                )
            close_first = kernel.create_close_command(
                position,
                now=NOW + timedelta(minutes=1),
                reason=ExitReason.AGENT_CLOSE,
                broker=broker(),
                units_to_deduct=Decimal("0.4"),
            )
            close_retry = kernel.create_close_command(
                position,
                now=NOW + timedelta(minutes=2),
                reason=ExitReason.AGENT_CLOSE,
                broker=broker(),
                units_to_deduct=Decimal("0.4"),
            )
            self.assertEqual(close_first, close_retry)
            self.assertEqual(close_first.proposal_source, "sol_master_close")
            self.assertTrue(
                kernel.command_verifier().verify(close_first, now=NOW + timedelta(minutes=1))
            )
            self.assertEqual(
                store.db.execute("SELECT COUNT(*) FROM v2_order_commands").fetchone()[0],
                2,
            )
            store.close()

    def test_open_reservation_is_atomic_and_released_only_on_terminal_truth(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = RuntimeStoreV2(Path(folder) / "runtime.sqlite3")
            constrained = replace(mandate(), max_trade_risk_usd=Decimal("5"))
            kernel = UnifiedTradingKernel(store, GlobalRiskKernel(constrained))
            _, first = kernel.submit_open_intent(intent(), quote(), broker(), now=NOW)
            assert first is not None
            second_intent = replace(
                intent(),
                intent_id="intent-2",
                correlation_id="corr-2",
            )

            with self.assertRaisesRegex(PermissionError, "order-slot reservation"):
                kernel.submit_open_intent(second_intent, quote(), broker(), now=NOW)

            self.assertEqual(
                store.db.execute("SELECT COUNT(*) FROM v2_order_commands").fetchone()[0],
                1,
            )
            self.assertEqual(
                store.db.execute("SELECT COUNT(*) FROM v2_outbox").fetchone()[0],
                1,
            )
            self.assertEqual(len(store.active_risk_reservations()), 1)

            kernel.reject_before_send(first.order_command_id, at=NOW, reason="fault injection")
            self.assertEqual(store.active_risk_reservations(), ())
            _, second = kernel.submit_open_intent(second_intent, quote(), broker(), now=NOW)
            assert second is not None
            self.assertEqual(len(store.active_risk_reservations()), 1)
            kernel.begin_submit(second.order_command_id, NOW)
            kernel.mark_unknown(
                second.order_command_id,
                at=NOW,
                reason="fault injection after possible send",
            )
            self.assertEqual(len(store.active_risk_reservations()), 1)
            kernel.reconcile_unknown(
                second.order_command_id,
                at=NOW,
                found=False,
                broker_snapshot_hash="b" * 64,
                detail="exact request identity absent at broker",
            )
            self.assertEqual(store.active_risk_reservations(), ())
            store.close()

    def test_concurrent_open_proposals_share_one_atomic_reservation_budget(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / "runtime.sqlite3"
            bootstrap = RuntimeStoreV2(database)
            bootstrap.close()
            start = threading.Barrier(2)

            def submit(index: int) -> str:
                store = RuntimeStoreV2(database)
                try:
                    kernel = UnifiedTradingKernel(store, GlobalRiskKernel(mandate()))
                    candidate = replace(
                        intent(),
                        intent_id=f"intent-concurrent-{index}",
                        correlation_id=f"corr-concurrent-{index}",
                    )
                    start.wait(timeout=5)
                    kernel.submit_open_intent(candidate, quote(), broker(), now=NOW)
                    return "approved"
                except PermissionError as exc:
                    return str(exc)
                finally:
                    store.close()

            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(submit, (1, 2)))

            self.assertEqual(outcomes.count("approved"), 1)
            self.assertEqual(
                sum("reservation budget exceeded" in outcome for outcome in outcomes),
                1,
            )
            store = RuntimeStoreV2(database)
            self.assertEqual(len(store.active_risk_reservations()), 1)
            self.assertEqual(
                store.db.execute("SELECT COUNT(*) FROM v2_order_commands").fetchone()[0],
                1,
            )
            self.assertEqual(
                store.db.execute("SELECT COUNT(*) FROM v2_outbox").fetchone()[0],
                1,
            )
            store.close()

    def test_fill_must_match_symbol_side_and_immutable_identity(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = RuntimeStoreV2(Path(folder) / "runtime.sqlite3")
            kernel = UnifiedTradingKernel(store, GlobalRiskKernel(mandate()))
            _, command = kernel.submit_open_intent(intent(), quote(), broker(), now=NOW)
            assert command is not None
            kernel.begin_submit(command.order_command_id, NOW)
            wrong = Fill(
                "f-wrong",
                command.order_command_id,
                command.client_order_id,
                "bo-1",
                "bp-1",
                "OIL",
                Side.SELL,
                Decimal("1"),
                Decimal("100"),
                Decimal("0"),
                Decimal("0"),
                NOW,
                NOW,
                "fill-wrong",
            )
            with self.assertRaisesRegex(ValueError, "economic identity"):
                kernel.apply_fill(wrong, final=True)
            valid = Fill(
                "f-valid",
                command.order_command_id,
                command.client_order_id,
                "bo-1",
                "bp-1",
                "AAPL",
                Side.BUY,
                Decimal("1"),
                Decimal("100"),
                Decimal("0"),
                Decimal("0"),
                NOW,
                NOW,
                "fill-valid",
            )
            kernel.apply_fill(valid, final=True)
            rebound = Fill(
                "f-rebound",
                command.order_command_id,
                command.client_order_id,
                "bo-1",
                "bp-1",
                "AAPL",
                Side.BUY,
                Decimal("0.5"),
                Decimal("100"),
                Decimal("0"),
                Decimal("0"),
                NOW,
                NOW,
                "fill-valid",
            )
            with self.assertRaisesRegex(ValueError, "cannot be rebound"):
                kernel.apply_fill(rebound, final=False)
            self.assertEqual(
                store.db.execute("SELECT COUNT(*) FROM v2_fills").fetchone()[0],
                1,
            )
            store.close()

    def test_partial_close_recomputes_remaining_unrealized_pnl(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = RuntimeStoreV2(Path(folder) / "runtime.sqlite3")
            kernel = UnifiedTradingKernel(store, GlobalRiskKernel(mandate()))
            _, command = kernel.submit_open_intent(intent(), quote(), broker(), now=NOW)
            assert command is not None
            kernel.begin_submit(command.order_command_id, NOW)
            position = kernel.apply_fill(
                Fill(
                    "f-open",
                    command.order_command_id,
                    command.client_order_id,
                    "bo-open",
                    "bp-1",
                    "AAPL",
                    Side.BUY,
                    Decimal("1"),
                    Decimal("100"),
                    Decimal("2"),
                    Decimal("0"),
                    NOW,
                    NOW,
                    "fill-open",
                ),
                final=True,
            ).with_mark(Decimal("110"))
            event = DomainEvent(
                "mark-1",
                "PositionMarked",
                2,
                NOW,
                NOW,
                "mark-1",
                position.position_id,
                position.position_id,
                {"mark": "110"},
            )
            store.save_position(position, event)
            close = kernel.create_close_command(
                position,
                now=NOW + timedelta(minutes=1),
                reason=ExitReason.REDUCE_ONLY,
                broker=broker(),
                units_to_deduct=Decimal("0.4"),
            )
            kernel.begin_submit(close.order_command_id, NOW + timedelta(minutes=1))
            reduced = kernel.apply_fill(
                Fill(
                    "f-close",
                    close.order_command_id,
                    close.client_order_id,
                    "bo-close",
                    "bp-1",
                    "AAPL",
                    Side.SELL,
                    Decimal("0.4"),
                    Decimal("105"),
                    Decimal("0.1"),
                    Decimal("0"),
                    NOW + timedelta(minutes=1),
                    NOW + timedelta(minutes=1),
                    "fill-close",
                ),
                final=True,
                exit_reason=ExitReason.REDUCE_ONLY,
            )
            self.assertEqual(reduced.quantity, Decimal("0.6"))
            self.assertEqual(reduced.fees_accrued, Decimal("1.2"))
            self.assertEqual(reduced.realized_pnl, Decimal("1.1"))
            self.assertEqual(reduced.unrealized_pnl, Decimal("1.8"))
            store.close()

    def test_position_cannot_have_concurrent_reduce_only_commands(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = RuntimeStoreV2(Path(folder) / "runtime.sqlite3")
            kernel = UnifiedTradingKernel(store, GlobalRiskKernel(mandate()))
            _, command = kernel.submit_open_intent(intent(), quote(), broker(), now=NOW)
            assert command is not None
            kernel.begin_submit(command.order_command_id, NOW)
            position = kernel.apply_fill(
                Fill(
                    "f-open",
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
                    NOW,
                    NOW,
                    "fill-open",
                ),
                final=True,
            )
            kernel.create_close_command(
                position,
                now=NOW + timedelta(minutes=1),
                reason=ExitReason.REDUCE_ONLY,
                broker=broker(),
                units_to_deduct=Decimal("0.4"),
            )

            with self.assertRaisesRegex(ValueError, "active reduce-only"):
                kernel.create_close_command(
                    position,
                    now=NOW + timedelta(minutes=1),
                    reason=ExitReason.AGENT_CLOSE,
                    broker=broker(),
                    units_to_deduct=Decimal("1"),
                )

            self.assertEqual(
                store.db.execute("SELECT COUNT(*) FROM v2_order_commands").fetchone()[0],
                2,
            )
            store.close()

    def test_stale_intent_and_broker_hash_mismatch_fail_closed(self) -> None:
        risk = GlobalRiskKernel(mandate())
        stale = risk.evaluate_open(intent(), quote(), broker(), NOW + timedelta(minutes=11))
        self.assertFalse(stale.approved)
        self.assertIn("intent_expired_or_not_yet_valid", stale.reasons)
        mismatch = risk.evaluate_open(intent(), quote(broker_hash="x"), broker(hash_="y"), NOW)
        self.assertFalse(mismatch.approved)
        self.assertIn("broker_snapshot_hash_mismatch", mismatch.reasons)

    def test_fresh_quote_risk_grid_preserves_direction_band_and_max_loss(self) -> None:
        evaluated = 0
        for side in (Side.BUY, Side.SELL):
            with self.subTest(side=side), tempfile.TemporaryDirectory() as folder:
                store = RuntimeStoreV2(Path(folder) / "runtime.sqlite3")
                risk = GlobalRiskKernel(mandate())
                kernel = UnifiedTradingKernel(store, risk)
                candidate = replace(
                    intent(),
                    intent_id=f"intent-{side.value}",
                    side=side,
                    correlation_id=f"corr-{side.value}",
                )
                _, command = kernel.submit_open_intent(candidate, quote(), broker(), now=NOW)
                assert command is not None
                assert command.reference_entry is not None
                assert command.min_acceptable_entry is not None
                assert command.max_acceptable_entry is not None
                assert command.stop_loss_fraction is not None
                assert command.take_profit_fraction is not None
                reference = command.reference_entry
                for offset_bps in range(-50, 51):
                    entry = reference * (Decimal("1") + Decimal(offset_bps) / Decimal("10000"))
                    for spread in (Decimal("0.01"), Decimal("0.05"), Decimal("0.10")):
                        fresh = QuoteProvenance(
                            "AAPL",
                            entry - spread if side is Side.BUY else entry,
                            entry if side is Side.BUY else entry + spread,
                            NOW,
                            NOW,
                            "test",
                            f"fresh-{offset_bps}-{spread}",
                            "market",
                            "broker",
                        )
                        stop = entry * (
                            Decimal("1") - command.stop_loss_fraction
                            if side is Side.BUY
                            else Decimal("1") + command.stop_loss_fraction
                        )
                        take = entry * (
                            Decimal("1") + command.take_profit_fraction
                            if side is Side.BUY
                            else Decimal("1") - command.take_profit_fraction
                        )
                        if side is Side.BUY:
                            self.assertLess(stop, entry)
                            self.assertLess(entry, take)
                        else:
                            self.assertLess(take, entry)
                            self.assertLess(entry, stop)
                        for cost in (Decimal("0"), Decimal("0.5"), Decimal("1")):
                            decision = risk.evaluate_fresh_open(
                                command,
                                fresh,
                                known_cost_usd=cost,
                                now=NOW,
                            )
                            self.assertTrue(decision.approved, decision.reasons)
                            evaluated += 1
                outside = command.max_acceptable_entry * Decimal("1.001")
                outside_quote = QuoteProvenance(
                    "AAPL",
                    outside - Decimal("0.01") if side is Side.BUY else outside,
                    outside if side is Side.BUY else outside + Decimal("0.01"),
                    NOW,
                    NOW,
                    "test",
                    "outside",
                    "market",
                    "broker",
                )
                outside_decision = risk.evaluate_fresh_open(
                    command,
                    outside_quote,
                    known_cost_usd=Decimal("0"),
                    now=NOW,
                )
                self.assertIn("signed_execution_band", outside_decision.reasons)
                costly = risk.evaluate_fresh_open(
                    command,
                    quote(),
                    known_cost_usd=Decimal("19"),
                    now=NOW,
                )
                self.assertIn("fresh_max_loss", costly.reasons)
                store.close()
        self.assertEqual(evaluated, 1818)


if __name__ == "__main__":
    unittest.main()
