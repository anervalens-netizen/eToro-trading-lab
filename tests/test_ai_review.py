from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from etoro_agent.ai_review import (
    AIReviewStore,
    LLMRun,
    LLMUsage,
    MINIMAX_MODEL,
    build_trade_review_packet,
    canonical_json,
    sha256_text,
    validate_strategy_change_proposal,
    validate_trade_review,
)
from etoro_agent.audit import AuditLog


def closed_trade(trade_id: str = "trade-1") -> dict[str, object]:
    return {
        "trade_id": trade_id,
        "strategy_id": "ema_adx",
        "strategy_version": "v1",
        "portfolio_id": "strategy_05",
        "symbol": "AAPL",
        "side": "long",
        "opened_at": "2026-08-10T13:30:00+00:00",
        "closed_at": "2026-08-10T14:30:00+00:00",
        "units": "1",
        "notional_usd": "100",
        "entry_price": "100",
        "exit_price": "101",
        "gross_pnl_usd": "1",
        "fees_usd": "0.10",
        "financing_usd": "0",
        "net_pnl_usd": "0.90",
        "holding_seconds": 3600,
        "exit_reason": "strategy_exit",
        "rule_context": {"entry_rule": "ema_cross", "passed": True},
        "authorization": "must never leave the process",
        "broker_url": "https://example.invalid",
    }


def valid_review() -> dict[str, object]:
    return {
        "verdict": "GOOD_PROCESS_GOOD_OUTCOME",
        "process_score": 82,
        "confidence": 0.74,
        "rule_adherence": "PASS",
        "reason_codes": ["rule_followed"],
        "findings": ["Entry and exit matched the supplied rules."],
        "suggested_experiments": ["Stress test the same rule at higher spread."],
        "summary": "Good process on the supplied evidence.",
    }


class TradeReviewPacketTests(unittest.TestCase):
    def test_packet_is_allowlisted_sanitized_hash_bound_and_immutable(self) -> None:
        packet = build_trade_review_packet(closed_trade())
        payload = packet.payload
        self.assertNotIn("authorization", payload)
        self.assertNotIn("broker_url", payload)
        self.assertEqual(payload["review_contract"]["authority"], "RESEARCH_ONLY")
        self.assertEqual(packet.packet_hash, sha256_text(packet.packet_json))
        with self.assertRaises(TypeError):
            payload["trade_id"] = "changed"  # type: ignore[index]

    def test_packet_rejects_sensitive_nested_fields(self) -> None:
        trade = closed_trade()
        trade["rule_context"] = {"api_key": "secret"}
        with self.assertRaises(ValueError):
            build_trade_review_packet(trade)

    def test_packet_requires_a_closed_round_trip(self) -> None:
        trade = closed_trade()
        del trade["closed_at"]
        with self.assertRaises(ValueError):
            build_trade_review_packet(trade)
        trade = closed_trade()
        trade["closed_at"] = None
        with self.assertRaises(ValueError):
            build_trade_review_packet(trade)

    def test_review_schema_is_strict(self) -> None:
        validated = validate_trade_review(valid_review())
        self.assertEqual(validated["process_score"], 82)
        invalid = valid_review()
        invalid["unexpected"] = True
        with self.assertRaises(ValueError):
            validate_trade_review(invalid)


class AIReviewStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.audit = AuditLog(Path(self.folder.name) / "audit.sqlite3")
        self.store = AIReviewStore(self.audit)

    def tearDown(self) -> None:
        self.audit.db.close()
        self.folder.cleanup()

    def _record_run(self, run_id: str = "run-1") -> LLMRun:
        run = LLMRun(
            run_id=run_id,
            purpose="TRADE_REVIEW",
            provider="minimax-coding-plan",
            model=MINIMAX_MODEL,
            status="COMPLETED",
            input_hash="1" * 64,
            prompt_hash="2" * 64,
            output_hash="3" * 64,
            usage=LLMUsage(
                input_tokens=100,
                output_tokens=20,
                reasoning_tokens=5,
                cache_read_tokens=40,
                cache_write_tokens=2,
            ),
            latency_ms=1200,
            error_type=None,
            error_message=None,
            started_at="2026-08-10T12:00:00+00:00",
            completed_at="2026-08-10T12:00:01.200000+00:00",
        )
        self.store.record_run(run)
        return run

    def test_telemetry_and_review_are_append_only_and_deduplicated(self) -> None:
        run = self._record_run()
        row = self.audit.db.execute(
            "SELECT input_tokens,output_tokens,cache_read_tokens FROM llm_runs"
        ).fetchone()
        self.assertEqual(tuple(row), (100, 20, 40))
        with self.assertRaises(sqlite3.IntegrityError):
            self.audit.db.execute("UPDATE llm_runs SET status='ERROR' WHERE run_id='run-1'")
        self.audit.db.rollback()

        packet = build_trade_review_packet(closed_trade())
        prompt_hash = "4" * 64
        review_id = self.store.record_review(packet, valid_review(), run.run_id, prompt_hash)
        self.assertTrue(review_id.startswith("trade-review-"))
        self.assertTrue(self.store.has_review(packet.trade_id, MINIMAX_MODEL, prompt_hash))
        self.assertEqual(
            self.store.record_review(packet, valid_review(), run.run_id, prompt_hash),
            review_id,
        )

    def test_daily_aggregate_is_deterministic_and_research_only(self) -> None:
        run = self._record_run()
        packet = build_trade_review_packet(closed_trade())
        self.store.record_review(
            packet,
            valid_review(),
            run.run_id,
            "4" * 64,
            created_at=datetime(2026, 8, 11, 8, 31, tzinfo=timezone.utc),
        )
        first = self.store.daily_aggregate(date(2026, 8, 10), "ema_adx")
        second = self.store.daily_aggregate(date(2026, 8, 10), "ema_adx")
        self.assertEqual(first, second)
        self.assertEqual(first["review_count"], 1)
        self.assertEqual(first["net_pnl_usd"], "0.90")
        self.assertIn("NO_RISK_POLICY_MUTATION", first["constraints"])

        proposal = validate_strategy_change_proposal(
            first,
            {
                "strategy_id": "ema_adx",
                "objective": "Test a stricter entry filter.",
                "evidence": ["One review found a weak entry context."],
                "suggested_experiments": ["Backtest a higher ADX threshold."],
                "confidence": 0.6,
            },
        )
        self.assertEqual(proposal.state, "RESEARCH_ONLY")
        self.store.record_strategy_proposal(first, proposal)
        state = self.audit.db.execute(
            "SELECT state FROM strategy_change_proposals"
        ).fetchone()[0]
        self.assertEqual(state, "RESEARCH_ONLY")

    def test_daily_run_count_includes_errors_and_completions(self) -> None:
        self._record_run()
        self.assertEqual(
            self.store.runs_on_day(
                "minimax-coding-plan", MINIMAX_MODEL, "TRADE_REVIEW", date(2026, 8, 10)
            ),
            1,
        )

    def test_durable_review_job_is_leased_completed_and_idempotent(self) -> None:
        packet = build_trade_review_packet(closed_trade())
        prompt_hash = "4" * 64
        job_id, created = self.store.queue_review_job(packet, prompt_hash)
        self.assertTrue(created)
        claimed = self.store.claim_pending_reviews(
            worker_id="test-worker", daily_cap=2
        )
        self.assertEqual(len(claimed), 1)
        review = valid_review()
        output_hash = sha256_text(canonical_json(review))
        result = {
            "run_id": "wire-run-1",
            "purpose": "TRADE_REVIEW",
            "provider": "minimax-coding-plan",
            "model": MINIMAX_MODEL,
            "status": "COMPLETED",
            "job_id": job_id,
            "attempt": claimed[0]["attempt"],
            "claim_token": claimed[0]["claim_token"],
            "packet_id": packet.packet_id,
            "packet_hash": packet.packet_hash,
            "trade_id": packet.trade_id,
            "strategy_id": packet.strategy_id,
            "prompt_version": "trade-review-v1",
            "prompt_hash": prompt_hash,
            "output_hash": output_hash,
            "review": review,
            "usage": {
                "input_tokens": 100,
                "output_tokens": 20,
                "reasoning_tokens": None,
                "cache_read_tokens": 10,
                "cache_write_tokens": None,
                "cost_usd": None,
            },
            "latency_ms": 123,
            "error_type": None,
            "started_at": "2026-08-10T12:00:00+00:00",
            "completed_at": "2026-08-10T12:00:01+00:00",
        }
        first = self.store.submit_review_result(result)
        second = self.store.submit_review_result(result)
        self.assertEqual(first, second)
        state = self.audit.db.execute(
            "SELECT state FROM ai_review_jobs WHERE job_id=?", (job_id,)
        ).fetchone()[0]
        self.assertEqual(state, "COMPLETED")
        self.assertEqual(
            self.audit.db.execute("SELECT COUNT(*) FROM llm_runs").fetchone()[0], 1
        )
        self.assertEqual(
            self.audit.db.execute("SELECT COUNT(*) FROM trade_ai_reviews").fetchone()[0], 1
        )


if __name__ == "__main__":
    unittest.main()
