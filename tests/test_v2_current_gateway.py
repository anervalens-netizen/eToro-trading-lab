from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
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
                                "symbol": "AAPL",
                                "allowOpenPosition": True,
                                "minPositionExposure": 10,
                                "leverageConfigs": [
                                    {
                                        "direction": "LONG",
                                        "leverageValues": [1],
                                        "allowStopLossTakeProfit": True,
                                        "minPositionAmount": 10,
                                        "settlementType": "REAL",
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
            if path == DEMO_COSTS:
                return ApiResponse(
                    200,
                    {
                        "instrumentId": 1001,
                        "symbol": "AAPL",
                        "costs": [
                            {"costType": "marketSpread", "amount": 0.03, "currency": "USD"},
                            {
                                "costType": "transactionFee",
                                "amount": 0,
                                "currency": "USD",
                            },
                        ],
                        "lastUpdated": datetime.now(UTC).isoformat(),
                    },
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
        self.assertEqual(calls[-1][2]["settlementType"], "real")
        self.assertEqual(sum(path.endswith("/rates") for _, path, _ in calls), 1)

    def test_stop_take_direction_and_cost_shape_fail_closed(self) -> None:
        config = {
            "minStopLossPercentage": 1,
            "maxStopLossPercentage": 50,
            "minTakeProfitPercentage": 1,
            "maxTakeProfitPercentage": 100,
        }
        client = EtoroPublicApiDemoClientV2()
        client._validate_stop_take(
            config,
            Decimal("100"),
            Decimal("98"),
            Decimal("104"),
            is_buy=True,
        )
        client._validate_stop_take(
            config,
            Decimal("100"),
            Decimal("102"),
            Decimal("96"),
            is_buy=False,
        )
        with self.assertRaisesRegex(PermissionError, "long order"):
            client._validate_stop_take(
                config,
                Decimal("100"),
                Decimal("101"),
                Decimal("104"),
                is_buy=True,
            )
        with self.assertRaisesRegex(PermissionError, "currency"):
            client._validated_cost_breakdown(
                ApiResponse(
                    200,
                    {
                        "instrumentId": 1001,
                        "symbol": "AAPL",
                        "costs": [{"costType": "transactionFee", "amount": 1, "currency": "EUR"}],
                        "lastUpdated": datetime.now(UTC).isoformat(),
                    },
                    "00000000-0000-0000-0000-000000000000",
                ),
                instrument_id=1001,
                symbol="AAPL",
            )
        with self.assertRaisesRegex(PermissionError, "stale"):
            client._validated_cost_breakdown(
                ApiResponse(
                    200,
                    {
                        "instrumentId": 1001,
                        "symbol": "AAPL",
                        "costs": [{"costType": "transactionFee", "amount": 1, "currency": "USD"}],
                        "lastUpdated": (datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
                    },
                    "00000000-0000-0000-0000-000000000000",
                ),
                instrument_id=1001,
                symbol="AAPL",
            )

    def test_executor_identity_rejects_any_real_scope(self) -> None:
        client = EtoroPublicApiDemoClientV2()
        client._request = lambda *args, **kwargs: ApiResponse(  # type: ignore[method-assign]
            200,
            {
                "scopes": [
                    "etoro-public:trade.demo:read",
                    "etoro-public:trade.demo:write",
                    "etoro-public:trade.real:read",
                ]
            },
            "00000000-0000-0000-0000-000000000000",
        )
        with self.assertRaisesRegex(PermissionError, "REAL scope"):
            client.verify_isolated_demo_execution_scope()

    def test_read_identity_rejects_write_scope(self) -> None:
        client = EtoroPublicApiDemoClientV2()
        client._request = lambda *args, **kwargs: ApiResponse(  # type: ignore[method-assign]
            200,
            {
                "scopes": [
                    "etoro-public:trade.demo:read",
                    "etoro-public:trade.demo:write",
                ]
            },
            "00000000-0000-0000-0000-000000000000",
        )
        with self.assertRaisesRegex(PermissionError, "write or REAL"):
            client.verify_isolated_demo_read_scope()

    def test_executor_identity_requires_demo_read_and_write(self) -> None:
        client = EtoroPublicApiDemoClientV2()
        client._request = lambda *args, **kwargs: ApiResponse(  # type: ignore[method-assign]
            200,
            {"scopes": ["etoro-public:trade.demo:read"]},
            "00000000-0000-0000-0000-000000000000",
        )
        with self.assertRaisesRegex(PermissionError, "read and write"):
            client.verify_isolated_demo_execution_scope()


if __name__ == "__main__":
    unittest.main()
