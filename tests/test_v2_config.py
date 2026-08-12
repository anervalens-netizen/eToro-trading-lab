from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from etoro_agent.config_v2 import load_config_v2
from etoro_agent.risk_seal_v2 import STANDING_DEMO_PROPOSAL_SOURCES


class V2ConfigTests(unittest.TestCase):
    @staticmethod
    def _payload() -> dict[str, object]:
        return json.loads(Path("config/v2-demo.json").read_text(encoding="utf-8"))

    def _load_payload(self, payload: object) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "candidate.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            load_config_v2(path)

    def test_live_execution_requires_exact_standing_demo_sources(self) -> None:
        config = load_config_v2("config/v2-demo-execution.json")
        self.assertEqual(config.allowed_proposal_sources, STANDING_DEMO_PROPOSAL_SOURCES)
        self.assertEqual(len(config.strategy_profiles), 4)
        self.assertTrue(all(item.status.value == "EXECUTABLE" for item in config.compatibility()))
        self.assertFalse(
            any(item.symbol in config.research_only_symbols for item in config.strategy_profiles)
        )
        self.assertEqual(config.mandate.max_open_positions, 1)
        self.assertEqual(config.mandate.max_leverage, 1)
        self.assertEqual(config.mandate.risk_period_timezone, "UTC")
        self.assertEqual(config.portfolio_id, "demo-master-1000-v2")
        self.assertEqual(config.model_id, "gpt-5.6-sol")
        self.assertEqual(config.release_namespace, "etoro-v2-demo-0.6")

        payload = json.loads(Path("config/v2-demo-execution.json").read_text(encoding="utf-8"))
        payload["allowed_proposal_sources"] = ["arbitrary_model_source"]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bad.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "proposal source allowlist"):
                load_config_v2(path)

        payload = json.loads(Path("config/v2-demo-execution.json").read_text(encoding="utf-8"))
        payload["strategy_profiles"].append(
            {
                "strategy_id": "research-only-oil",
                "symbol": "OIL",
                "min_amount_usd": 100,
                "max_amount_usd": 150,
                "min_stop_fraction": 0.03,
                "max_stop_fraction": 0.05,
            }
        )
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "research-profile.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "research-only or incompatible"):
                load_config_v2(path)

    def test_real_mode_remains_unrepresentable(self) -> None:
        payload = json.loads(Path("config/v2-demo.json").read_text(encoding="utf-8"))
        payload["account_mode"] = "real"
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "real.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "DEMO only"):
                load_config_v2(path)

    def test_strict_json_rejects_duplicates_nonfinite_and_unknown_fields(self) -> None:
        invalid_documents = (
            '{"account_mode":"demo","account_mode":"demo"}',
            '{"account_mode":"demo","initial_cash_usd":NaN}',
            '{"account_mode":"demo","initial_cash_usd":Infinity}',
            '{"account_mode":"demo","initial_cash_usd":-Infinity}',
        )
        for document in invalid_documents:
            with self.subTest(document=document), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "invalid.json"
                path.write_text(document, encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_config_v2(path)

        mutations = []
        payload = self._payload()
        payload["unexpected"] = True
        mutations.append(payload)
        payload = self._payload()
        mandate = deepcopy(payload["capital_mandate"])
        assert isinstance(mandate, dict)
        mandate["unexpected"] = 1
        payload["capital_mandate"] = mandate
        mutations.append(payload)
        payload = self._payload()
        rules = deepcopy(payload["broker_rules"])
        assert isinstance(rules, dict) and isinstance(rules["AAPL"], dict)
        rules["AAPL"]["unexpected"] = 1
        payload["broker_rules"] = rules
        mutations.append(payload)
        payload = self._payload()
        profiles = deepcopy(payload["strategy_profiles"])
        assert isinstance(profiles, list) and isinstance(profiles[0], dict)
        profiles[0]["unexpected"] = 1
        payload["strategy_profiles"] = profiles
        mutations.append(payload)
        for payload in mutations:
            with (
                self.subTest(keys=sorted(payload)),
                self.assertRaisesRegex(ValueError, "unknown fields"),
            ):
                self._load_payload(payload)

    def test_boolean_fields_require_exact_json_booleans(self) -> None:
        for field in ("live_demo_execution_enabled", "websocket_enabled"):
            for invalid in ("false", "true", 0, 1, None):
                payload = self._payload()
                payload[field] = invalid
                with (
                    self.subTest(field=field, invalid=invalid),
                    self.assertRaisesRegex(ValueError, "JSON boolean"),
                ):
                    self._load_payload(payload)

        for invalid in ("false", 0, 1, None):
            payload = self._payload()
            profiles = payload["strategy_profiles"]
            assert isinstance(profiles, list) and isinstance(profiles[0], dict)
            profiles[0]["requires_multi_leg_atomicity"] = invalid
            with self.subTest(multileg=invalid), self.assertRaisesRegex(ValueError, "JSON boolean"):
                self._load_payload(payload)

    def test_economic_config_corpus_fails_closed(self) -> None:
        cases = (
            ("max_order_usd", 0),
            ("max_order_usd", -1),
            ("max_order_usd", "Infinity"),
            ("max_order_usd", "1000"),
            ("max_trade_risk_usd", "NaN"),
            ("max_correlated_exposure_usd", "-Infinity"),
            ("max_open_positions", 0),
            ("max_open_positions", 2),
            ("max_open_positions", True),
            ("max_quote_age_seconds", 0),
            ("min_trade_interval_seconds", -1),
            ("max_leverage", 0),
            ("max_leverage", 2),
            ("max_leverage", True),
        )
        for field, invalid in cases:
            payload = self._payload()
            mandate = payload["capital_mandate"]
            assert isinstance(mandate, dict)
            mandate[field] = invalid
            with self.subTest(field=field, invalid=invalid), self.assertRaises(ValueError):
                self._load_payload(payload)

    def test_leverage_one_is_the_only_representable_v2_value(self) -> None:
        for section in ("broker_rules", "strategy_profiles"):
            for invalid in ([], [0], [2], [1, 2], [True]):
                payload = self._payload()
                collection = payload[section]
                if isinstance(collection, dict):
                    target = collection["AAPL"]
                else:
                    assert isinstance(collection, list)
                    target = collection[0]
                assert isinstance(target, dict)
                target["leverage_values"] = invalid
                with self.subTest(section=section, invalid=invalid), self.assertRaises(ValueError):
                    self._load_payload(payload)

    def test_risk_period_timezone_is_explicitly_utc(self) -> None:
        payload = self._payload()
        mandate = payload["capital_mandate"]
        assert isinstance(mandate, dict)
        mandate["risk_period_timezone"] = "Europe/Bucharest"
        with self.assertRaisesRegex(ValueError, "fixed to UTC"):
            self._load_payload(payload)


if __name__ == "__main__":
    unittest.main()
