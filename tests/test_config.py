from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from etoro_agent.config import load_config


class ConfigTests(unittest.TestCase):
    def test_standing_authorization_is_enabled_only_in_demo_execution_config(self) -> None:
        paper = load_config("config/demo.json")
        demo = load_config("config/demo-execution.json")
        self.assertEqual(paper.demo_execution_authorization, "manual")
        self.assertEqual(demo.demo_execution_authorization, "standing_demo")
        self.assertEqual(demo.account_mode, "demo")
        self.assertTrue(demo.etoro_demo_execution_enabled)

    def test_standing_authorization_fails_closed_outside_enabled_demo(self) -> None:
        payload = json.loads(Path("config/demo.json").read_text(encoding="utf-8"))
        payload["demo_execution_authorization"] = "standing_demo"
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "invalid.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "only for enabled DEMO"):
                load_config(path)

    def test_candle_close_grace_is_bounded(self) -> None:
        payload = json.loads(Path("config/demo.json").read_text(encoding="utf-8"))
        payload["candle_close_grace_seconds"] = 301
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "invalid.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "candle close grace"):
                load_config(path)


if __name__ == "__main__":
    unittest.main()
