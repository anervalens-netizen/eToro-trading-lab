from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from etoro_agent.audit import AuditLog
from etoro_agent.config import load_config
from etoro_agent.engine import AutonomousShadowEngine
from etoro_agent.market import INSTRUMENTS_BY_SYMBOL, MarketSnapshot
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


if __name__ == "__main__":
    unittest.main()
