from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from etoro_agent.audit import AuditLog
from etoro_agent.config import load_config
from etoro_agent.data_quality import (
    DataQualityIssue,
    DataQualityReport,
    MarketDataQualityError,
)
from etoro_agent.engine import AutonomousShadowEngine, MarketCollectionFailure
from etoro_agent.market import CandleSnapshot, INSTRUMENTS_BY_SYMBOL, MarketSnapshot
from etoro_agent.mcp import MCPResult
from etoro_agent.models import (
    CloseIntent,
    ExecutionState,
    KillState,
    RiskContext,
    Side,
    TradeIntent,
)
from etoro_agent.risk import generate_private_signing_key
from etoro_agent.strategy_catalog import STRATEGY_COUNT


def series(start: Decimal, count: int = 250) -> tuple[Decimal, ...]:
    return tuple(start + Decimal(index) / Decimal("100") for index in range(count))


class ShadowEngineTests(unittest.TestCase):
    def test_repeated_market_quality_failure_is_detailed_rate_limited_and_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            audit = AuditLog(Path(folder) / "audit.sqlite3")
            engine = AutonomousShadowEngine(load_config("config/demo.json"), audit)
            now = datetime.now(timezone.utc)
            report = DataQualityReport(
                checked_at=now,
                interval="FifteenMinutes",
                candle_count=250,
                expected_interval_seconds=900,
                freshness_seconds=1800,
                issues=(DataQualityIssue("stale_series", "redacted"),),
            )
            failure = MarketCollectionFailure(
                "SPX500", MarketDataQualityError("redacted", report)
            )

            engine._record_engine_failure(failure)
            engine._record_engine_failure(failure)

            rows = audit.db.execute(
                "SELECT payload FROM events WHERE event_type='shadow_engine_error'"
            ).fetchall()
            self.assertEqual(len(rows), 1)
            payload = json.loads(rows[0][0])
            self.assertEqual(payload["symbol"], "SPX500")
            self.assertEqual(payload["issue_codes"], ["stale_series"])
            heartbeat = audit.db.execute(
                "SELECT details FROM service_heartbeats WHERE service='shadow-engine'"
            ).fetchone()
            self.assertEqual(json.loads(heartbeat[0])["repeat_count"], 1)

            engine._record_engine_recovery()
            self.assertEqual(
                audit.db.execute(
                    "SELECT COUNT(*) FROM events WHERE event_type='shadow_engine_recovered'"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                audit.state_get("shadow_engine_error_signature", "missing"), ""
            )

    def test_new_research_epoch_resets_all_strategy_fingerprints(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            audit = AuditLog(Path(folder) / "audit.sqlite3")
            audit.state_set("research_epoch", "old-policy")
            for index in range(1, STRATEGY_COUNT + 1):
                audit.state_set(
                    f"shadow_last_evaluated_bar:strategy_{index:02d}", "old-bar"
                )
            AutonomousShadowEngine(load_config("config/demo.json"), audit)
            self.assertEqual(
                audit.state_get("research_epoch", ""),
                "commodity-risk-grid-v5-20260810",
            )
            self.assertTrue(
                all(
                    audit.state_get(
                        f"shadow_last_evaluated_bar:strategy_{index:02d}", "missing"
                    )
                    == ""
                    for index in range(1, STRATEGY_COUNT + 1)
                )
            )

    def test_full_strategy_tick_is_offline_isolated_and_audited(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            audit = AuditLog(Path(folder) / "audit.sqlite3")
            audit.set_kill_state(KillState.ACTIVE, "test", "shadow ready")
            config = load_config("config/demo.json")
            engine = AutonomousShadowEngine(config, audit)
            now = datetime.now(timezone.utc)
            snapshots = {
                symbol: MarketSnapshot(
                    symbol,
                    instrument.instrument_id,
                    Decimal("99.9"),
                    Decimal("100"),
                    series(Decimal("90") + Decimal(index)),
                    captured_at=now,
                    interval="FifteenMinutes",
                )
                for index, (symbol, instrument) in enumerate(INSTRUMENTS_BY_SYMBOL.items())
            }
            result = engine.tick(snapshots)
            self.assertEqual(len(result.strategy_results), STRATEGY_COUNT)
            self.assertEqual(
                len({row["portfolio_id"] for row in result.strategy_results}), STRATEGY_COUNT
            )
            self.assertEqual(
                audit.db.execute("SELECT COUNT(*) FROM approvals").fetchone()[0], 0
            )
            self.assertEqual(
                audit.db.execute(
                    "SELECT COUNT(*) FROM events WHERE event_type='strategy_snapshot'"
                ).fetchone()[0],
                STRATEGY_COUNT,
            )
            self.assertTrue(audit.verify_chain())

    def test_locked_kill_prevents_new_shadow_opens(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            audit = AuditLog(Path(folder) / "audit.sqlite3")
            config = load_config("config/demo.json")
            engine = AutonomousShadowEngine(config, audit)
            now = datetime.now(timezone.utc)
            snapshots = {
                symbol: MarketSnapshot(
                    symbol,
                    instrument.instrument_id,
                    Decimal("99.9"),
                    Decimal("100"),
                    series(Decimal("90") + Decimal(index)),
                    captured_at=now,
                    interval="FifteenMinutes",
                )
                for index, (symbol, instrument) in enumerate(INSTRUMENTS_BY_SYMBOL.items())
            }
            result = engine.tick(snapshots)
            self.assertFalse(any(row["status"] == "shadow_filled" for row in result.strategy_results))
            self.assertEqual(
                audit.db.execute("SELECT COUNT(*) FROM shadow_fills").fetchone()[0], 0
            )

    def test_filesystem_kill_prevents_new_shadow_opens(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            runtime = Path(folder)
            audit = AuditLog(runtime / "audit.sqlite3")
            audit.set_kill_state(KillState.ACTIVE, "test", "ready")
            (runtime / "KILL_SWITCH").touch()
            engine = AutonomousShadowEngine(load_config("config/demo.json"), audit)
            now = datetime.now(timezone.utc)
            snapshots = {
                symbol: MarketSnapshot(
                    symbol,
                    instrument.instrument_id,
                    Decimal("99.9"),
                    Decimal("100"),
                    series(Decimal("90") + Decimal(index)),
                    captured_at=now,
                    interval="FifteenMinutes",
                )
                for index, (symbol, instrument) in enumerate(
                    INSTRUMENTS_BY_SYMBOL.items()
                )
            }
            result = engine.tick(snapshots)
            self.assertFalse(
                any(row["status"] == "shadow_filled" for row in result.strategy_results)
            )
            self.assertEqual(
                audit.db.execute("SELECT COUNT(*) FROM shadow_fills").fetchone()[0], 0
            )

    def test_sol_open_decision_controls_single_master_and_fills_next_quote(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            audit = AuditLog(Path(folder) / "audit.sqlite3")
            audit.set_kill_state(KillState.ACTIVE, "test", "ready")
            engine = AutonomousShadowEngine(load_config("config/demo.json"), audit)
            now = datetime.now(timezone.utc)

            def snapshots(at: datetime, offset: Decimal) -> dict[str, MarketSnapshot]:
                return {
                    symbol: MarketSnapshot(
                        symbol,
                        instrument.instrument_id,
                        Decimal("99.9") + offset,
                        Decimal("100") + offset,
                        series(Decimal("90") + Decimal(index) + offset),
                        captured_at=at,
                        interval="FifteenMinutes",
                    )
                    for index, (symbol, instrument) in enumerate(
                        INSTRUMENTS_BY_SYMBOL.items()
                    )
                }

            engine.tick(snapshots(now, Decimal("0")))
            pending = engine.ai.pending()
            self.assertEqual(len(pending), 1)
            candidate = pending[0]["payload"]["candidates"][0]
            engine.ai.decide(
                pending[0]["packet_id"],
                pending[0]["packet_hash"],
                "OPEN",
                candidate["candidate_id"],
                Decimal("0.8"),
                ("trend_confirmed",),
                "bounded candidate selected",
                "gpt-5.6-sol",
            )
            self.assertIsNone(engine._position("master_1000"))
            engine.tick(snapshots(now + timedelta(minutes=15), Decimal("0.1")))
            self.assertIsNotNone(engine._position("master_1000"))
            self.assertEqual(
                audit.db.execute(
                    "SELECT COUNT(*) FROM shadow_portfolios WHERE portfolio_id='master_1000'"
                ).fetchone()[0],
                1,
            )

    def test_duplicate_closed_bar_consumes_sol_decision_at_fresh_quote(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            audit = AuditLog(Path(folder) / "audit.sqlite3")
            audit.set_kill_state(KillState.ACTIVE, "test", "ready")
            engine = AutonomousShadowEngine(load_config("config/demo.json"), audit)
            now = datetime(2026, 8, 10, 14, 0, tzinfo=timezone.utc)
            current_time = now

            class Collector:
                client = None

                def collect(self, symbol, instrument_id, interval, count, **kwargs):
                    closes = series(Decimal("90") + Decimal(instrument_id % 10), count)
                    candles = tuple(
                        CandleSnapshot(
                            now - timedelta(minutes=15 * (count - index)),
                            close,
                            close + Decimal("0.1"),
                            close - Decimal("0.1"),
                            close,
                        )
                        for index, close in enumerate(closes)
                    )
                    return MarketSnapshot(
                        symbol,
                        instrument_id,
                        Decimal("99.9"),
                        Decimal("100"),
                        closes,
                        candles=candles,
                        captured_at=current_time,
                        interval=interval,
                    )

            collector = Collector()
            engine.collect_and_tick(collector)
            pending = engine.ai.pending()
            self.assertEqual(len(pending), 1)
            engine.ai.decide(
                pending[0]["packet_id"],
                pending[0]["packet_hash"],
                "HOLD",
                "",
                Decimal("0.9"),
                ("wait",),
                "wait for stronger evidence",
                "gpt-5.6-sol",
            )
            current_time = now + timedelta(minutes=1)
            engine.collect_and_tick(collector)
            state = audit.db.execute(
                "SELECT state FROM ai_decision_packets WHERE packet_id=?",
                (pending[0]["packet_id"],),
            ).fetchone()[0]
            self.assertEqual(state, "CONSUMED")

    def test_sol_direct_intent_reaches_same_deterministic_risk_path(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            audit = AuditLog(Path(folder) / "audit.sqlite3")
            audit.set_kill_state(KillState.ACTIVE, "test", "ready")
            engine = AutonomousShadowEngine(load_config("config/demo.json"), audit)
            now = datetime.now(timezone.utc)

            def market(at: datetime, offset: Decimal) -> dict[str, MarketSnapshot]:
                return {
                    symbol: MarketSnapshot(
                        symbol, instrument.instrument_id, Decimal("99.9") + offset,
                        Decimal("100") + offset, series(Decimal("95") + offset),
                        captured_at=at, interval="FifteenMinutes",
                    )
                    for symbol, instrument in INSTRUMENTS_BY_SYMBOL.items()
                }

            engine.tick(market(now, Decimal("0")))
            packet = engine.ai.pending()[0]
            engine.ai.decide(
                packet["packet_id"], packet["packet_hash"], "OPEN", "",
                Decimal("0.75"), ("direct_edge",), "Sol direct bounded intent",
                "gpt-5.6-sol",
                intent={
                    "symbol": "AAPL", "side": "buy", "amount_usd": 250,
                    "stop_loss_fraction": 0.05, "take_profit_fraction": 0.10,
                    "max_holding_seconds": 21600,
                },
            )
            engine.tick(market(now + timedelta(minutes=15), Decimal("0.1")))
            position = engine._position("master_1000")
            self.assertIsNotNone(position)
            self.assertEqual(position[0], "AAPL")
            payload = json.loads(
                audit.db.execute(
                    "SELECT payload FROM events WHERE event_type='master_ai_open_result' "
                    "ORDER BY id DESC LIMIT 1"
                ).fetchone()[0]
            )
            self.assertEqual(payload["decision_source"], "sol_direct")
            self.assertTrue(payload["accepted_by_risk"])

    def test_each_strategy_evaluates_a_closed_bar_once(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            audit = AuditLog(Path(folder) / "audit.sqlite3")
            audit.set_kill_state(KillState.ACTIVE, "test", "ready")
            engine = AutonomousShadowEngine(load_config("config/demo.json"), audit)
            base = datetime(2026, 8, 10, 14, 1, tzinfo=timezone.utc)
            current_time = base
            advanced: set[str] = set()

            class Collector:
                client = None

                def collect(self, symbol, instrument_id, interval, count, **kwargs):
                    shift = timedelta(minutes=15) if symbol in advanced else timedelta(0)
                    closes = tuple(Decimal("100") for _ in range(count))
                    candles = tuple(
                        CandleSnapshot(
                            base - timedelta(minutes=15 * (count - index)) + shift,
                            close,
                            close,
                            close,
                            close,
                        )
                        for index, close in enumerate(closes)
                    )
                    return MarketSnapshot(
                        symbol,
                        instrument_id,
                        Decimal("99.9"),
                        Decimal("100"),
                        closes,
                        candles=candles,
                        captured_at=current_time,
                        interval=interval,
                        quote_observed_at=current_time,
                    )

            collector = Collector()
            first = engine.collect_and_tick(collector)
            self.assertEqual(len(first.strategy_results), STRATEGY_COUNT)
            first_count = audit.db.execute(
                "SELECT COUNT(*) FROM events WHERE event_type='strategy_snapshot'"
            ).fetchone()[0]

            current_time = base + timedelta(minutes=1)
            second = engine.collect_and_tick(collector)
            self.assertEqual(second.strategy_results, ())
            self.assertEqual(
                audit.db.execute(
                    "SELECT COUNT(*) FROM events WHERE event_type='strategy_snapshot'"
                ).fetchone()[0],
                first_count,
            )

            advanced.add("BTC")
            current_time = base + timedelta(minutes=16)
            third = engine.collect_and_tick(collector)
            self.assertEqual(
                [row["portfolio_id"] for row in third.strategy_results],
                ["strategy_04"],
            )

    def test_shadow_signal_fills_on_next_poll_not_next_bar(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            audit = AuditLog(Path(folder) / "audit.sqlite3")
            audit.set_kill_state(KillState.ACTIVE, "test", "ready")
            engine = AutonomousShadowEngine(load_config("config/demo.json"), audit)
            base = datetime(2026, 8, 10, 14, 1, tzinfo=timezone.utc)
            current_time = base

            class Collector:
                client = None

                def collect(self, symbol, instrument_id, interval, count, **kwargs):
                    closes = series(Decimal("90") + Decimal(instrument_id % 10), count)
                    candles = tuple(
                        CandleSnapshot(
                            base - timedelta(minutes=15 * (count - index)),
                            close,
                            close + Decimal("0.1"),
                            close - Decimal("0.1"),
                            close,
                        )
                        for index, close in enumerate(closes)
                    )
                    return MarketSnapshot(
                        symbol,
                        instrument_id,
                        Decimal("99.9"),
                        Decimal("100"),
                        closes,
                        candles=candles,
                        captured_at=current_time,
                        interval=interval,
                        quote_observed_at=current_time,
                    )

            collector = Collector()
            engine.collect_and_tick(collector)
            intent_count = audit.db.execute(
                "SELECT COUNT(*) FROM events WHERE event_type='trade_intent'"
            ).fetchone()[0]
            self.assertGreater(intent_count, 0)
            engine.collect_and_tick(collector)
            self.assertEqual(
                audit.db.execute("SELECT COUNT(*) FROM shadow_fills").fetchone()[0],
                0,
            )
            current_time = base + timedelta(minutes=1)
            engine.collect_and_tick(collector)
            self.assertEqual(
                audit.db.execute(
                    "SELECT COUNT(*) FROM events WHERE event_type='trade_intent'"
                ).fetchone()[0],
                intent_count,
            )
            self.assertGreater(
                audit.db.execute("SELECT COUNT(*) FROM shadow_fills").fetchone()[0],
                0,
            )

    def test_short_positions_are_marked_at_ask(self) -> None:
        snapshot = MarketSnapshot(
            "BTC",
            100000,
            Decimal("99"),
            Decimal("101"),
            (Decimal("100"),),
        )
        self.assertEqual(
            AutonomousShadowEngine._position_mark(
                snapshot, ("BTC", Decimal("-1"), Decimal("100"))
            ),
            Decimal("101"),
        )
        self.assertEqual(
            AutonomousShadowEngine._position_mark(
                snapshot, ("BTC", Decimal("1"), Decimal("100"))
            ),
            Decimal("99"),
        )

    def test_unreconciled_master_ack_locks_once_after_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            audit = AuditLog(Path(folder) / "audit.sqlite3")
            audit.set_kill_state(KillState.ACTIVE, "test", "ready")
            engine = AutonomousShadowEngine(load_config("config/demo.json"), audit)
            now = datetime.now(timezone.utc)
            pending: dict[str, object] = {
                "proposal_id": "proposal-timeout",
                "action": "OPEN",
                "symbol": "BTC",
                "created_at": (now - timedelta(seconds=121)).isoformat(),
            }
            engine._lock_stale_master_execution(pending, now)
            engine._lock_stale_master_execution(pending, now + timedelta(seconds=1))
            self.assertEqual(audit.kill_state(), KillState.LOCKED)
            self.assertEqual(
                audit.db.execute(
                    "SELECT COUNT(*) FROM events "
                    "WHERE event_type='master_execution_reconciliation_timeout'"
                ).fetchone()[0],
                1,
            )

    def test_expired_master_close_locks_when_broker_truth_has_drifted(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            runtime = Path(folder)
            key_path = runtime / "risk-signing.key"
            generate_private_signing_key(key_path)
            audit = AuditLog(runtime / "audit.sqlite3")
            audit.set_kill_state(KillState.ACTIVE, "test", "ready")
            previous_key = os.environ.get("ETORO_RISK_SIGNING_KEY_FILE")
            os.environ["ETORO_RISK_SIGNING_KEY_FILE"] = str(key_path)
            try:
                engine = AutonomousShadowEngine(
                    load_config("config/demo-execution.json"), audit
                )
            finally:
                if previous_key is None:
                    os.environ.pop("ETORO_RISK_SIGNING_KEY_FILE", None)
                else:
                    os.environ["ETORO_RISK_SIGNING_KEY_FILE"] = previous_key

            class EmptyDemoPortfolio:
                def execute_read(self, path, query=None, body=None):
                    return MCPResult(
                        200,
                        True,
                        {
                            "clientPortfolio": {
                                "positions": [],
                                "ordersForOpen": [],
                                "orders": [],
                            }
                        },
                        "read",
                        {},
                    )

            engine.demo_client = EmptyDemoPortfolio()
            now = datetime.now(timezone.utc)
            engine.master_ledger.record_fill(
                "master_1000",
                "OIL",
                "buy",
                Decimal("1"),
                Decimal("70"),
                executed_at=now - timedelta(minutes=5),
            )
            sealed_at = int((now - timedelta(seconds=61)).timestamp())
            close_result = engine.risk.evaluate_close(
                CloseIntent("OIL", 123, 17, None, "test expired close"),
                RiskContext(
                    equity_usd=Decimal("1000"),
                    peak_equity_usd=Decimal("1000"),
                    daily_pnl_usd=Decimal("0"),
                    gross_exposure_usd=Decimal("70"),
                    symbol_exposure_usd=Decimal("70"),
                    trades_today=1,
                    bid=Decimal("70"),
                    ask=Decimal("70.1"),
                    kill_switch_active=False,
                    quote_observed_at=sealed_at,
                    evaluated_at=sealed_at,
                ),
            )
            assert close_result.order is not None
            engine._register_demo_proposal(
                close_result.order, "sol_master_close"
            )
            engine._set_master_pending_execution(
                "CLOSE", close_result.order, symbol="OIL"
            )
            snapshot = MarketSnapshot(
                "OIL",
                17,
                Decimal("70"),
                Decimal("70.1"),
                (Decimal("70"),),
                captured_at=now,
                quote_observed_at=now,
            )

            engine._reconcile_master_pending_execution({"OIL": snapshot}, now)

            proposal = audit.proposal(close_result.order.proposal_id)
            assert proposal is not None
            self.assertEqual(proposal["state"], "REJECTED")
            self.assertEqual(audit.state_get("master_pending_execution", "missing"), "")
            self.assertIsNotNone(engine._position("master_1000"))
            self.assertEqual(audit.kill_state(), KillState.LOCKED)
            drift = json.loads(audit.state_get("master_reconciliation_drift", "{}"))
            self.assertEqual(drift["action"], "CLOSE")
            self.assertEqual(drift["broker_position_ids"], [])
            self.assertTrue(audit.verify_chain())

    def test_server_side_demo_close_uses_exact_broker_history_without_a_write(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            runtime = Path(folder)
            key_path = runtime / "risk-signing.key"
            generate_private_signing_key(key_path)
            audit = AuditLog(runtime / "audit.sqlite3")
            audit.set_kill_state(KillState.ACTIVE, "test", "ready")
            previous_key = os.environ.get("ETORO_RISK_SIGNING_KEY_FILE")
            os.environ["ETORO_RISK_SIGNING_KEY_FILE"] = str(key_path)
            try:
                engine = AutonomousShadowEngine(
                    load_config("config/demo-execution.json"), audit
                )
            finally:
                if previous_key is None:
                    os.environ.pop("ETORO_RISK_SIGNING_KEY_FILE", None)
                else:
                    os.environ["ETORO_RISK_SIGNING_KEY_FILE"] = previous_key

            now = datetime(2026, 8, 11, 8, 0, tzinfo=timezone.utc)
            units = Decimal("1000") / Decimal("79.61")
            engine.master_ledger.record_fill(
                "master_1000",
                "OIL",
                "buy",
                units,
                Decimal("79.61"),
                executed_at=now - timedelta(hours=18),
            )
            audit.state_set("master_broker_position_id", "3577917785")

            class ClosedByTakeProfitClient:
                def __init__(self):
                    self.read_paths: list[str] = []

                def execute_read(self, path, query=None, body=None):
                    self.read_paths.append(path)
                    if path.endswith("/portfolio"):
                        return MCPResult(
                            200,
                            True,
                            {
                                "clientPortfolio": {
                                    "positions": [],
                                    "ordersForOpen": [],
                                    "orders": [],
                                }
                            },
                            "portfolio-read",
                            {},
                        )
                    if path.endswith("/history"):
                        if query != {
                            "minDate": "2026-08-10",
                            "page": "1",
                            "pageSize": "100",
                        }:
                            raise AssertionError(query)
                        return MCPResult(
                            200,
                            True,
                            [
                                {
                                    "positionId": 3577917785,
                                    "instrumentId": 17,
                                    "isBuy": True,
                                    "openRate": 79.61,
                                    "openTimestamp": "2026-08-10T13:49:45.417Z",
                                    "closeRate": 82.76,
                                    "closeTimestamp": "2026-08-11T06:44:38.377Z",
                                    "netProfit": 39.57,
                                    "fees": 0.0,
                                    "units": 12.561236,
                                    "initialInvestment": 1000.0,
                                    "investment": 1000.0,
                                    "orderId": 372516753,
                                }
                            ],
                            "history-read",
                            {},
                        )
                    raise AssertionError(path)

            client = ClosedByTakeProfitClient()
            engine.demo_client = client
            engine._reconcile_master_external_close(now)

            self.assertIsNone(engine._position("master_1000"))
            state = engine.master_ledger.snapshot("master_1000", as_of=now)
            self.assertEqual(state.equity_usd, Decimal("1039.57"))
            self.assertEqual(state.realized_pnl_usd, Decimal("39.57"))
            self.assertEqual(audit.kill_state(), KillState.ACTIVE)
            self.assertEqual(
                client.read_paths,
                [
                    "/api/v1/trading/info/demo/portfolio",
                    "/api/v1/trading/info/trade/demo/history",
                ],
            )
            self.assertTrue(audit.verify_chain())

    def test_closed_market_signals_are_audited_but_never_queued(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            audit = AuditLog(Path(folder) / "audit.sqlite3")
            audit.set_kill_state(KillState.ACTIVE, "test", "ready")
            engine = AutonomousShadowEngine(load_config("config/demo.json"), audit)
            now = datetime.now(timezone.utc)
            snapshots = {
                symbol: MarketSnapshot(
                    symbol,
                    instrument.instrument_id,
                    Decimal("99.9"),
                    Decimal("100"),
                    series(Decimal("90") + Decimal(index)),
                    captured_at=now,
                    interval="FifteenMinutes",
                    quote_observed_at=now,
                    market_open=False,
                )
                for index, (symbol, instrument) in enumerate(
                    INSTRUMENTS_BY_SYMBOL.items()
                )
            }
            engine.tick(snapshots)
            intents = [
                json.loads(row[0])
                for row in audit.db.execute(
                    "SELECT payload FROM events WHERE event_type='trade_intent'"
                ).fetchall()
            ]
            self.assertGreater(len(intents), 0)
            self.assertTrue(
                all(item["accepted_for_execution"] is False for item in intents)
            )
            self.assertTrue(
                all(
                    audit.state_get(f"shadow_pending_intent:strategy_{index:02d}", "")
                    == ""
                    for index in range(1, 13)
                )
            )
            self.assertEqual(engine.ai.pending(), ())

    def test_demo_master_changes_only_after_ack_and_broker_truth(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            runtime = Path(folder)
            key_path = runtime / "risk-signing.key"
            generate_private_signing_key(key_path)
            audit = AuditLog(runtime / "audit.sqlite3")
            audit.set_kill_state(KillState.ACTIVE, "test", "ready")
            previous_key = os.environ.get("ETORO_RISK_SIGNING_KEY_FILE")
            os.environ["ETORO_RISK_SIGNING_KEY_FILE"] = str(key_path)
            try:
                engine = AutonomousShadowEngine(
                    load_config("config/demo-execution.json"), audit
                )
            finally:
                if previous_key is None:
                    os.environ.pop("ETORO_RISK_SIGNING_KEY_FILE", None)
                else:
                    os.environ["ETORO_RISK_SIGNING_KEY_FILE"] = previous_key

            class DemoClient:
                positions: list[dict[str, int]] = []

                def execute_read(self, path, query=None, body=None):
                    if path.endswith("/eligibility"):
                        return MCPResult(
                            200,
                            True,
                            {
                                "eligibilities": [
                                    {
                                        "allowOpenPosition": True,
                                        "allowedOrderQuantityType": "all",
                                        "minPositionExposure": 10,
                                        "leverageConfigs": [
                                            {
                                                "settlementType": "real",
                                                "direction": "long",
                                                "leverageValues": [1],
                                                "minPositionAmount": 10,
                                                "allowStopLossTakeProfit": True,
                                                "minStopLossPercentage": 0,
                                                "maxStopLossPercentage": 100,
                                                "minTakeProfitPercentage": 0,
                                                "maxTakeProfitPercentage": 1000,
                                            },
                                            {
                                                "settlementType": "cfd",
                                                "direction": "short",
                                                "leverageValues": [1],
                                                "minPositionAmount": 10,
                                                "allowStopLossTakeProfit": True,
                                                "minStopLossPercentage": 0,
                                                "maxStopLossPercentage": 100,
                                                "minTakeProfitPercentage": 0,
                                                "maxTakeProfitPercentage": 1000,
                                            },
                                        ],
                                    }
                                ]
                            },
                            "read",
                            {},
                        )
                    if path.endswith("/costs"):
                        return MCPResult(200, True, {"costs": []}, "read", {})
                    return MCPResult(
                        200,
                        True,
                        {
                            "clientPortfolio": {
                                "positions": self.positions,
                                "ordersForOpen": [],
                                "orders": [],
                            }
                        },
                        "read",
                        {},
                    )

            client = DemoClient()
            engine.demo_client = client
            now = datetime.now(timezone.utc)

            def snapshots(at: datetime) -> dict[str, MarketSnapshot]:
                return {
                    symbol: MarketSnapshot(
                        symbol,
                        instrument.instrument_id,
                        Decimal("99.9"),
                        Decimal("100"),
                        series(Decimal("90") + Decimal(index)),
                        captured_at=at,
                        interval="FifteenMinutes",
                        quote_observed_at=at,
                    )
                    for index, (symbol, instrument) in enumerate(
                        INSTRUMENTS_BY_SYMBOL.items()
                    )
                }

            engine.tick(snapshots(now))
            pending_ai = engine.ai.pending()[0]
            candidate = pending_ai["payload"]["candidates"][0]
            engine.ai.decide(
                pending_ai["packet_id"],
                pending_ai["packet_hash"],
                "OPEN",
                candidate["candidate_id"],
                Decimal("0.8"),
                ("test",),
                "test acknowledged lifecycle",
                "gpt-5.6-sol",
            )
            engine.tick(snapshots(now + timedelta(seconds=1)))
            self.assertIsNone(engine._position("master_1000"))
            pending_execution = json.loads(
                audit.state_get("master_pending_execution", "")
            )
            proposal_id = pending_execution["proposal_id"]
            proposal = audit.proposal(proposal_id)
            assert proposal is not None
            audit.approve_once(
                proposal_id,
                str(proposal["envelope_hash"]),
                "standing-demo-policy",
            )
            audit.begin_execution(
                proposal_id, str(proposal["envelope_hash"]), proposal_id
            )
            audit.finish_execution(
                proposal_id, ExecutionState.ACKNOWLEDGED, {"orderId": 123}
            )
            symbol = str(pending_execution["symbol"])
            intent = pending_execution["intent"]
            amount = Decimal(str(intent["amount_usd"]))
            open_rate = Decimal("100")
            units = amount / open_rate
            client.positions = [
                {
                    "positionID": 123,
                    "orderID": 123,
                    "instrumentID": INSTRUMENTS_BY_SYMBOL[symbol].instrument_id,
                    "isBuy": str(intent["side"]) == "buy",
                    "units": units,
                    "initialUnits": units,
                    "openRate": open_rate,
                    "openDateTime": now.isoformat(),
                    "amount": amount,
                    "initialAmountInDollars": amount,
                    "leverage": 1,
                    "totalFees": Decimal("0"),
                }
            ]
            engine.tick(snapshots(now + timedelta(seconds=2)))
            self.assertEqual(
                engine._position("master_1000"),
                (symbol, units, open_rate),
            )
            self.assertEqual(audit.state_get("master_pending_execution", ""), "")

    def test_broker_minimum_rejects_before_a_demo_proposal_exists(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            runtime = Path(folder)
            key_path = runtime / "risk-signing.key"
            generate_private_signing_key(key_path)
            audit = AuditLog(runtime / "audit.sqlite3")
            audit.set_kill_state(KillState.ACTIVE, "test", "ready")
            previous_key = os.environ.get("ETORO_RISK_SIGNING_KEY_FILE")
            os.environ["ETORO_RISK_SIGNING_KEY_FILE"] = str(key_path)
            try:
                engine = AutonomousShadowEngine(
                    load_config("config/demo-execution.json"), audit
                )
            finally:
                if previous_key is None:
                    os.environ.pop("ETORO_RISK_SIGNING_KEY_FILE", None)
                else:
                    os.environ["ETORO_RISK_SIGNING_KEY_FILE"] = previous_key

            class MinimumClient:
                def execute_read(self, path, query=None, body=None):
                    return MCPResult(
                        200,
                        True,
                        {
                            "eligibilities": [
                                {
                                    "allowOpenPosition": True,
                                    "allowedOrderQuantityType": "all",
                                    "minPositionExposure": 1000,
                                    "leverageConfigs": [],
                                }
                            ]
                        },
                        "read",
                        {},
                    )

            engine.demo_client = MinimumClient()
            now = datetime.now(timezone.utc)
            intent = TradeIntent(
                "EURUSD",
                Side.BUY,
                Decimal("100"),
                Decimal("0.8"),
                "broker minimum test",
                Decimal("0.02"),
                Decimal("0.04"),
            )
            approved, reasons, order = engine._prepare_master_open(
                intent,
                MarketSnapshot(
                    "EURUSD",
                    1,
                    Decimal("1.15"),
                    Decimal("1.151"),
                    series(Decimal("1.1")),
                    captured_at=now,
                    quote_observed_at=now,
                ),
                now,
            )
            self.assertFalse(approved)
            self.assertEqual(reasons, ("broker_eligibility_rejected",))
            self.assertIsNone(order)
            self.assertEqual(
                audit.db.execute("SELECT COUNT(*) FROM approvals").fetchone()[0], 0
            )
            self.assertEqual(audit.kill_state(), KillState.ACTIVE)


if __name__ == "__main__":
    unittest.main()
