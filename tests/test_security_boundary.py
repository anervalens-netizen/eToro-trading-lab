from __future__ import annotations

import inspect
import unittest

from etoro_agent import execution, mcp, risk
from etoro_agent.mcp import EtoroMCPClient


class SecurityBoundaryTests(unittest.TestCase):
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
