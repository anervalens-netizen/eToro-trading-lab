from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from etoro_agent.audit import AuditLog
from etoro_agent.config import load_config
from etoro_agent.engine import AutonomousShadowEngine
from etoro_agent.market import CandleSnapshot, INSTRUMENTS_BY_SYMBOL, MarketSnapshot
from etoro_agent.models import KillState


def series(start: Decimal, count: int = 250) -> tuple[Decimal, ...]:
    return tuple(start + Decimal(index) / Decimal("100") for index in range(count))


class ShadowEngineTests(unittest.TestCase):
    def test_twelve_strategy_tick_is_offline_isolated_and_audited(self) -> None:
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
            self.assertEqual(len(result.strategy_results), 12)
            self.assertEqual(
                len({row["portfolio_id"] for row in result.strategy_results}), 12
            )
            self.assertEqual(
                audit.db.execute("SELECT COUNT(*) FROM approvals").fetchone()[0], 0
            )
            self.assertEqual(
                audit.db.execute(
                    "SELECT COUNT(*) FROM events WHERE event_type='strategy_snapshot'"
                ).fetchone()[0],
                12,
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

                def collect(self, symbol, instrument_id, interval, count):
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


if __name__ == "__main__":
    unittest.main()
