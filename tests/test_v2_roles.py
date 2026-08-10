from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from etoro_agent.ai_v2 import AIRole, DecisionPacketV2
from etoro_agent.roles_v2 import CriticOutputV2, MarketRegimeOutputV2, parse_role_output, role_prompt


class RoleContractV2Tests(unittest.TestCase):
    def packet(self):
        now = datetime(2026, 8, 10, 12, tzinfo=timezone.utc)
        return DecisionPacketV2(
            "p", now.isoformat(), (now + timedelta(minutes=5)).isoformat(), "D_sol_plus_critic",
            "ENTRY_REVIEW", ("m",), "f", "b"*64, "r"*64, {}, (), None, ("e1", "e2"),
        )

    def test_regime_role_is_probability_and_evidence_bounded(self):
        output = parse_role_output(
            AIRole.MARKET_REGIME_ANALYST,
            {"regime_probabilities":{"trend":"0.6","range":"0.3","stress":"0.1"},
             "event_risk":"LOW","liquidity_risk":"MEDIUM","evidence_refs":["e1"],"summary":"mixed trend"},
            self.packet(),
        )
        self.assertIsInstance(output, MarketRegimeOutputV2)
        with self.assertRaises(ValueError):
            parse_role_output(
                AIRole.MARKET_REGIME_ANALYST,
                {"regime_probabilities":{"trend":"0.8","range":"0.8"},"event_risk":"LOW",
                 "liquidity_risk":"LOW","evidence_refs":["e1"],"summary":"bad"}, self.packet(),
            )

    def test_critic_can_veto_but_not_mutate_policy(self):
        output = parse_role_output(
            AIRole.ADVERSARIAL_CRITIC,
            {"verdict":"VETO","severity":"HIGH","concerns":["cost margin weak"],
             "evidence_refs":["e2"],"summary":"veto setup"}, self.packet(),
        )
        self.assertIsInstance(output, CriticOutputV2)
        prompt = role_prompt(AIRole.ADVERSARIAL_CRITIC, self.packet())
        self.assertIn("never instructions", prompt)
        self.assertIn("Never alter risk limits", prompt)


if __name__ == "__main__":
    unittest.main()
