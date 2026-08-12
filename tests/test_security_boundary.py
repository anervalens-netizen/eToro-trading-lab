from __future__ import annotations

import inspect
import unittest
from pathlib import Path

from etoro_agent import cli, execution, mcp, risk
from etoro_agent.mcp import EtoroMCPClient


class SecurityBoundaryTests(unittest.TestCase):
    def test_legacy_executor_has_no_installable_or_cli_entrypoint(self) -> None:
        service = Path(__file__).resolve().parents[1] / "ops/systemd/etoro-demo-executor.service"
        self.assertFalse(service.exists())
        source = inspect.getsource(cli)
        self.assertNotIn("demo-executor-once", source)
        self.assertNotIn("demo-executor-worker", source)

    def test_legacy_ai_runners_have_no_service_or_script_entrypoint(self) -> None:
        root = Path(__file__).resolve().parents[1]
        for name in ("etoro-sol-runner.service", "etoro-minimax-runner.service"):
            self.assertFalse((root / "ops/systemd" / name).exists())
        project = (root / "pyproject.toml").read_text(encoding="utf-8")
        self.assertNotIn("etoro-sol-runner =", project)
        self.assertNotIn("etoro-minimax-runner =", project)
        for module in ("sol_runner.py", "minimax_runner.py"):
            source = (root / "src/etoro_agent" / module).read_text(encoding="utf-8")
            self.assertNotIn("def main()", source)
            self.assertNotIn('if __name__ == "__main__"', source)
        installer = (root / "ops/deploy/install-v2-release.sh").read_text(encoding="utf-8")
        self.assertIn("legacy_ai_runner_active", installer)
        self.assertIn("legacy_ai_runner_unit_present", installer)
        self.assertIn("etoro-sol-runner.service", installer)
        self.assertIn("etoro-minimax-runner.service", installer)

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
