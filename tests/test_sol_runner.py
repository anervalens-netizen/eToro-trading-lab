from __future__ import annotations

import unittest

from etoro_agent.sol_runner import MODEL, _validate


class SolRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.packet = {
            "packet_id": "ai-1",
            "packet_hash": "0" * 64,
            "payload": {
                "mode": "ENTRY_REVIEW",
                "position": None,
                "candidates": [{"candidate_id": "candidate-1"}],
            },
        }

    def test_open_must_select_an_exact_candidate(self) -> None:
        decision = _validate(
            self.packet,
            {
                "action": "OPEN",
                "candidate_id": "candidate-1",
                "confidence": 0.8,
                "reason_codes": ["edge_present"],
                "rationale": "bounded candidate chosen",
            },
        )
        self.assertEqual(decision["model"], MODEL)
        with self.assertRaises(ValueError):
            _validate(
                self.packet,
                {
                    "action": "OPEN",
                    "candidate_id": "invented",
                    "confidence": 0.8,
                    "reason_codes": ["edge_present"],
                    "rationale": "invalid candidate",
                },
            )

    def test_close_requires_position_review(self) -> None:
        with self.assertRaises(ValueError):
            _validate(
                self.packet,
                {
                    "action": "CLOSE",
                    "candidate_id": "",
                    "confidence": 0.8,
                    "reason_codes": ["exit"],
                    "rationale": "invalid without position",
                },
            )


if __name__ == "__main__":
    unittest.main()
