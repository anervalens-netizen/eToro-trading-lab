from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from etoro_agent import dashboard
from etoro_agent.dashboard import DashboardService, OwnerIdentityPolicy, sanitize


SCHEMA = """
CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE
);
CREATE TABLE approvals (
    proposal_id TEXT PRIMARY KEY,
    request_json TEXT NOT NULL,
    approved_at TEXT,
    consumed_at TEXT
);
CREATE TABLE pnl_daily (
    day TEXT PRIMARY KEY,
    realized_usd TEXT NOT NULL,
    unrealized_usd TEXT NOT NULL,
    equity_usd TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class DashboardServiceTests(unittest.TestCase):
    def test_missing_store_returns_fail_closed_twelve_strategy_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            snapshot = DashboardService(root / "missing.sqlite3", root).snapshot()

        self.assertEqual(snapshot["schema_version"], 1)
        self.assertEqual(snapshot["overview"]["account_mode"], "DEMO")
        self.assertFalse(snapshot["overview"]["real_money"])
        self.assertTrue(snapshot["overview"]["read_only"])
        self.assertEqual(snapshot["overview"]["strategy_count"], 12)
        self.assertEqual(snapshot["overview"]["shadow_capital_usd"], "12000.00")
        self.assertEqual(len(snapshot["strategies"]), 12)
        self.assertEqual(snapshot["health"]["status"], "degraded")
        self.assertFalse(snapshot["audit"]["readable"])

    def test_snapshot_projects_operations_and_redacts_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            database = root / "audit.sqlite3"
            with sqlite3.connect(database) as connection:
                connection.executescript(SCHEMA)
                events = [
                    (
                        "2026-08-09T09:00:00+00:00",
                        "strategy_snapshot",
                        {
                            "strategy_id": "orb_15m_immediate",
                            "status": "shadow_live",
                            "nav_usd": "1012.50",
                            "daily_pnl_usd": "12.50",
                            "total_pnl_usd": "12.50",
                            "drawdown_fraction": "0.01",
                            "trades": 4,
                            "rank": 1,
                            "api_key": "must-not-leak",
                        },
                    ),
                    (
                        "2026-08-09T09:01:00+00:00",
                        "risk_approval",
                        {
                            "proposal_id": "proposal-1",
                            "strategy_id": "orb_15m_immediate",
                            "request": {
                                "account": "DEMO",
                                "method": "POST",
                                "path": "/api/v2/trading/execution/demo/orders",
                                "body": {"symbol": "AAPL", "amount": 25},
                                "headers": {"Authorization": "Bearer must-not-leak"},
                            },
                        },
                    ),
                    (
                        "2026-08-09T09:02:00+00:00",
                        "operator_approval",
                        {"proposal_id": "proposal-1"},
                    ),
                ]
                previous = "0" * 64
                for index, (timestamp, event_type, payload) in enumerate(events, start=1):
                    event_hash = f"{index:064x}"
                    connection.execute(
                        "INSERT INTO events(ts,event_type,payload,previous_hash,event_hash) VALUES(?,?,?,?,?)",
                        (timestamp, event_type, json.dumps(payload), previous, event_hash),
                    )
                    previous = event_hash
                connection.execute(
                    "INSERT INTO approvals VALUES(?,?,NULL,NULL)",
                    (
                        "proposal-1",
                        json.dumps(
                            {
                                "account": "DEMO",
                                "path": "/api/v2/trading/execution/demo/orders",
                                "body": {"symbol": "AAPL", "amount": 25},
                                "user_token": "must-not-leak",
                            }
                        ),
                    ),
                )
                connection.execute(
                    "INSERT INTO pnl_daily VALUES(?,?,?,?,?)",
                    ("2026-08-09", "5.25", "-1.00", "12004.25", "2026-08-09T09:03:00+00:00"),
                )
                connection.commit()
            (root / "KILL_SWITCH").touch()

            snapshot = DashboardService(database, root).snapshot()

        encoded = json.dumps(snapshot)
        self.assertNotIn("must-not-leak", encoded)
        self.assertIn("[REDACTED]", encoded)
        first = next(item for item in snapshot["strategies"] if item["id"] == "orb_15m_immediate")
        self.assertEqual(first["rank"], 1)
        self.assertTrue(first["top3"])
        self.assertEqual(first["nav_usd"], "1012.50")
        self.assertEqual(snapshot["overview"]["daily_pnl_usd"], "4.25")
        self.assertEqual(snapshot["overview"]["pending_approvals"], 1)
        self.assertEqual(snapshot["overview"]["audit_events"], 3)
        self.assertTrue(snapshot["kill_switch"]["active"])
        self.assertEqual(snapshot["health"]["status"], "halted")
        self.assertEqual(snapshot["orders"][0]["status"], "approved")
        self.assertEqual(len(snapshot["orders"][0]["lifecycle"]), 2)
        self.assertTrue(snapshot["approvals"][0]["read_only"])

    def test_snapshot_never_mutates_database(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            database = root / "audit.sqlite3"
            with sqlite3.connect(database) as connection:
                connection.executescript(SCHEMA)
                connection.commit()
            before = database.read_bytes()
            DashboardService(database, root).snapshot()
            after = database.read_bytes()
        self.assertEqual(before, after)

    def test_snapshot_supports_durable_state_machine_pnl_v2_and_heartbeats(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            database = root / "audit.sqlite3"
            with sqlite3.connect(database) as connection:
                connection.executescript(SCHEMA)
                connection.executescript(
                    """
                    ALTER TABLE approvals ADD COLUMN state TEXT;
                    ALTER TABLE approvals ADD COLUMN actor TEXT;
                    ALTER TABLE approvals ADD COLUMN expires_at INTEGER;
                    ALTER TABLE approvals ADD COLUMN envelope_hash TEXT;
                    ALTER TABLE approvals ADD COLUMN x_request_id TEXT;
                    ALTER TABLE approvals ADD COLUMN response_json TEXT;
                    ALTER TABLE approvals ADD COLUMN last_updated TEXT;
                    CREATE TABLE pnl_daily_v2 (
                        portfolio_id TEXT NOT NULL,
                        day TEXT NOT NULL,
                        realized_usd TEXT NOT NULL,
                        unrealized_usd TEXT NOT NULL,
                        fees_usd TEXT NOT NULL,
                        financing_usd TEXT NOT NULL,
                        equity_usd TEXT NOT NULL,
                        recorded_at TEXT NOT NULL,
                        PRIMARY KEY(portfolio_id, day)
                    );
                    CREATE TABLE service_heartbeats (
                        service TEXT PRIMARY KEY,
                        status TEXT NOT NULL,
                        details TEXT NOT NULL,
                        recorded_at TEXT NOT NULL
                    );
                    """
                )
                connection.execute("INSERT INTO state VALUES('kill_state','ACTIVE')")
                connection.execute(
                    "INSERT INTO approvals VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        "proposal-2",
                        json.dumps({"account": "DEMO"}),
                        None,
                        None,
                        "AWAITING_APPROVAL",
                        None,
                        2_000_000_000,
                        "a" * 64,
                        None,
                        None,
                        "2026-08-09T10:00:00+00:00",
                    ),
                )
                for portfolio_id, equity in (("shadow-1", "1002"), ("shadow-2", "999")):
                    connection.execute(
                        "INSERT INTO pnl_daily_v2 VALUES(?,?,?,?,?,?,?,?)",
                        (portfolio_id, "2026-08-09", "1", "0.5", "0.1", "0.2", equity, "2026-08-09T10:00:00+00:00"),
                    )
                connection.execute(
                    "INSERT INTO service_heartbeats VALUES(?,?,?,?)",
                    ("collector", "error", json.dumps({"reason": "stale data"}), "2026-08-09T10:00:00+00:00"),
                )
                connection.commit()

            snapshot = DashboardService(database, root).snapshot()

        self.assertFalse(snapshot["kill_switch"]["active"])
        self.assertEqual(snapshot["kill_switch"]["mode"], "ACTIVE")
        self.assertEqual(snapshot["approvals"][0]["status"], "awaiting_owner")
        self.assertEqual(snapshot["pnl"]["daily"][0]["portfolio_count"], "2")
        self.assertEqual(snapshot["pnl"]["daily"][0]["equity_usd"], "2001")
        self.assertEqual(snapshot["health"]["status"], "degraded")
        self.assertTrue(any(check["name"] == "service:collector" for check in snapshot["health"]["checks"]))

    def test_shadow_ledger_projection_maps_portfolio_to_strategy(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            database = root / "audit.sqlite3"
            with sqlite3.connect(database) as connection:
                connection.executescript(SCHEMA)
                connection.executescript(
                    """
                    CREATE TABLE shadow_daily_pnl (
                        portfolio_id TEXT NOT NULL, day TEXT NOT NULL,
                        realized_pnl_usd TEXT NOT NULL, unrealized_pnl_usd TEXT NOT NULL,
                        fees_usd TEXT NOT NULL, financing_usd TEXT NOT NULL,
                        daily_pnl_usd TEXT NOT NULL, equity_usd TEXT NOT NULL,
                        recorded_at TEXT NOT NULL, PRIMARY KEY(portfolio_id,day)
                    );
                    """
                )
                connection.execute(
                    "INSERT INTO events(ts,event_type,payload,previous_hash,event_hash) VALUES(?,?,?,?,?)",
                    (
                        "2026-08-09T11:00:00+00:00",
                        "shadow_portfolio_snapshot",
                        json.dumps(
                            {
                                "portfolio_id": "strategy_01",
                                "initial_cash_usd": "1000",
                                "equity_usd": "1015",
                                "peak_equity_usd": "1020",
                                "daily_pnl_usd": "15",
                                "trades_today": 3,
                            }
                        ),
                        "0" * 64,
                        "b" * 64,
                    ),
                )
                connection.execute(
                    "INSERT INTO shadow_daily_pnl VALUES(?,?,?,?,?,?,?,?,?)",
                    ("strategy_01", "2026-08-09", "12", "5", "1", "1", "15", "1015", "2026-08-09T11:00:00+00:00"),
                )
                connection.commit()

            snapshot = DashboardService(database, root).snapshot()

        card = next(item for item in snapshot["strategies"] if item["id"] == "orb_15m_immediate")
        self.assertEqual(card["nav_usd"], "1015")
        self.assertEqual(card["total_pnl_usd"], "15")
        self.assertEqual(card["trades"], 3)
        self.assertEqual(snapshot["overview"]["daily_pnl_usd"], "15")
        self.assertEqual(snapshot["pnl"]["daily"][0]["portfolio_count"], "1")

    def test_sanitizer_bounds_untrusted_payloads(self) -> None:
        cleaned = sanitize(
            {
                "x-api-key": "private",
                "nested": {"password": "private", "safe": "visible"},
                "long": "x" * 2_200,
            }
        )
        self.assertEqual(cleaned["x-api-key"], "[REDACTED]")
        self.assertEqual(cleaned["nested"]["password"], "[REDACTED]")
        self.assertEqual(sanitize({"pass_word": "secret"})["pass_word"], "[REDACTED]")
        self.assertEqual(sanitize({"pwd": "secret"})["pwd"], "[REDACTED]")
        self.assertEqual(cleaned["nested"]["safe"], "visible")
        self.assertTrue(cleaned["long"].endswith("[TRUNCATED]"))


class DashboardAccessTests(unittest.TestCase):
    def test_owner_identity_policy_fails_closed_and_matches_exactly(self) -> None:
        self.assertFalse(OwnerIdentityPolicy(None).allows({"x-authentik-username": "andrei"}))
        policy = OwnerIdentityPolicy("andrei")
        self.assertTrue(policy.allows({"x-authentik-username": "andrei"}))
        self.assertFalse(policy.allows({"x-authentik-username": "Andrei"}))
        self.assertFalse(policy.allows({}))

    def test_fastapi_factory_exposes_only_explicit_owner_control_writes(self) -> None:
        if dashboard.FastAPI is None:
            with self.assertRaisesRegex(RuntimeError, "optional and not installed"):
                dashboard.create_app()
            return
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            service = DashboardService(root / "missing.sqlite3", root)
            app = dashboard.create_app(service, owner_username="andrei")
        routes = {(method, route.path) for route in app.routes for method in getattr(route, "methods", set())}
        self.assertIn(("GET", "/api/snapshot"), routes)
        self.assertIn(("GET", "/api/events"), routes)
        self.assertIn(("GET", "/healthz"), routes)
        self.assertEqual(
            {path for method, path in routes if method == "POST"},
            {"/api/control/kill", "/api/control/resume", "/api/approvals/{proposal_id}"},
        )
        self.assertFalse(any(method in {"PUT", "PATCH", "DELETE"} for method, _ in routes))


if __name__ == "__main__":
    unittest.main()
