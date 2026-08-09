from __future__ import annotations

import dataclasses
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from etoro_agent.audit import AuditLog
from etoro_agent.config import RiskLimits
from etoro_agent.execution import EtoroDemoBroker
from etoro_agent.mcp import MCPResult
from etoro_agent.models import KillState, RiskContext, Side, TradeIntent
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
    def __init__(self, *, fail_write: bool = False) -> None:
        self.writes = 0
        self.fail_write = fail_write

    def verify_demo_scope(self):
        return {"scopes": ["etoro-public:demo:write"]}

    def execute_read(self, path: str, query=None, body=None) -> MCPResult:
        if path.endswith("/eligibility"):
            payload = {"eligibilities": [{"allowOpenPosition": True}]}
        else:
            payload = {"estimated": True}
        return MCPResult(200, True, payload, "read", {})

    def execute_demo_order(self, route: str, body_json: str, request_id: str) -> MCPResult:
        self.writes += 1
        if self.fail_write:
            raise TimeoutError("simulated timeout")
        return MCPResult(200, True, {"orderId": 123}, request_id, {})


class ExecutionTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
