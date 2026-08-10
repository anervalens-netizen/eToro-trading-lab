from __future__ import annotations

import unittest
from unittest.mock import patch

from etoro_agent.sol_runner import CODEX_NATIVE, MODEL, _parse_codex_usage, _validate, review_strategy


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

    def test_direct_open_is_bounded_by_packet_and_not_candidate_list(self) -> None:
        packet = {
            **self.packet,
            "payload": {
                **self.packet["payload"],
                "allowed_symbols": ["AAPL", "TSLA"],
                "intent_constraints": {
                    "max_order_notional_usd": "500",
                    "min_stop_loss_fraction": "0.005",
                    "max_stop_loss_fraction": "0.10",
                },
            },
        }
        decision = _validate(
            packet,
            {
                "action": "OPEN",
                "candidate_id": "",
                "intent": {
                    "symbol": "AAPL",
                    "side": "buy",
                    "amount_usd": 250,
                    "stop_loss_fraction": 0.04,
                    "take_profit_fraction": 0.08,
                    "max_holding_seconds": 21600,
                },
                "confidence": 0.72,
                "reason_codes": ["direct_edge"],
                "rationale": "Direct bounded decision from supplied market features.",
            },
        )
        self.assertEqual(decision["intent"]["symbol"], "AAPL")
        with self.assertRaises(ValueError):
            _validate(
                packet,
                {
                    "action": "OPEN", "candidate_id": "", "intent": {
                        "symbol": "BTC", "side": "buy", "amount_usd": 250,
                        "stop_loss_fraction": 0.04, "take_profit_fraction": 0.08,
                        "max_holding_seconds": 21600,
                    },
                    "confidence": 0.72, "reason_codes": ["outside_catalog"],
                    "rationale": "Must fail.",
                },
            )

    def test_jsonl_usage_keeps_exact_available_token_fields(self) -> None:
        usage = _parse_codex_usage(
            '{"type":"thread.started","thread_id":"t"}\n'
            '{"type":"turn.completed","usage":{"input_tokens":100,'
            '"cached_input_tokens":40,"output_tokens":20,"reasoning_tokens":5}}\n'
        )
        self.assertEqual(usage["input_tokens"], 100)
        self.assertEqual(usage["cache_read_tokens"], 40)
        self.assertEqual(usage["output_tokens"], 20)
        self.assertEqual(usage["reasoning_tokens"], 5)
        self.assertIsNone(usage["cache_write_tokens"])

    def test_jsonl_usage_rejects_unstructured_output(self) -> None:
        with self.assertRaises(ValueError):
            _parse_codex_usage("not-json")

    @patch("etoro_agent.sol_runner._run")
    def test_daily_strategy_review_is_research_only_and_telemetered(self, mocked_run) -> None:
        def fake_run(command, **kwargs):
            output = command[command.index("--output-last-message") + 1]
            with open(output, "w", encoding="utf-8") as handle:
                handle.write(
                    '{"strategy_id":"ema_adx","objective":"Test stricter entries",'
                    '"evidence":["One weak entry"],'
                    '"suggested_experiments":["Backtest higher threshold"],'
                    '"confidence":0.6}'
                )
            return '{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":5}}\n'

        mocked_run.side_effect = fake_run
        aggregate = {
            "day": "2026-08-09",
            "strategy_id": "ema_adx",
            "review_count": 1,
            "net_pnl_usd": "-1.00",
            "aggregate_hash": "a" * 64,
            "authority": "RESEARCH_ONLY",
        }
        result = review_strategy(
            {
                "source_day": "2026-08-09",
                "strategy_id": "ema_adx",
                "aggregate_hash": "a" * 64,
                "aggregate": aggregate,
            }
        )
        self.assertEqual(result["llm_run"]["purpose"], "STRATEGY_REVIEW")
        self.assertEqual(result["llm_run"]["input_tokens"], 10)
        self.assertEqual(result["proposal"]["strategy_id"], "ema_adx")
        command = mocked_run.call_args.args[0]
        self.assertIn("--property=NoExecPaths=/", command)
        self.assertIn(f"--property=ExecPaths={CODEX_NATIVE}", command)
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", command)
        self.assertNotIn("/usr/bin/codex", command)
        self.assertTrue(any("BindReadOnlyPaths=/home/andrei/.codex/auth.json:" in item for item in command))


if __name__ == "__main__":
    unittest.main()
