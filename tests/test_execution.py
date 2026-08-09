from __future__ import annotations

import dataclasses
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from etoro_agent.audit import AuditLog
from etoro_agent.config import RiskLimits
from etoro_agent.execution import EtoroDemoBroker, authorize_standing_demo
from etoro_agent.mcp import MCPResult
from etoro_agent.models import CloseIntent, KillState, RiskContext, Side, TradeIntent
from etoro_agent.risk import DeterministicRiskEngine


def limits() -> RiskLimits:
    return RiskLimits(
        frozenset({"BTC"}),
        Decimal("100"),
        Decimal("250"),
        Decimal("500"),
        5,
        Decimal("15"),
        Decimal("0.10"),
        1,
        Decimal("0.005"),
        Decimal("0.05"),
        300,
    )


def sealed_order(engine: DeterministicRiskEngine):
    intent = TradeIntent(
        "BTC",
        Side.BUY,
        Decimal("50"),
        Decimal("0.8"),
        "test",
        Decimal("0.03"),
        Decimal("0.06"),
    )
    context = RiskContext(
        equity_usd=Decimal("1000"),
        peak_equity_usd=Decimal("1000"),
        daily_pnl_usd=Decimal("0"),
        gross_exposure_usd=Decimal("0"),
        symbol_exposure_usd=Decimal("0"),
        trades_today=0,
        bid=Decimal("99"),
        ask=Decimal("100"),
        kill_switch_active=False,
    )
    result = engine.evaluate(intent, context)
    assert result.order is not None
    return result.order


class FakeClient:
    def __init__(
        self,
        *,
        fail_write: bool = False,
        position_ids: tuple[int, ...] = (),
        pending_orders: int = 0,
        min_stop_percentage: int = 1,
        fail_post_reconciliation: bool = False,
        quote_age_seconds: int = 0,
        quote_bid: int = 99,
        quote_ask: int = 100,
    ) -> None:
        self.writes = 0
        self.fail_write = fail_write
        self.position_ids = position_ids
        self.pending_orders = pending_orders
        self.min_stop_percentage = min_stop_percentage
        self.fail_post_reconciliation = fail_post_reconciliation
        self.quote_age_seconds = quote_age_seconds
        self.quote_bid = quote_bid
        self.quote_ask = quote_ask

    def verify_isolated_demo_execution_scope(self):
        return {
            "scopes": [
                "etoro-public:trade.demo:read",
                "etoro-public:trade.demo:write",
            ]
        }

    def execute_read(self, path: str, query=None, body=None) -> MCPResult:
        if path.endswith("/portfolio"):
            if self.fail_post_reconciliation and self.writes:
                return MCPResult(500, False, {}, "read", {})
            payload = {
                "clientPortfolio": {
                    "positions": [],
                    "ordersForOpen": [
                        {"orderId": index + 1}
                        for index in range(self.pending_orders)
                    ],
                    "orders": [],
                }
            }
            payload["clientPortfolio"]["positions"] = [
                {"positionID": position_id} for position_id in self.position_ids
            ]
        elif path.endswith("/eligibility"):
            payload = {
                "eligibilities": [
                    {
                        "allowOpenPosition": True,
                        "minPositionExposure": 10,
                        "allowedOrderQuantityType": "all",
                        "leverageConfigs": [
                            {
                                "settlementType": "cfd",
                                "direction": "long",
                                "leverageValues": [1],
                                "minPositionAmount": 10,
                                "allowStopLossTakeProfit": True,
                                "minStopLossPercentage": self.min_stop_percentage,
                                "maxStopLossPercentage": 50,
                                "minTakeProfitPercentage": 1,
                                "maxTakeProfitPercentage": 100,
                            }
                        ],
                    }
                ]
            }
        elif path.endswith("/rates"):
            payload = {
                "rates": [
                    {
                        "bid": self.quote_bid,
                        "ask": self.quote_ask,
                        "date": (
                            datetime.now(timezone.utc)
                            - timedelta(seconds=self.quote_age_seconds)
                        ).isoformat(),
                    }
                ]
            }
        else:
            payload = {"estimated": True}
        return MCPResult(200, True, payload, "read", {})

    def execute_demo_order(self, route: str, body_json: str, request_id: str) -> MCPResult:
        self.writes += 1
        if self.fail_write:
            raise TimeoutError("simulated timeout")
        return MCPResult(200, True, {"orderId": 123}, request_id, {})


class ExecutionTests(unittest.TestCase):
    def test_standing_demo_authorizes_only_sealed_sol_master_source(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            runtime = Path(folder)
            audit = AuditLog(runtime / "audit.sqlite3")
            audit.set_kill_state(KillState.ACTIVE, "test", "ready")
            engine = DeterministicRiskEngine(limits(), b"x" * 32)
            order = sealed_order(engine)
            request = {"account": "DEMO", "body": order.body_json}
            audit.register_proposal(
                order.proposal_id,
                request,
                order,
                source="sol_master_open",
            )
            proposal = audit.proposal(order.proposal_id)
            assert proposal is not None
            self.assertTrue(
                authorize_standing_demo(
                    audit, runtime, engine.verifier(), proposal
                )
            )
            authorized = audit.proposal(order.proposal_id)
            assert authorized is not None
            self.assertEqual(authorized["state"], "APPROVED")
            self.assertEqual(authorized["actor"], "standing-demo-policy")
            self.assertEqual(
                audit.recent_events(1)[0]["event_type"],
                "standing_demo_authorization",
            )

    def test_standing_demo_rejects_manual_source_kill_and_bad_seal(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            runtime = Path(folder)
            audit = AuditLog(runtime / "audit.sqlite3")
            audit.set_kill_state(KillState.ACTIVE, "test", "ready")
            engine = DeterministicRiskEngine(limits(), b"x" * 32)
            manual = sealed_order(engine)
            audit.register_proposal(
                manual.proposal_id, {}, manual, source="manual"
            )
            proposal = audit.proposal(manual.proposal_id)
            assert proposal is not None
            self.assertFalse(
                authorize_standing_demo(
                    audit, runtime, engine.verifier(), proposal
                )
            )

            killed = sealed_order(engine)
            audit.register_proposal(
                killed.proposal_id, {}, killed, source="sol_master_open"
            )
            audit.set_kill_state(KillState.LOCKED, "test", "halt")
            proposal = audit.proposal(killed.proposal_id)
            assert proposal is not None
            self.assertFalse(
                authorize_standing_demo(
                    audit, runtime, engine.verifier(), proposal
                )
            )

            close_context = RiskContext(
                Decimal("1000"),
                Decimal("1000"),
                Decimal("0"),
                Decimal("50"),
                Decimal("50"),
                1,
                Decimal("99"),
                Decimal("100"),
                True,
            )
            close_result = engine.evaluate_close(
                CloseIntent("BTC", 12345, 100000, None, "reduce risk"),
                close_context,
            )
            assert close_result.order is not None
            close_order = close_result.order
            audit.register_proposal(
                close_order.proposal_id,
                {},
                close_order,
                source="sol_master_close",
            )
            close_proposal = audit.proposal(close_order.proposal_id)
            assert close_proposal is not None
            self.assertTrue(
                authorize_standing_demo(
                    audit, runtime, engine.verifier(), close_proposal
                )
            )

            audit.set_kill_state(KillState.ACTIVE, "test", "resume")
            bad = dataclasses.replace(sealed_order(engine), seal="tampered")
            audit.register_proposal(
                bad.proposal_id, {}, bad, source="sol_master_open"
            )
            proposal = audit.proposal(bad.proposal_id)
            assert proposal is not None
            self.assertFalse(
                authorize_standing_demo(
                    audit, runtime, engine.verifier(), proposal
                )
            )

    def test_exact_one_time_approval_produces_at_most_one_demo_write(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            audit = AuditLog(Path(folder) / "audit.sqlite3")
            audit.set_kill_state(KillState.ACTIVE, "test", "ready")
            engine = DeterministicRiskEngine(limits(), b"x" * 32)
            order = sealed_order(engine)
            request = {
                "account": "DEMO",
                "method": order.method,
                "path": order.route,
                "body": order.body_json,
            }
            envelope_hash = audit.register_proposal(order.proposal_id, request, order)
            audit.approve_once(order.proposal_id, envelope_hash, "owner")
            client = FakeClient()
            result = EtoroDemoBroker(client, audit).execute(order, engine.verifier())
            self.assertTrue(result.is_success)
            self.assertEqual(client.writes, 1)
            self.assertEqual(audit.proposal(order.proposal_id)["state"], "ACKNOWLEDGED")
            with self.assertRaises(PermissionError):
                EtoroDemoBroker(client, audit).execute(order, engine.verifier())
            self.assertEqual(client.writes, 1)

    def test_kill_or_tampered_seal_cannot_reach_network(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            audit = AuditLog(Path(folder) / "audit.sqlite3")
            engine = DeterministicRiskEngine(limits(), b"x" * 32)
            order = sealed_order(engine)
            client = FakeClient()
            with self.assertRaises(PermissionError):
                EtoroDemoBroker(client, audit).execute(order, engine.verifier())
            tampered = dataclasses.replace(order, account_mode="REAL")
            self.assertFalse(engine.verifier().verify(tampered))
            self.assertEqual(client.writes, 0)

    def test_filesystem_kill_is_rechecked_by_executor(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            runtime = Path(folder)
            audit = AuditLog(runtime / "audit.sqlite3")
            audit.set_kill_state(KillState.ACTIVE, "test", "ready")
            engine = DeterministicRiskEngine(limits(), b"x" * 32)
            order = sealed_order(engine)
            envelope_hash = audit.register_proposal(order.proposal_id, {}, order)
            audit.approve_once(order.proposal_id, envelope_hash, "owner")
            (runtime / "KILL_SWITCH").touch()
            client = FakeClient()
            with self.assertRaisesRegex(PermissionError, "kill switch"):
                EtoroDemoBroker(client, audit, runtime).execute(
                    order, engine.verifier()
                )
            self.assertEqual(client.writes, 0)
            self.assertEqual(audit.proposal(order.proposal_id)["state"], "APPROVED")

    def test_timeout_is_unknown_and_never_retried_automatically(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            audit = AuditLog(Path(folder) / "audit.sqlite3")
            audit.set_kill_state(KillState.ACTIVE, "test", "ready")
            engine = DeterministicRiskEngine(limits(), b"x" * 32)
            order = sealed_order(engine)
            envelope_hash = audit.register_proposal(order.proposal_id, {}, order)
            audit.approve_once(order.proposal_id, envelope_hash, "owner")
            client = FakeClient(fail_write=True)
            with self.assertRaises(TimeoutError):
                EtoroDemoBroker(client, audit).execute(order, engine.verifier())
            self.assertEqual(client.writes, 1)
            self.assertEqual(audit.proposal(order.proposal_id)["state"], "UNKNOWN")

    def test_broker_stop_bounds_reject_before_write(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            audit = AuditLog(Path(folder) / "audit.sqlite3")
            audit.set_kill_state(KillState.ACTIVE, "test", "ready")
            engine = DeterministicRiskEngine(limits(), b"x" * 32)
            order = sealed_order(engine)
            envelope_hash = audit.register_proposal(order.proposal_id, {}, order)
            audit.approve_once(order.proposal_id, envelope_hash, "owner")
            client = FakeClient(min_stop_percentage=10)
            with self.assertRaisesRegex(PermissionError, "stop-loss"):
                EtoroDemoBroker(client, audit).execute(order, engine.verifier())
            self.assertEqual(client.writes, 0)

    def test_stale_or_wide_broker_quote_rejects_before_write(self) -> None:
        for client, message in (
            (FakeClient(quote_age_seconds=31), "stale"),
            (FakeClient(quote_bid=90, quote_ask=100), "spread"),
        ):
            with self.subTest(message=message), tempfile.TemporaryDirectory() as folder:
                audit = AuditLog(Path(folder) / "audit.sqlite3")
                audit.set_kill_state(KillState.ACTIVE, "test", "ready")
                engine = DeterministicRiskEngine(limits(), b"x" * 32)
                order = sealed_order(engine)
                envelope_hash = audit.register_proposal(order.proposal_id, {}, order)
                audit.approve_once(order.proposal_id, envelope_hash, "owner")
                with self.assertRaisesRegex(PermissionError, message):
                    EtoroDemoBroker(client, audit).execute(order, engine.verifier())
                self.assertEqual(client.writes, 0)

    def test_pending_broker_order_blocks_another_open(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            audit = AuditLog(Path(folder) / "audit.sqlite3")
            audit.set_kill_state(KillState.ACTIVE, "test", "ready")
            engine = DeterministicRiskEngine(limits(), b"x" * 32)
            order = sealed_order(engine)
            envelope_hash = audit.register_proposal(order.proposal_id, {}, order)
            audit.approve_once(order.proposal_id, envelope_hash, "owner")
            client = FakeClient(pending_orders=1)
            with self.assertRaisesRegex(PermissionError, "position/order exposure"):
                EtoroDemoBroker(client, audit).execute(order, engine.verifier())
            self.assertEqual(client.writes, 0)

    def test_failed_post_write_reconciliation_locks_new_opens(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            audit = AuditLog(Path(folder) / "audit.sqlite3")
            audit.set_kill_state(KillState.ACTIVE, "test", "ready")
            engine = DeterministicRiskEngine(limits(), b"x" * 32)
            order = sealed_order(engine)
            envelope_hash = audit.register_proposal(order.proposal_id, {}, order)
            audit.approve_once(order.proposal_id, envelope_hash, "owner")
            client = FakeClient(fail_post_reconciliation=True)
            result = EtoroDemoBroker(client, audit).execute(order, engine.verifier())
            self.assertTrue(result.is_success)
            self.assertEqual(audit.kill_state(), KillState.LOCKED)
            self.assertEqual(
                audit.proposal(order.proposal_id)["state"], "ACKNOWLEDGED"
            )

    def test_kill_allows_only_a_sealed_reduce_only_demo_close(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            audit = AuditLog(Path(folder) / "audit.sqlite3")
            engine = DeterministicRiskEngine(limits(), b"x" * 32)
            context = RiskContext(
                Decimal("1000"), Decimal("1000"), Decimal("0"), Decimal("50"),
                Decimal("50"), 1, Decimal("99"), Decimal("100"), True,
            )
            result = engine.evaluate_close(
                CloseIntent("BTC", 12345, 100000, None, "reduce risk"), context
            )
            assert result.order is not None
            order = result.order
            envelope_hash = audit.register_proposal(order.proposal_id, {}, order)
            audit.approve_once(order.proposal_id, envelope_hash, "owner")
            client = FakeClient(position_ids=(12345,))
            response = EtoroDemoBroker(client, audit).execute(order, engine.verifier())
            self.assertTrue(response.is_success)
            self.assertEqual(client.writes, 1)


if __name__ == "__main__":
    unittest.main()
