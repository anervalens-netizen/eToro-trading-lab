from __future__ import annotations

import inspect
import unittest
from pathlib import Path

from etoro_agent import execution, mcp, risk
from etoro_agent.mcp import EtoroMCPClient


class SecurityBoundaryTests(unittest.TestCase):
    def test_executor_service_receives_only_public_risk_key(self) -> None:
        service = (
            Path(__file__).resolve().parents[1] / "ops/systemd/etoro-demo-executor.service"
        ).read_text(encoding="utf-8")
        self.assertNotIn("ETORO_RISK_SIGNING_KEY_FILE", service)
        self.assertNotIn("risk-signing.key", service)
        self.assertIn("risk-verifying.pub", service)

    def test_runtime_has_no_public_generic_mcp_tool_api(self) -> None:
        self.assertFalse(hasattr(EtoroMCPClient(), "call_tool"))
        with self.assertRaises(PermissionError):
            EtoroMCPClient().execute_read("/api/v2/trading/execution/orders")

    def test_execution_modules_contain_no_real_order_route(self) -> None:
        forbidden = "/api/v2/trading/execution/" + "orders"
        for module in (mcp, execution, risk):
            with self.subTest(module=module.__name__):
                self.assertNotIn(forbidden, inspect.getsource(module))


if __name__ == "__main__":
    unittest.main()
