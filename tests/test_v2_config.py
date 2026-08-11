from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from etoro_agent.config_v2 import load_config_v2
from etoro_agent.risk_seal_v2 import STANDING_DEMO_PROPOSAL_SOURCES


class V2ConfigTests(unittest.TestCase):
    def test_live_execution_requires_exact_standing_demo_sources(self) -> None:
        config = load_config_v2("config/v2-demo-execution.json")
        self.assertEqual(config.allowed_proposal_sources, STANDING_DEMO_PROPOSAL_SOURCES)

        payload = json.loads(Path("config/v2-demo-execution.json").read_text(encoding="utf-8"))
        payload["allowed_proposal_sources"] = ["arbitrary_model_source"]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bad.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "proposal source allowlist"):
                load_config_v2(path)

    def test_real_mode_remains_unrepresentable(self) -> None:
        payload = json.loads(Path("config/v2-demo.json").read_text(encoding="utf-8"))
        payload["account_mode"] = "real"
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "real.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "DEMO only"):
                load_config_v2(path)


if __name__ == "__main__":
    unittest.main()
