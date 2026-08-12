from __future__ import annotations

import unittest
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from etoro_agent.etoro_api_current_v2 import ApiResponse
from etoro_agent.market_data_v2 import MarketDataCollector

NOW = datetime(2026, 8, 12, 12, 3, tzinfo=UTC)


class MarketClient:
    def __init__(self) -> None:
        self.rate_body: dict[str, object] = {
            "rates": [
                {
                    "instrumentID": 1001,
                    "instrumentId": 1001,
                    "bid": "99.5",
                    "ask": "100",
                    "date": NOW.isoformat(),
                    "timestamp": NOW.isoformat(),
                }
            ]
        }
        self.candle_body: dict[str, object] = {
            "candles": [
                {
                    "instrumentID": 1001,
                    "instrumentId": 1001,
                    "symbol": "AAPL",
                    "instrumentSymbol": "AAPL",
                    "candles": [
                        {
                            "from": (NOW - timedelta(minutes=2)).isoformat(),
                            "timestamp": (NOW - timedelta(minutes=2)).isoformat(),
                            "open": "99",
                            "high": "100",
                            "low": "98",
                            "close": "99.5",
                            "volume": "10",
                        },
                        {
                            "from": (NOW - timedelta(minutes=1)).isoformat(),
                            "timestamp": (NOW - timedelta(minutes=1)).isoformat(),
                            "open": "99.5",
                            "high": "101",
                            "low": "99",
                            "close": "100",
                            "volume": "12",
                        },
                    ],
                }
            ]
        }

    def rates(self, _instrument_ids: tuple[int, ...]) -> ApiResponse:
        return ApiResponse(200, self.rate_body, "rate-request")

    def history_candles(self, **_kwargs: object) -> ApiResponse:
        return ApiResponse(200, self.candle_body, "candle-request")


class MarketDataStrictContractTests(unittest.TestCase):
    @staticmethod
    def _collect(client: MarketClient):
        with patch("etoro_agent.market_data_v2.market_is_open", return_value=True):
            return MarketDataCollector(client).collect(
                "AAPL",
                1001,
                "OneMinute",
                2,
                now=NOW,
            )

    def test_valid_exact_aliases_produce_one_snapshot(self) -> None:
        snapshot = self._collect(MarketClient())
        self.assertEqual(snapshot.symbol, "AAPL")
        self.assertEqual(snapshot.instrument_id, 1001)
        self.assertEqual(len(snapshot.candles), 2)
        self.assertEqual(snapshot.quote_observed_at, NOW)

    def test_rate_identity_and_time_contract_failure_produces_no_snapshot(self) -> None:
        mutations = (
            {"instrumentID": 1002},
            {"instrumentId": True},
            {"timestamp": (NOW - timedelta(seconds=1)).isoformat()},
        )
        for changes in mutations:
            client = MarketClient()
            row = client.rate_body["rates"][0]  # type: ignore[index]
            assert isinstance(row, dict)
            row.update(changes)
            snapshots = []
            with self.subTest(changes=changes), self.assertRaises(PermissionError):
                snapshots.append(self._collect(client))
            self.assertEqual(snapshots, [])

    def test_candle_group_requires_one_exact_identity(self) -> None:
        mutations = (
            {"instrumentID": 1002},
            {"instrumentId": True},
            {"symbol": "TSLA"},
            {"instrumentSymbol": "TSLA"},
        )
        for changes in mutations:
            client = MarketClient()
            group = client.candle_body["candles"][0]  # type: ignore[index]
            assert isinstance(group, dict)
            group.update(changes)
            snapshots = []
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                snapshots.append(self._collect(client))
            self.assertEqual(snapshots, [])

        for groups in ([], [{}, {}], [None]):
            client = MarketClient()
            client.candle_body["candles"] = groups
            with (
                self.subTest(groups=groups),
                self.assertRaisesRegex(ValueError, "one candle group"),
            ):
                self._collect(client)

    def test_all_candle_timestamp_aliases_must_be_aware_and_identical(self) -> None:
        client = MarketClient()
        baseline = deepcopy(client.candle_body)
        mutations = (
            {"timestamp": (NOW - timedelta(minutes=2, seconds=1)).isoformat()},
            {"timestamp": datetime(2026, 8, 12, 12, 1).isoformat()},
            {"timestamp": True},
        )
        for changes in mutations:
            client.candle_body = deepcopy(baseline)
            row = client.candle_body["candles"][0]["candles"][0]  # type: ignore[index]
            assert isinstance(row, dict)
            row.update(changes)
            snapshots = []
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                snapshots.append(self._collect(client))
            self.assertEqual(snapshots, [])


if __name__ == "__main__":
    unittest.main()
