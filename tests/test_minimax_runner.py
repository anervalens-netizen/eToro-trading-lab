from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from etoro_agent.ai_review import (
    AIReviewStore,
    LLMUsage,
    MINIMAX_MODEL,
    build_trade_review_packet,
)
from etoro_agent.audit import AuditLog
from etoro_agent.minimax_runner import (
    OPENCODE,
    execute_wire_packet,
    parse_opencode_jsonl,
    review_closed_trade,
    run_opencode,
)


def trade() -> dict[str, object]:
    return {
        "trade_id": "trade-1",
        "strategy_id": "ema_adx",
        "strategy_version": "v1",
        "symbol": "AAPL",
        "side": "long",
        "opened_at": "2026-08-10T13:30:00+00:00",
        "closed_at": "2026-08-10T14:30:00+00:00",
        "entry_price": "100",
        "exit_price": "101",
        "net_pnl_usd": "0.90",
    }


def review() -> dict[str, object]:
    return {
        "verdict": "GOOD_PROCESS_GOOD_OUTCOME",
        "process_score": 80,
        "confidence": 0.8,
        "rule_adherence": "PASS",
        "reason_codes": ["rule_followed"],
        "findings": ["The supplied evidence is internally consistent."],
        "suggested_experiments": ["Stress test with wider costs."],
        "summary": "The process was sound on supplied evidence.",
    }


class OpenCodeJSONLTests(unittest.TestCase):
    def test_parser_extracts_strict_review_and_exact_available_usage(self) -> None:
        raw = "\n".join(
            [
                json.dumps({"type": "step_start", "part": {"type": "step-start"}}),
                json.dumps({"type": "text", "part": {"type": "text", "text": json.dumps(review())}}),
                json.dumps(
                    {
                        "type": "step_finish",
                        "part": {
                            "type": "step-finish",
                            "tokens": {
                                "input": 110,
                                "output": 22,
                                "reasoning": 7,
                                "cache": {"read": 50, "write": 3},
                            },
                            "cost": 0.012,
                        },
                    }
                ),
            ]
        )
        parsed, usage = parse_opencode_jsonl(raw)
        self.assertEqual(parsed["process_score"], 80)
        self.assertEqual(usage.input_tokens, 110)
        self.assertEqual(usage.output_tokens, 22)
        self.assertEqual(usage.reasoning_tokens, 7)
        self.assertEqual(usage.cache_read_tokens, 50)
        self.assertEqual(usage.cache_write_tokens, 3)
        self.assertEqual(usage.cost_usd, "0.012")

    def test_parser_rejects_tool_events_and_markdown_wrapped_json(self) -> None:
        tool = json.dumps({"type": "tool_call", "part": {"type": "tool", "name": "bash"}})
        with self.assertRaises(PermissionError):
            parse_opencode_jsonl(tool)
        markdown = json.dumps(
            {"type": "text", "part": {"type": "text", "text": f"```json\n{json.dumps(review())}\n```"}}
        )
        with self.assertRaises(ValueError):
            parse_opencode_jsonl(markdown)

    @patch("etoro_agent.minimax_runner.subprocess.run")
    def test_runner_uses_exact_model_json_format_and_no_shell(self, subprocess_run) -> None:
        subprocess_run.return_value.returncode = 0
        subprocess_run.return_value.stderr = ""
        subprocess_run.return_value.stdout = json.dumps(
            {"type": "text", "part": {"type": "text", "text": json.dumps(review())}}
        )
        run_opencode("safe prompt")
        args, kwargs = subprocess_run.call_args
        command = args[0]
        self.assertEqual(command[:3], ("sudo", "-n", "systemd-run"))
        self.assertIn("--property=InaccessiblePaths=-/opt/Mobiup/.ssh", command)
        self.assertIn(str(OPENCODE), command)
        self.assertIn(MINIMAX_MODEL, command)
        self.assertEqual(command[command.index("--format") + 1], "json")
        self.assertIn("Review the attached immutable trade packet and return only the required JSON.", command)
        self.assertIn("--file", command)
        self.assertNotIn("safe prompt", command)
        self.assertNotIn("--auto", command)
        self.assertNotIn("shell", kwargs)
        self.assertIn(
            '--setenv=OPENCODE_CONFIG_CONTENT={"permission":"deny"}', command
        )

    def test_wire_packet_is_hash_verified_before_model_invocation(self) -> None:
        packet = build_trade_review_packet(trade())
        wire = {
            "job_id": "job-1",
            "attempt": 1,
            "claim_token": "opaque-claim-token",
            "packet_id": packet.packet_id,
            "packet_hash": packet.packet_hash,
            "packet_json": packet.packet_json,
            "trade_id": packet.trade_id,
            "strategy_id": packet.strategy_id,
        }
        result = execute_wire_packet(wire, runner=lambda prompt: (review(), LLMUsage()))
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["provider"], "minimax-coding-plan")
        self.assertEqual(result["model"], MINIMAX_MODEL)
        self.assertEqual(result["claim_token"], "opaque-claim-token")
        tampered = dict(wire)
        tampered["trade_id"] = "trade-else"
        with self.assertRaises(ValueError):
            execute_wire_packet(tampered, runner=lambda prompt: (review(), LLMUsage()))


class ReviewExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.audit = AuditLog(Path(self.folder.name) / "audit.sqlite3")
        self.store = AIReviewStore(self.audit)

    def tearDown(self) -> None:
        self.audit.db.close()
        self.folder.cleanup()

    def test_success_is_persisted_once_and_deduplicated(self) -> None:
        calls = 0

        def fake_runner(prompt: str):
            nonlocal calls
            calls += 1
            self.assertNotIn("broker_url", prompt)
            return review(), LLMUsage(input_tokens=100, output_tokens=20)

        first = review_closed_trade(self.store, trade(), runner=fake_runner)
        second = review_closed_trade(self.store, trade(), runner=fake_runner)
        self.assertEqual(first.status, "COMPLETED")
        self.assertEqual(second.status, "DEDUPED")
        self.assertEqual(calls, 1)
        self.assertEqual(self.audit.db.execute("SELECT COUNT(*) FROM llm_runs").fetchone()[0], 1)
        self.assertEqual(
            self.audit.db.execute("SELECT COUNT(*) FROM trade_ai_reviews").fetchone()[0], 1
        )

    def test_model_failure_is_recorded_but_never_raised(self) -> None:
        def failing_runner(prompt: str):
            raise RuntimeError("quota exhausted")

        result = review_closed_trade(self.store, trade(), runner=failing_runner)
        self.assertEqual(result.status, "ERROR")
        row = self.audit.db.execute(
            "SELECT status,error_type,error_message FROM llm_runs"
        ).fetchone()
        self.assertEqual(row[0], "ERROR")
        self.assertEqual(row[1], "RuntimeError")
        self.assertNotIn("quota exhausted", canonical_events(self.audit))

    def test_daily_cap_is_hard_and_does_not_invoke_model(self) -> None:
        calls = 0

        def fake_runner(prompt: str):
            nonlocal calls
            calls += 1
            return review(), LLMUsage()

        first = review_closed_trade(self.store, trade(), daily_cap=1, runner=fake_runner)
        second_trade = trade()
        second_trade["trade_id"] = "trade-2"
        second = review_closed_trade(self.store, second_trade, daily_cap=1, runner=fake_runner)
        self.assertEqual(first.status, "COMPLETED")
        self.assertEqual(second.status, "CAP_REACHED")
        self.assertEqual(calls, 1)


def canonical_events(audit: AuditLog) -> str:
    return "\n".join(str(row[0]) for row in audit.db.execute("SELECT payload FROM events"))


if __name__ == "__main__":
    unittest.main()
