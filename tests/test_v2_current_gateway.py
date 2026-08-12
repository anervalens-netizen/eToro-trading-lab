from __future__ import annotations

import hashlib
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
    def test_broker_request_bytes_are_canonical_decimal_and_hashable(self) -> None:
        body = {
            "amount": Decimal("50.10"),
            "instrumentId": 1001,
            "stopLossRate": Decimal("95.1250"),
            "units": None,
        }
        encoded = EtoroPublicApiDemoClientV2.canonical_request_bytes(body)
        self.assertEqual(
            encoded,
            b'{"amount":50.10,"instrumentId":1001,"stopLossRate":95.1250,"units":null}',
        )
        self.assertEqual(len(hashlib.sha256(encoded).hexdigest()), 64)
        with self.assertRaisesRegex(TypeError, "binary floats"):
            EtoroPublicApiDemoClientV2.canonical_request_bytes({"amount": 50.1})

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
                        "costs": [
                            {"costType": "marketSpread", "amount": 0, "currency": "USD"},
                            {"costType": "transactionFee", "amount": 1, "currency": "USD"},
                        ],
                        "lastUpdated": (datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
                    },
                    "00000000-0000-0000-0000-000000000000",
                ),
                instrument_id=1001,
                symbol="AAPL",
            )
        with self.assertRaisesRegex(PermissionError, "amount"):
            client._validated_cost_breakdown(
                ApiResponse(
                    200,
                    {
                        "instrumentId": 1001,
                        "symbol": "AAPL",
                        "costs": [
                            {"costType": "marketSpread", "amount": None, "currency": "USD"},
                            {
                                "costType": "transactionFee",
                                "amount": None,
                                "currency": "USD",
                            },
                        ],
                        "lastUpdated": datetime.now(UTC).isoformat(),
                    },
                    "00000000-0000-0000-0000-000000000000",
                ),
                instrument_id=1001,
                symbol="AAPL",
            )

    def test_cost_breakdown_accepts_current_value_field_and_rejects_disagreement(self) -> None:
        client = EtoroPublicApiDemoClientV2()
        current = ApiResponse(
            200,
            {
                "instrumentId": 1001,
                "symbol": "AAPL",
                "costs": [
                    {"costType": "marketSpread", "value": 0.05, "currency": "USD"},
                    {"costType": "transactionFee", "value": 1, "currency": "USD"},
                    {"costType": "markup", "value": 0, "currency": "USD"},
                ],
                "lastUpdated": datetime.now(UTC).isoformat(),
            },
            "00000000-0000-0000-0000-000000000000",
        )
        total, snapshot_hash = client._validated_cost_breakdown(
            current,
            instrument_id=1001,
            symbol="AAPL",
        )
        self.assertEqual(total, Decimal("1.05"))
        self.assertEqual(len(snapshot_hash), 64)

        current.body["costs"][0]["amount"] = 0.06
        with self.assertRaisesRegex(PermissionError, "disagree"):
            client._validated_cost_breakdown(
                current,
                instrument_id=1001,
                symbol="AAPL",
            )
        current.body["costs"][0]["amount"] = None
        with self.assertRaisesRegex(PermissionError, "amount"):
            client._validated_cost_breakdown(
                current,
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

    def test_account_truth_is_one_strict_timed_snapshot(self) -> None:
        client = EtoroPublicApiDemoClientV2()
        requested = datetime.now(UTC) - timedelta(seconds=4)
        received = requested + timedelta(seconds=3)
        calls = 0

        def demo_pnl() -> ApiResponse:
            nonlocal calls
            calls += 1
            return ApiResponse(
                200,
                {
                    "clientPortfolio": {
                        "credit": "900",
                        "positions": [
                            {
                                "positionID": 10,
                                "mirrorID": 0,
                                "amount": "100",
                                "unrealizedPnL": {
                                    "exposureInAccountCurrency": "105",
                                    "pnL": "5",
                                },
                            }
                        ],
                        "ordersForOpen": [{"orderID": 20, "mirrorID": 0, "amount": "25"}],
                        "orders": [{"orderID": 21, "mirrorID": 0, "amount": "15"}],
                    }
                },
                "00000000-0000-0000-0000-000000000001",
                requested,
                received,
                requested + timedelta(seconds=1),
            )

        client.demo_pnl = demo_pnl  # type: ignore[method-assign]
        snapshot = client.account_snapshot()
        cash = snapshot.cash_truth()
        self.assertEqual(calls, 1)
        self.assertEqual(snapshot.equity_usd, Decimal("1005"))
        self.assertEqual(snapshot.gross_exposure_usd, Decimal("105"))
        self.assertEqual(cash.available_cash_usd, Decimal("860"))
        self.assertEqual(snapshot.observed_at, requested)
        self.assertEqual(cash.snapshot_hash, snapshot.snapshot_hash)

    def test_account_snapshot_rejects_malformed_overlap_and_flags_mirror(self) -> None:
        client = EtoroPublicApiDemoClientV2()
        now = datetime.now(UTC)

        def response(portfolio: object) -> ApiResponse:
            return ApiResponse(200, {"clientPortfolio": portfolio}, "request", now, now, now)

        client.demo_pnl = lambda: response(  # type: ignore[method-assign]
            {"credit": "1000", "positions": [None], "ordersForOpen": [], "orders": []}
        )
        with self.assertRaisesRegex(ValueError, "row"):
            client.account_snapshot()

        client.demo_pnl = lambda: response(  # type: ignore[method-assign]
            {
                "credit": "1000",
                "positions": [],
                "ordersForOpen": [{"orderID": 1, "amount": "10"}],
                "orders": [{"orderId": 1, "amount": "10"}],
            }
        )
        with self.assertRaisesRegex(ValueError, "overlap"):
            client.account_snapshot()

        client.demo_pnl = lambda: response(  # type: ignore[method-assign]
            {
                "credit": "1000",
                "positions": [],
                "ordersForOpen": [{"orderID": 2, "mirrorID": 9, "amount": "10"}],
                "orders": [],
            }
        )
        self.assertEqual(client.account_snapshot().foreign_activity, ("mirror_order:2",))

    def test_account_snapshot_uses_request_start_for_freshness(self) -> None:
        client = EtoroPublicApiDemoClientV2()
        requested = datetime.now(UTC) - timedelta(minutes=2)
        received = datetime.now(UTC)
        client.demo_pnl = lambda: ApiResponse(  # type: ignore[method-assign]
            200,
            {
                "clientPortfolio": {
                    "credit": "1000",
                    "positions": [],
                    "ordersForOpen": [],
                    "orders": [],
                }
            },
            "request",
            requested,
            received,
            received,
        )
        self.assertEqual(client.account_snapshot().observed_at, requested)

    def test_partial_close_requires_precision_and_rejects_dust(self) -> None:
        client = EtoroPublicApiDemoClientV2()
        position = {
            "positionID": 10,
            "instrumentID": 1001,
            "units": "1.000",
            "unitPrecision": 3,
            "minimumCloseUnits": "0.100",
            "minimumResidualUnits": "0.100",
        }
        client.demo_portfolio = lambda: ApiResponse(  # type: ignore[method-assign]
            200,
            {"clientPortfolio": {"positions": [position]}},
            "request",
        )
        prepared = client.prepare_close_position(position_id=10, units_to_deduct=Decimal("0.400"))
        self.assertEqual(prepared.body["UnitsToDeduct"], Decimal("0.400"))
        self.assertEqual(len(prepared.quantity_rules_hash), 64)
        with self.assertRaisesRegex(PermissionError, "quantized"):
            client.prepare_close_position(position_id=10, units_to_deduct=Decimal("0.4001"))
        with self.assertRaisesRegex(PermissionError, "dust"):
            client.prepare_close_position(position_id=10, units_to_deduct=Decimal("0.950"))
        position.pop("unitPrecision")
        with self.assertRaisesRegex(PermissionError, "precision/minimum"):
            client.prepare_close_position(position_id=10, units_to_deduct=Decimal("0.400"))


if __name__ == "__main__":
    unittest.main()
