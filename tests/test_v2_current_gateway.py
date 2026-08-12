from __future__ import annotations

import hashlib
import os
import threading
import unittest
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from etoro_agent.decision_apply_service_v2 import _quote
from etoro_agent.etoro_api_current_v2 import (
    DEMO_CLOSE_PREFIX,
    DEMO_COSTS,
    DEMO_CREATE_ORDER,
    DEMO_ELIGIBILITY,
    ApiResponse,
    EtoroPublicApiDemoClientV2,
    decode_broker_rate_v2,
)


class CurrentGatewayV2Tests(unittest.TestCase):
    @staticmethod
    def _eligibility_body() -> dict[str, object]:
        return {
            "currency": "USD",
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
            ],
        }

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

    def test_authenticated_gateway_never_follows_redirects(self) -> None:
        target_hits: list[dict[str, str]] = []
        unexpected_hits: list[dict[str, str]] = []

        class TargetHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                target_hits.append(dict(self.headers))
                self.send_response(200)
                self.end_headers()

            def log_message(self, _format: str, *_args: object) -> None:
                return

        target = ThreadingHTTPServer(("127.0.0.1", 0), TargetHandler)
        target_thread = threading.Thread(target=target.serve_forever, daemon=True)
        target_thread.start()

        class RedirectHandler(BaseHTTPRequestHandler):
            location = f"http://127.0.0.1:{target.server_port}/capture"

            def do_GET(self) -> None:
                if self.path == "/unexpected":
                    unexpected_hits.append(dict(self.headers))
                    self.send_response(200)
                    self.end_headers()
                    return
                self.send_response(302)
                self.send_header("Location", type(self).location)
                self.end_headers()

            def log_message(self, _format: str, *_args: object) -> None:
                return

        redirect = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        redirect_thread = threading.Thread(target=redirect.serve_forever, daemon=True)
        redirect_thread.start()
        base_url = f"http://127.0.0.1:{redirect.server_port}"
        try:
            with (
                patch("etoro_agent.etoro_api_current_v2.BASE_URL", base_url),
                patch.dict(
                    os.environ,
                    {"ETORO_API_KEY": "test-api", "ETORO_USER_KEY": "test-user"},
                ),
            ):
                client = EtoroPublicApiDemoClientV2(base_url=base_url)
                self.assertEqual(client._request("GET", "/api/v1/me").status_code, 302)
                RedirectHandler.location = "/unexpected"
                self.assertEqual(client._request("GET", "/api/v1/me").status_code, 302)
            self.assertEqual(target_hits, [])
            self.assertEqual(unexpected_hits, [])
        finally:
            redirect.shutdown()
            target.shutdown()
            redirect.server_close()
            target.server_close()
            redirect_thread.join(timeout=2)
            target_thread.join(timeout=2)

    def test_eligibility_decoder_requires_exact_complete_finite_contract(self) -> None:
        client = EtoroPublicApiDemoClientV2()
        response = ApiResponse(200, self._eligibility_body(), "request")
        config, settlement = client._select_configuration(
            response,
            symbol="AAPL",
            amount_usd=Decimal("100"),
            is_buy=True,
            leverage=1,
        )
        self.assertEqual(config["direction"], "LONG")
        self.assertEqual(settlement, "real")

        mutations: list[dict[str, object]] = []
        for path, value in (
            (("currency",), None),
            (("eligibilities", 0, "symbol"), None),
            (("eligibilities", 0, "allowOpenPosition"), "false"),
            (("eligibilities", 0, "minPositionExposure"), "Infinity"),
            (("eligibilities", 0, "leverageConfigs", 0, "leverageValues"), [True]),
            (("eligibilities", 0, "leverageConfigs", 0, "allowStopLossTakeProfit"), 1),
            (("eligibilities", 0, "leverageConfigs", 0, "minPositionAmount"), 0),
            (("eligibilities", 0, "leverageConfigs", 0, "minStopLossPercentage"), False),
            (("eligibilities", 0, "leverageConfigs", 0, "minStopLossPercentage"), 51),
            (("eligibilities", 0, "leverageConfigs", 0, "maxTakeProfitPercentage"), "NaN"),
        ):
            payload = deepcopy(self._eligibility_body())
            target: object = payload
            for key in path[:-1]:
                target = target[key]  # type: ignore[index]
            if value is None:
                del target[path[-1]]  # type: ignore[index]
            else:
                target[path[-1]] = value  # type: ignore[index]
            mutations.append(payload)
        for payload in mutations:
            with self.subTest(payload=payload), self.assertRaises(PermissionError):
                client._select_configuration(
                    ApiResponse(200, payload, "request"),
                    symbol="AAPL",
                    amount_usd=Decimal("100"),
                    is_buy=True,
                    leverage=1,
                )

        for amount, is_buy, leverage in (
            (Decimal("Infinity"), True, 1),
            (Decimal("100"), 1, 1),
            (Decimal("100"), True, True),
        ):
            with (
                self.subTest(amount=amount, is_buy=is_buy, leverage=leverage),
                self.assertRaises(PermissionError),
            ):
                client._select_configuration(
                    response,
                    symbol="AAPL",
                    amount_usd=amount,
                    is_buy=is_buy,  # type: ignore[arg-type]
                    leverage=leverage,
                )

    def test_eligibility_rejects_duplicate_preferred_economic_configs(self) -> None:
        client = EtoroPublicApiDemoClientV2()
        body = self._eligibility_body()
        row = body["eligibilities"][0]  # type: ignore[index]
        first = row["leverageConfigs"][0]  # type: ignore[index]
        second = deepcopy(first)
        second["minPositionAmount"] = 20
        row["leverageConfigs"] = [first, second]  # type: ignore[index]
        for configs in ([first, second], [second, first]):
            row["leverageConfigs"] = configs  # type: ignore[index]
            with (
                self.subTest(configs=configs),
                self.assertRaisesRegex(PermissionError, "ambiguous"),
            ):
                client._select_configuration(
                    ApiResponse(200, body, "request"),
                    symbol="AAPL",
                    amount_usd=Decimal("100"),
                    is_buy=True,
                    leverage=1,
                )

    def test_rate_decoder_requires_exact_identity_economics_and_broker_time(self) -> None:
        now = datetime.now(UTC).replace(microsecond=0)
        valid_row = {
            "instrumentID": 1001,
            "instrumentId": 1001,
            "bid": "99.5",
            "ask": 100,
            "date": now.isoformat(),
            "timestamp": now.isoformat(),
            "sequence": 42,
            "eventId": "42",
        }
        decoded = decode_broker_rate_v2(
            ApiResponse(200, {"rates": [valid_row]}, "request"), instrument_id=1001
        )
        self.assertEqual(decoded.bid, Decimal("99.5"))
        self.assertEqual(decoded.observed_at, now)
        self.assertEqual(decoded.sequence_or_event_id, "42")

        mutations = (
            {"instrumentID": None},
            {"instrumentID": True},
            {"instrumentId": 1002},
            {"bid": False},
            {"ask": "Infinity"},
            {"date": None, "timestamp": None},
            {"date": datetime.now().isoformat(), "timestamp": datetime.now().isoformat()},
            {"timestamp": (now + timedelta(seconds=1)).isoformat()},
            {"eventId": "43"},
        )
        for changes in mutations:
            row = dict(valid_row)
            if changes == {"instrumentID": None}:
                row.pop("instrumentID")
                row.pop("instrumentId")
            elif changes == {"date": None, "timestamp": None}:
                row.pop("date")
                row.pop("timestamp")
            else:
                row.update(changes)
            with self.subTest(changes=changes), self.assertRaises(PermissionError):
                decode_broker_rate_v2(
                    ApiResponse(200, {"rates": [row]}, "request"), instrument_id=1001
                )

    def test_decision_quote_never_uses_local_time_as_broker_time(self) -> None:
        client = EtoroPublicApiDemoClientV2()
        client.rates = lambda _: ApiResponse(  # type: ignore[method-assign]
            200,
            {"rates": [{"instrumentID": 1001, "bid": 99, "ask": 100}]},
            "request",
        )
        with self.assertRaisesRegex(PermissionError, "provenance time"):
            _quote(
                client,
                symbol="AAPL",
                instrument_id=1001,
                broker_hash="a" * 64,
                received_at=datetime.now(UTC),
            )

    def test_open_compatibility_interface_uses_current_create_order(self) -> None:
        client = EtoroPublicApiDemoClientV2()
        calls: list[tuple[str, str, object]] = []

        def fake_request(method, path, **kwargs):
            calls.append((method, path, kwargs.get("body")))
            if path == DEMO_ELIGIBILITY:
                return ApiResponse(
                    200,
                    {
                        "currency": "USD",
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
                        ],
                    },
                    "00000000-0000-0000-0000-000000000000",
                )
            if path.endswith("/rates"):
                return ApiResponse(
                    200,
                    {
                        "rates": [
                            {
                                "instrumentID": 1001,
                                "bid": 99,
                                "ask": 100,
                                "date": datetime.now(UTC).isoformat(),
                            }
                        ]
                    },
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

    def test_cost_identity_and_amount_aliases_are_exact(self) -> None:
        client = EtoroPublicApiDemoClientV2()
        base = {
            "instrumentId": 1001,
            "instrumentID": 1001,
            "symbol": "AAPL",
            "costs": [
                {"costType": "marketSpread", "amount": 0, "currency": "USD"},
                {"costType": "transactionFee", "value": 1, "currency": "USD"},
            ],
            "lastUpdated": datetime.now(UTC).isoformat(),
        }
        client._validated_cost_breakdown(
            ApiResponse(200, base, "request"), instrument_id=1001, symbol="AAPL"
        )
        for changes in (
            {"instrumentId": True},
            {"instrumentID": 1002},
            {"symbol": None},
            {"symbol": "aapl"},
        ):
            body = deepcopy(base)
            body.update(changes)
            with self.subTest(changes=changes), self.assertRaises(PermissionError):
                client._validated_cost_breakdown(
                    ApiResponse(200, body, "request"), instrument_id=1001, symbol="AAPL"
                )
        body = deepcopy(base)
        body.pop("instrumentId")
        body.pop("instrumentID")
        with self.assertRaisesRegex(PermissionError, "missing"):
            client._validated_cost_breakdown(
                ApiResponse(200, body, "request"), instrument_id=1001, symbol="AAPL"
            )
        for invalid in (True, None, "Infinity"):
            body = deepcopy(base)
            costs = body["costs"]
            assert isinstance(costs, list) and isinstance(costs[0], dict)
            costs[0]["amount"] = invalid
            with self.subTest(amount=invalid), self.assertRaisesRegex(PermissionError, "amount"):
                client._validated_cost_breakdown(
                    ApiResponse(200, body, "request"), instrument_id=1001, symbol="AAPL"
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

    def test_scope_decoder_rejects_generic_real_segments_and_malformed_values(self) -> None:
        client = EtoroPublicApiDemoClientV2()
        base = [
            "etoro-public:trade.demo:read",
            "etoro-public:trade.demo:write",
        ]
        invalid_scope_sets: tuple[list[object], ...] = (
            [*base, "vendor:portfolio.real:read"],
            [*base, "vendor:future:unknown-real:read"],
            [*base, "vendor:REAL:inspect"],
            [*base, True],
            [*base, " malformed"],
            [*base, "malformed"],
            [*base, "etoro-public:trade..demo:read"],
            [*base, base[0]],
        )
        for scopes in invalid_scope_sets:
            client._request = (  # type: ignore[method-assign]
                lambda *args, scopes=scopes, **kwargs: ApiResponse(
                    200, {"scopes": scopes}, "request"
                )
            )
            with self.subTest(scopes=scopes), self.assertRaises(PermissionError):
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

    def test_account_snapshot_mirror_aliases_are_exact_non_boolean_integers(self) -> None:
        client = EtoroPublicApiDemoClientV2()
        now = datetime.now(UTC)
        for mirror_fields in (
            {"mirrorID": True},
            {"mirrorID": 9, "mirrorId": 10},
            {"mirrorID": "9"},
            {"mirrorId": -1},
        ):
            portfolio = {
                "credit": "1000",
                "positions": [],
                "ordersForOpen": [{"orderID": 2, "amount": "10", **mirror_fields}],
                "orders": [],
            }
            client.demo_pnl = lambda portfolio=portfolio: ApiResponse(  # type: ignore[method-assign]
                200, {"clientPortfolio": portfolio}, "request", now, now, now
            )
            with (
                self.subTest(mirror=mirror_fields),
                self.assertRaisesRegex(ValueError, "mirror identity"),
            ):
                client.account_snapshot()

        valid = {
            "credit": "1000",
            "positions": [],
            "ordersForOpen": [{"orderID": 2, "amount": "10", "mirrorID": 9, "mirrorId": 9}],
            "orders": [],
        }
        client.demo_pnl = lambda: ApiResponse(  # type: ignore[method-assign]
            200, {"clientPortfolio": valid}, "request", now, now, now
        )
        self.assertEqual(client.account_snapshot().foreign_activity, ("mirror_order:2",))

    def test_account_snapshot_pending_amount_aliases_are_exact_and_agree(self) -> None:
        client = EtoroPublicApiDemoClientV2()
        now = datetime.now(UTC)
        valid = {
            "credit": "1000",
            "positions": [],
            "ordersForOpen": [{"orderID": 2, "amount": "10.00", "exposure": 10}],
            "orders": [],
        }
        client.demo_pnl = lambda: ApiResponse(  # type: ignore[method-assign]
            200, {"clientPortfolio": valid}, "request", now, now, now
        )
        self.assertEqual(client.account_snapshot().pending_manual_orders_usd, Decimal("10"))
        for amount_fields in (
            {},
            {"amount": True},
            {"amount": "Infinity"},
            {"amount": "10", "exposure": "11"},
        ):
            portfolio = deepcopy(valid)
            portfolio["ordersForOpen"] = [{"orderID": 2, **amount_fields}]
            client.demo_pnl = lambda portfolio=portfolio: ApiResponse(  # type: ignore[method-assign]
                200, {"clientPortfolio": portfolio}, "request", now, now, now
            )
            with (
                self.subTest(amount=amount_fields),
                self.assertRaisesRegex(ValueError, "pending order amount"),
            ):
                client.account_snapshot()

    def test_account_snapshot_keeps_broker_and_client_order_identities_distinct(self) -> None:
        client = EtoroPublicApiDemoClientV2()
        now = datetime.now(UTC)
        valid = {
            "credit": "1000",
            "positions": [],
            "ordersForOpen": [
                {
                    "orderID": 701,
                    "orderId": 701,
                    "referenceID": "client-open-1",
                    "requestId": "client-open-1",
                    "amount": "10",
                }
            ],
            "orders": [
                {
                    "orderID": 702,
                    "referenceId": "client-pending-1",
                    "amount": "20",
                }
            ],
        }

        client.demo_pnl = lambda: ApiResponse(  # type: ignore[method-assign]
            200, {"clientPortfolio": valid}, "request", now, now, now
        )
        snapshot = client.account_snapshot()
        self.assertEqual(snapshot.pending_manual_orders_usd, Decimal("10"))
        self.assertEqual(snapshot.pending_orders_usd, Decimal("20"))
        self.assertEqual(snapshot.open_orders[0]["orderID"], 701)
        self.assertEqual(snapshot.open_orders[0]["referenceID"], "client-open-1")

        duplicate_reference = deepcopy(valid)
        duplicate_reference["ordersForOpen"].append(  # type: ignore[union-attr]
            {"orderID": 703, "referenceID": "client-open-1", "amount": "5"}
        )
        client.demo_pnl = lambda: ApiResponse(  # type: ignore[method-assign]
            200, {"clientPortfolio": duplicate_reference}, "request", now, now, now
        )
        with self.assertRaisesRegex(ValueError, "duplicated"):
            client.account_snapshot()

        overlapping_reference = deepcopy(valid)
        overlapping_reference["orders"][0]["referenceId"] = "client-open-1"  # type: ignore[index]
        client.demo_pnl = lambda: ApiResponse(  # type: ignore[method-assign]
            200, {"clientPortfolio": overlapping_reference}, "request", now, now, now
        )
        with self.assertRaisesRegex(ValueError, "overlap"):
            client.account_snapshot()

        for invalid_identity in (
            {"amount": "5"},
            {"orderID": 701, "orderId": 702, "amount": "5"},
            {"orderID": True, "amount": "5"},
            {"orderID": [701], "amount": "5"},
            {"orderID": -1, "amount": "5"},
            {"orderID": "bad identity", "amount": "5"},
            {
                "orderID": 701,
                "referenceID": "client-1",
                "requestId": "client-2",
                "amount": "5",
            },
        ):
            invalid = deepcopy(valid)
            invalid["ordersForOpen"] = [invalid_identity]
            invalid["orders"] = []
            client.demo_pnl = lambda invalid=invalid: ApiResponse(  # type: ignore[method-assign]
                200, {"clientPortfolio": invalid}, "request", now, now, now
            )
            with (
                self.subTest(identity=invalid_identity),
                self.assertRaisesRegex(ValueError, "identity"),
            ):
                client.account_snapshot()

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

    def test_close_preparation_rejects_identity_and_instrument_alias_coercion(self) -> None:
        client = EtoroPublicApiDemoClientV2()
        valid = {
            "positionID": 10,
            "positionId": 10,
            "instrumentID": 1001,
            "instrumentId": 1001,
            "units": "1",
        }
        submitted: list[str] = []

        def portfolio(row: dict[str, object]) -> ApiResponse:
            return ApiResponse(200, {"clientPortfolio": {"positions": [row]}}, "request")

        client.submit_prepared_close = (  # type: ignore[method-assign]
            lambda **kwargs: submitted.append("submitted")
        )
        for changes in (
            {"positionID": True, "positionId": True},
            {"positionId": 11},
            {"instrumentID": True, "instrumentId": True},
            {"instrumentId": 1002},
            {"units": True},
            {"units": "1", "quantity": "2"},
        ):
            row = {**valid, **changes}
            client.demo_portfolio = lambda row=row: portfolio(row)  # type: ignore[method-assign]
            with self.subTest(changes=changes), self.assertRaises((ValueError, PermissionError)):
                client.close_position(
                    position_id=10,
                    units_to_deduct=None,
                    request_id="00000000-0000-0000-0000-000000000001",
                )
            self.assertEqual(submitted, [])
        with self.assertRaisesRegex(ValueError, "close order arguments"):
            client.prepare_close_position(position_id=True, units_to_deduct=None)


if __name__ == "__main__":
    unittest.main()
