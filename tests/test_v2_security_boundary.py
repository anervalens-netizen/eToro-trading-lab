from __future__ import annotations

import inspect
import unittest
from pathlib import Path

from etoro_agent import (
    etoro_api_current_v2,
    etoro_api_v2,
    executor_v2,
    sol_runner_v2,
    ws_market_v2,
)


class V2SecurityBoundaryTests(unittest.TestCase):
    def test_v2_execution_source_contains_no_real_execution_route(self) -> None:
        forbidden = "/trading/execution/" + "real/"
        self.assertNotIn(forbidden, inspect.getsource(etoro_api_v2))
        self.assertNotIn(forbidden, inspect.getsource(etoro_api_current_v2))
        self.assertNotIn(forbidden, inspect.getsource(executor_v2))

    def test_executor_and_market_services_use_separate_user_keys(self) -> None:
        root = Path(__file__).resolve().parents[1] / "ops" / "systemd"
        executor = (root / "etoro-v2-executor-postgres.service").read_text(encoding="utf-8")
        market = (root / "etoro-v2-market.service").read_text(encoding="utf-8")
        self.assertIn("etoro-demo-write-user-key", executor)
        self.assertNotIn("etoro-demo-read-user-key", executor)
        self.assertIn("etoro-demo-read-user-key", market)
        self.assertNotIn("etoro-demo-write-user-key", market)
        self.assertIn("ENABLE_V2_DEMO_EXECUTION", executor)

    def test_dashboard_has_no_inet_socket_and_anchor_has_no_network(self) -> None:
        root = Path(__file__).resolve().parents[1] / "ops" / "systemd"
        dashboard = (root / "etoro-v2-dashboard.service").read_text(encoding="utf-8")
        anchor = (root / "etoro-v2-anchor.service").read_text(encoding="utf-8")
        role_apply = (root / "etoro-v2-role-apply.service").read_text(encoding="utf-8")
        self.assertIn("RestrictAddressFamilies=AF_UNIX\n", dashboard)
        self.assertIn("RestrictAddressFamilies=AF_UNIX\n", anchor)
        self.assertIn("RestrictAddressFamilies=AF_UNIX\n", role_apply)
        self.assertNotIn("AF_INET", dashboard)
        self.assertNotIn("AF_INET", anchor)
        self.assertNotIn("AF_INET", role_apply)

    def test_websocket_is_pinned_to_official_host(self) -> None:
        self.assertEqual(ws_market_v2.ETORO_WS_URL, "wss://ws.etoro.com/ws")

    def test_v2_sol_remote_execution_targets_are_fixed(self) -> None:
        self.assertEqual(sol_runner_v2.REMOTE_HOST, "andrei@server")
        self.assertEqual(
            str(sol_runner_v2.SSH_IDENTITY),
            "/opt/Mobiup/.ssh/id_ed25519_mobiup_primary_admin",
        )
        unit = (
            Path(__file__).resolve().parents[1] / "ops/systemd/etoro-v2-sol-runner.service"
        ).read_text(encoding="utf-8")
        self.assertNotIn("ETORO_V2_REMOTE_HOST", unit)
        self.assertNotIn("ETORO_V2_CODEX_NATIVE", unit)


if __name__ == "__main__":
    unittest.main()
