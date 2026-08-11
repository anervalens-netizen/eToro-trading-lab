from __future__ import annotations

import unittest
from decimal import Decimal

from etoro_agent.etoro_api_current_v2 import (
    DEMO_CLOSE_PREFIX,
    DEMO_COSTS,
    DEMO_CREATE_ORDER,
    DEMO_ELIGIBILITY,
    ApiResponse,
    EtoroPublicApiDemoClientV2,
)


class CurrentGatewayV2Tests(unittest.TestCase):
    def test_current_routes_are_demo_only(self) -> None:
        self.assertEqual(DEMO_CREATE_ORDER, "/api/v2/trading/execution/demo/orders")
        self.assertEqual(DEMO_ELIGIBILITY, "/api/v2/trading/info/demo/eligibility")
        self.assertEqual(DEMO_COSTS, "/api/v2/trading/info/demo/costs")
        self.assertTrue(DEMO_CLOSE_PREFIX.startswith("/api/v1/trading/execution/demo/"))
        client = EtoroPublicApiDemoClientV2()
        with self.assertRaises(PermissionError):
            client._request("POST", "/api/v2/trading/execution/real/orders", body={})

    def test_open_compatibility_interface_uses_current_create_order(self) -> None:
        client = EtoroPublicApiDemoClientV2()
        calls: list[tuple[str, str, object]] = []

        def fake_request(method, path, **kwargs):
            calls.append((method, path, kwargs.get("body")))
            if path == DEMO_ELIGIBILITY:
                return ApiResponse(
                    200,
                    {
                        "eligibilities": [
                            {
                                "allowOpenPosition": True,
                                "minPositionExposure": 10,
                                "leverageConfigs": [
                                    {
                                        "direction": "long",
                                        "leverageValues": [1],
                                        "allowStopLossTakeProfit": True,
                                        "minPositionAmount": 10,
                                        "settlementType": "real",
                                        "minStopLossPercentage": 1,
                                        "maxStopLossPercentage": 50,
                                        "minTakeProfitPercentage": 1,
                                        "maxTakeProfitPercentage": 100,
                                    }
                                ],
                            }
                        ]
                    },
                    "00000000-0000-0000-0000-000000000000",
                )
            if path.endswith("/rates"):
                return ApiResponse(
                    200,
                    {"rates": [{"instrumentID": 1001, "bid": 99, "ask": 100}]},
                    "00000000-0000-0000-0000-000000000000",
                )
            return ApiResponse(
                200, {}, kwargs.get("request_id") or "00000000-0000-0000-0000-000000000000"
            )

        client._request = fake_request  # type: ignore[method-assign]
        client.open_by_amount(
            instrument_id=1001,
            amount_usd=Decimal("100"),
            is_buy=True,
            leverage=1,
            request_id="00000000-0000-0000-0000-000000000001",
            stop_loss_rate=Decimal("95"),
            take_profit_rate=Decimal("110"),
        )
        self.assertIn(DEMO_ELIGIBILITY, [path for _, path, _ in calls])
        self.assertIn(DEMO_COSTS, [path for _, path, _ in calls])
        self.assertEqual(calls[-1][1], DEMO_CREATE_ORDER)


if __name__ == "__main__":
    unittest.main()
