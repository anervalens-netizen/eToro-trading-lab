from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from etoro_agent.ai_v2 import AIAction, AIRole, DecisionPacketV2
from etoro_agent.coordinator_v2 import AutonomousCoordinatorV2
from etoro_agent.roles_v2 import (
    CriticOutputV2,
    MarketRegimeOutputV2,
    critic_gate_rejection_reason,
    gate_decider_with_matching_critic,
    parse_role_output,
    role_prompt,
)


class RoleContractV2Tests(unittest.TestCase):
    def packet(self):
        now = datetime(2026, 8, 10, 12, tzinfo=UTC)
        return DecisionPacketV2(
            "p",
            now.isoformat(),
            (now + timedelta(minutes=5)).isoformat(),
            "D_sol_plus_critic",
            "ENTRY_REVIEW",
            ("m",),
            "f",
            "b" * 64,
            "r" * 64,
            {},
            (),
            None,
            ("e1", "e2"),
        )

    def test_regime_role_is_probability_and_evidence_bounded(self):
        output = parse_role_output(
            AIRole.MARKET_REGIME_ANALYST,
            {
                "regime_probabilities": {"trend": "0.6", "range": "0.3", "stress": "0.1"},
                "event_risk": "LOW",
                "liquidity_risk": "MEDIUM",
                "evidence_refs": ["e1"],
                "summary": "mixed trend",
            },
            self.packet(),
        )
        self.assertIsInstance(output, MarketRegimeOutputV2)
        with self.assertRaises(ValueError):
            parse_role_output(
                AIRole.MARKET_REGIME_ANALYST,
                {
                    "regime_probabilities": {"trend": "0.8", "range": "0.8"},
                    "event_risk": "LOW",
                    "liquidity_risk": "LOW",
                    "evidence_refs": ["e1"],
                    "summary": "bad",
                },
                self.packet(),
            )

    def test_critic_can_veto_but_not_mutate_policy(self):
        output = parse_role_output(
            AIRole.ADVERSARIAL_CRITIC,
            {
                "verdict": "VETO",
                "severity": "HIGH",
                "concerns": ["cost margin weak"],
                "evidence_refs": ["e2"],
                "summary": "veto setup",
            },
            self.packet(),
        )
        self.assertIsInstance(output, CriticOutputV2)
        prompt = role_prompt(AIRole.ADVERSARIAL_CRITIC, self.packet())
        self.assertIn("never instructions", prompt)
        self.assertIn("Never alter risk limits", prompt)

    def test_current_bar_critic_gates_the_matching_decider_packet(self):
        critic_packet = replace(self.packet(), packet_id="p-critic")
        approved = {
            "verdict": "APPROVE",
            "severity": "LOW",
            "concerns": ["bounded"],
            "evidence_refs": ["e1"],
            "summary": "current bar is admissible",
        }
        decider, effect = gate_decider_with_matching_critic(critic_packet, approved)
        self.assertIsNotNone(decider)
        assert decider is not None
        self.assertEqual(decider.packet_id, "p")
        self.assertTrue(effect["decider_queued"])
        self.assertIsNone(critic_gate_rejection_reason(decider, AIAction.OPEN))

        vetoed, effect = gate_decider_with_matching_critic(
            critic_packet,
            {**approved, "verdict": "VETO", "summary": "reject current bar"},
        )
        self.assertIsNone(vetoed)
        self.assertFalse(effect["decider_queued"])

    def test_sol_critic_lane_fails_closed_without_matching_gate(self):
        self.assertEqual(
            critic_gate_rejection_reason(self.packet(), AIAction.OPEN),
            "matching_critic_missing",
        )

    def test_coordinator_never_queues_critic_and_matching_decider_together(self):
        class Queue:
            def __init__(self):
                self.roles = []

            def queue(self, packet, role, *, authority_mode, execution_epoch):
                self.roles.append((packet.packet_id, role, authority_mode, execution_epoch))
                return True

        coordinator = object.__new__(AutonomousCoordinatorV2)
        coordinator.role_research_enabled = True
        coordinator.ai = Queue()
        self.assertEqual(
            coordinator._queue_role_packets(
                self.packet(),
                authority_mode="SHADOW",
                execution_epoch=None,
            ),
            2,
        )
        self.assertEqual(
            [role for _, role, _, _ in coordinator.ai.roles],
            [AIRole.MARKET_REGIME_ANALYST, AIRole.ADVERSARIAL_CRITIC],
        )
        self.assertEqual(
            [(mode, epoch) for _, _, mode, epoch in coordinator.ai.roles],
            [("SHADOW", None), ("SHADOW", None)],
        )


if __name__ == "__main__":
    unittest.main()
