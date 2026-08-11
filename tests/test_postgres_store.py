from __future__ import annotations

import os
import re
import sysconfig
import unittest
from datetime import UTC, datetime
from pathlib import Path

from etoro_agent.postgres_store import (
    ALLOWED_EXECUTION_TRANSITIONS,
    EXECUTION_STATES,
    KILL_STATES,
    SCHEMA_PATH,
    ZERO_HASH,
    PostgresOperationalStore,
    canonical_json,
    compute_event_hash,
    ensure_no_credentials,
    load_schema,
    psycopg_available,
    validate_execution_transition,
)


class PostgresStoreContractTests(unittest.TestCase):
    def test_schema_contains_all_operational_tables_and_append_only_guards(self) -> None:
        schema = load_schema()
        repository_schema = Path(__file__).parents[1] / "ops/postgres/schema.sql"
        installed_schema = Path(sysconfig.get_path("data")) / "share/etoro-demo-agent/schema.sql"
        self.assertIn(SCHEMA_PATH, (repository_schema, installed_schema))
        self.assertTrue(SCHEMA_PATH.is_file())
        for table in (
            "events",
            "proposals",
            "approvals",
            "execution_transitions",
            "kill_switch",
            "pnl_daily",
            "service_heartbeats",
        ):
            self.assertRegex(schema, rf"CREATE TABLE IF NOT EXISTS {table}\b")
        self.assertIn("events_append_only", schema)
        self.assertIn("execution_transitions_append_only", schema)
        self.assertIn("BEFORE UPDATE OR DELETE", schema)
        self.assertIn("'LOCKED', 'bootstrap', 'fail-closed initialization'", schema)
        self.assertIn("NUMERIC(38, 18)", schema)
        self.assertNotIn("BEGIN;", schema)
        self.assertNotIn("COMMIT;", schema)

    def test_schema_and_python_contract_have_identical_state_sets(self) -> None:
        schema = load_schema()
        quoted = set(re.findall(r"'([A-Z_]+)'", schema))
        self.assertTrue(EXECUTION_STATES.issubset(quoted))
        self.assertTrue(KILL_STATES.issubset(quoted))
        self.assertEqual(set(ALLOWED_EXECUTION_TRANSITIONS), set(EXECUTION_STATES))

    def test_hash_chain_is_canonical_and_tamper_evident(self) -> None:
        body_a = canonical_json(
            {
                "payload": {"z": 2, "a": 1},
                "event_type": "decision",
                "ts": "2026-08-09T12:00:00.000000+00:00",
            }
        )
        body_b = canonical_json(
            {
                "ts": "2026-08-09T12:00:00.000000+00:00",
                "event_type": "decision",
                "payload": {"a": 1, "z": 2},
            }
        )
        self.assertEqual(body_a, body_b)
        digest = compute_event_hash(ZERO_HASH, body_a)
        self.assertEqual(len(digest), 64)
        self.assertNotEqual(digest, compute_event_hash(ZERO_HASH, body_a + " "))
        with self.assertRaises(ValueError):
            compute_event_hash("not-a-hash", body_a)

    def test_invalid_execution_transitions_fail_closed(self) -> None:
        validate_execution_transition("AWAITING_APPROVAL", "APPROVED")
        validate_execution_transition("APPROVED", "SENDING")
        validate_execution_transition("SENDING", "UNKNOWN")
        validate_execution_transition("UNKNOWN", "RECONCILED")
        with self.assertRaises(ValueError):
            validate_execution_transition("AWAITING_APPROVAL", "SENDING")
        with self.assertRaises(ValueError):
            validate_execution_transition("RECONCILED", "APPROVED")

    def test_credential_like_fields_are_rejected_recursively(self) -> None:
        ensure_no_credentials({"proposal": {"symbol": "BTC"}})
        for payload in (
            {"api_key": "redacted"},
            {"nested": {"Authorization": "redacted"}},
            {"items": [{"user-key": "redacted"}]},
        ):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                ensure_no_credentials(payload)

    def test_psycopg_is_an_explicit_optional_capability(self) -> None:
        self.assertIsInstance(psycopg_available(), bool)
        self.assertFalse(hasattr(PostgresOperationalStore, "dsn"))


@unittest.skipUnless(
    bool(os.getenv("ETORO_TEST_POSTGRES_DSN")) and psycopg_available(),
    "optional PostgreSQL integration DSN/dependency absent",
)
class PostgresStoreIntegrationTests(unittest.TestCase):
    def test_migration_and_fail_closed_default(self) -> None:
        store = PostgresOperationalStore.from_dsn(os.environ["ETORO_TEST_POSTGRES_DSN"])
        try:
            store.migrate()
            self.assertEqual(store.kill_state()[0], "LOCKED")
            digest = store.append_event(
                "integration_contract",
                {"status": "ok"},
                occurred_at=datetime.now(UTC),
            )
            self.assertEqual(len(digest), 64)
            self.assertTrue(store.verify_event_chain())
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
