from __future__ import annotations

import unittest
from pathlib import Path


STATIC = Path(__file__).resolve().parents[1] / "src" / "etoro_agent" / "dashboard_static"


class DashboardUiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (STATIC / "index.html").read_text(encoding="utf-8")
        cls.css = (STATIC / "dashboard.css").read_text(encoding="utf-8")
        cls.javascript = (STATIC / "dashboard.js").read_text(encoding="utf-8")

    def test_light_zero_build_workspace_has_all_primary_views(self) -> None:
        self.assertIn('name="color-scheme" content="light"', self.html)
        for view in ("overview", "strategies", "trades", "reviews", "risk", "usage"):
            self.assertIn(f'data-view="{view}"', self.html)
        self.assertNotIn("cdn.", self.html.lower())
        self.assertNotIn("http://", self.html.lower())
        self.assertNotIn("https://", self.html.lower())

    def test_strategy_and_trade_details_use_read_only_api_contracts(self) -> None:
        self.assertIn("#strategies/${encodeURIComponent(strategy.id)}", self.javascript)
        self.assertIn("/api/strategies/${encodeURIComponent(strategyId)}", self.javascript)
        self.assertIn("/api/strategies/${encodeURIComponent(strategyId)}/trades", self.javascript)
        self.assertIn("/api/trades/${encodeURIComponent(trade.trade_id)}", self.javascript)

    def test_controls_preserve_explicit_confirmation_phrases(self) -> None:
        self.assertIn("APPROVE ${approval.proposal_id} ${suffix}", self.javascript)
        self.assertIn('confirmation !== "RESUME_DEMO"', self.javascript)
        self.assertIn('postJson("/api/control/kill"', self.javascript)
        self.assertIn('postJson("/api/control/resume"', self.javascript)
        self.assertNotIn("/api/v2/trading/execution", self.javascript)

    def test_mobile_navigation_and_reduced_motion_are_supported(self) -> None:
        self.assertIn("@media (max-width: 760px)", self.css)
        self.assertIn("prefers-reduced-motion", self.css)
        self.assertIn('aria-label="Toggle navigation"', self.html)


if __name__ == "__main__":
    unittest.main()
