from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from etoro_agent.mcp import EtoroMCPClient, MCPResult


class MCPAuthenticationTests(unittest.TestCase):
    def test_systemd_credential_files_are_supported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            user_key = Path(directory) / "user"
            api_key = Path(directory) / "api"
            user_key.write_text("user-value\n", encoding="utf-8")
            api_key.write_text("api-value\n", encoding="utf-8")
            environment = {
                "ETORO_USER_KEY_FILE": str(user_key),
                "ETORO_API_KEY_FILE": str(api_key),
            }
            with patch.dict(os.environ, environment, clear=True):
                headers = EtoroMCPClient()._headers()
        self.assertEqual(headers["x-user-key"], "user-value")
        self.assertEqual(headers["x-api-key"], "api-value")
        self.assertEqual(headers["User-Agent"], "etoro-demo-agent/0.1 MCP-Client")

    def test_file_and_direct_secret_fail_closed(self) -> None:
        with tempfile.NamedTemporaryFile() as credential:
            credential.write(b"value")
            credential.flush()
            with patch.dict(
                os.environ,
                {
                    "ETORO_USER_KEY": "direct",
                    "ETORO_USER_KEY_FILE": credential.name,
                },
                clear=True,
            ):
                with self.assertRaisesRegex(RuntimeError, "must not be mixed"):
                    EtoroMCPClient()._headers()

    def test_real_trade_route_is_never_allowlisted(self) -> None:
        client = EtoroMCPClient()
        with self.assertRaises(PermissionError):
            client.execute_demo_order(
                "/api/v2/trading/execution/real/orders", "{}", "request-id"
            )

    def test_isolated_executor_rejects_any_real_scope(self) -> None:
        client = EtoroMCPClient()
        with patch.object(
            client,
            "execute_read",
            return_value=MCPResult(
                200,
                True,
                {
                    "scopes": [
                        "etoro-public:trade.demo:read",
                        "etoro-public:trade.demo:write",
                        "etoro-public:trade.real:read",
                    ]
                },
                "request",
                {},
            ),
        ):
            with self.assertRaisesRegex(PermissionError, "REAL scope"):
                client.verify_isolated_demo_execution_scope()

    def test_isolated_executor_requires_demo_read_and_write(self) -> None:
        client = EtoroMCPClient()
        with patch.object(
            client,
            "execute_read",
            return_value=MCPResult(
                200,
                True,
                {"scopes": ["etoro-public:trade.demo:write"]},
                "request",
                {},
            ),
        ):
            with self.assertRaisesRegex(PermissionError, "DEMO trade read and write"):
                client.verify_isolated_demo_execution_scope()

    def test_isolated_executor_accepts_extra_read_only_scope(self) -> None:
        client = EtoroMCPClient()
        with patch.object(
            client,
            "execute_read",
            return_value=MCPResult(
                200,
                True,
                {
                    "scopes": [
                        "etoro-public:trade.demo:read",
                        "etoro-public:trade.demo:write",
                        "etoro-public:markets:read",
                    ]
                },
                "request",
                {},
            ),
        ):
            identity = client.verify_isolated_demo_execution_scope()
        self.assertIn("etoro-public:markets:read", identity["scopes"])

    def test_isolated_executor_rejects_another_write_scope(self) -> None:
        client = EtoroMCPClient()
        with patch.object(
            client,
            "execute_read",
            return_value=MCPResult(
                200,
                True,
                {
                    "scopes": [
                        "etoro-public:trade.demo:read",
                        "etoro-public:trade.demo:write",
                        "etoro-public:agent-portfolios:write",
                    ]
                },
                "request",
                {},
            ),
        ):
            with self.assertRaisesRegex(PermissionError, "another write scope"):
                client.verify_isolated_demo_execution_scope()


if __name__ == "__main__":
    unittest.main()
