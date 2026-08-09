from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from etoro_agent.agent_portfolio import (
    DELEGATED_DEMO_SCOPES,
    build_demo_agent_portfolio_request,
    parse_agent_portfolios,
)


class AgentPortfolioTests(unittest.TestCase):
    def test_provisioning_request_is_demo_only_ip_bounded_and_expiring(self) -> None:
        request = build_demo_agent_portfolio_request(
            investment_amount_usd=Decimal("1000"),
            portfolio_name="SolDemo1",
            token_name="sol-demo-executor",
            ipv4_whitelist=("203.0.113.8",),
            expires_at=datetime.now(timezone.utc) + timedelta(days=90),
        )
        self.assertEqual(tuple(request["scopeNames"]), DELEGATED_DEMO_SCOPES)
        self.assertEqual(request["investmentAmountInUsd"], 1000.0)
        self.assertEqual(request["ipsWhitelist"], ["203.0.113.8"])
        self.assertFalse(any("real" in value for value in request["scopeNames"]))

    def test_request_rejects_missing_ip_or_invalid_name(self) -> None:
        expiry = datetime.now(timezone.utc) + timedelta(days=1)
        with self.assertRaisesRegex(ValueError, "IPv4 whitelist"):
            build_demo_agent_portfolio_request(
                investment_amount_usd=Decimal("1000"),
                portfolio_name="SolDemo1",
                token_name="executor",
                ipv4_whitelist=(),
                expires_at=expiry,
            )
        with self.assertRaisesRegex(ValueError, "6-10"):
            build_demo_agent_portfolio_request(
                investment_amount_usd=Decimal("1000"),
                portfolio_name="too-long-name",
                token_name="executor",
                ipv4_whitelist=("203.0.113.8",),
                expires_at=expiry,
            )

    def test_portfolio_parser_never_requires_or_returns_token_secret(self) -> None:
        rows = parse_agent_portfolios(
            {
                "agentPortfolios": [
                    {
                        "agentPortfolioId": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
                        "agentPortfolioName": "SolDemo1",
                        "agentPortfolioVirtualBalance": 10000,
                        "userTokens": [
                            {
                                "userTokenId": "f9e8d7c6-b5a4-3210-fedc-ba9876543210",
                                "userTokenName": "executor",
                                "scopeNames": list(DELEGATED_DEMO_SCOPES),
                                "expiresAt": "2026-12-31T23:59:59Z",
                                "userToken": "must-not-be-projected",
                            }
                        ],
                    }
                ]
            }
        )
        self.assertEqual(rows[0].name, "SolDemo1")
        self.assertFalse(hasattr(rows[0].tokens[0], "user_token"))


if __name__ == "__main__":
    unittest.main()
