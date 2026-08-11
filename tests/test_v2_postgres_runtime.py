from __future__ import annotations

import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from etoro_agent.codec_v2 import decode_dataclass
from etoro_agent.config_v2 import load_config_v2
from etoro_agent.domain_v2 import DomainEvent, IntentEnvelope, QuoteProvenance, Side
from etoro_agent.kernel_v2 import UnifiedTradingKernel
from etoro_agent.postgres_runtime_v2 import PostgresRuntimeStoreV2
from etoro_agent.postgres_store_v2 import psycopg_available
from etoro_agent.risk_v2 import BrokerTruth, GlobalRiskKernel


class V2CodecTests(unittest.TestCase):
    def test_domain_codec_restores_decimal_datetime_and_nested_types(self) -> None:
        now = datetime(2026, 8, 10, 12, tzinfo=UTC)
        quote = QuoteProvenance(
            "AAPL",
            Decimal("99.9"),
            Decimal("100"),
            now,
            now,
            "test",
            "1",
            "m" * 64,
            "b" * 64,
        )
        restored = decode_dataclass(QuoteProvenance, asdict(quote))
        self.assertEqual(restored, quote)
        self.assertIsInstance(restored.bid, Decimal)
        self.assertEqual(restored.quote_observed_at.tzinfo, UTC)


@unittest.skipUnless(
    bool(os.getenv("ETORO_TEST_POSTGRES_DSN")) and psycopg_available(),
    "optional v2 PostgreSQL integration DSN absent",
)
class V2PostgresRuntimeIntegrationTests(unittest.TestCase):
    @contextmanager
    def _temporary_database(self):
        import psycopg
        from psycopg import sql
        from psycopg.conninfo import conninfo_to_dict, make_conninfo

        base_dsn = os.environ["ETORO_TEST_POSTGRES_DSN"]
        parameters = conninfo_to_dict(base_dsn)
        database_name = "etoro_v2_test_" + uuid4().hex
        admin = psycopg.connect(base_dsn, autocommit=True)
        try:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
        finally:
            admin.close()
        try:
            yield make_conninfo(**{**parameters, "dbname": database_name})
        finally:
            admin = psycopg.connect(base_dsn, autocommit=True)
            try:
                admin.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname=%s AND pid<>pg_backend_pid()",
                    (database_name,),
                )
                admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(database_name)))
            finally:
                admin.close()

    def test_migration_fail_closed_state_and_hash_chain(self) -> None:
        with self._temporary_database() as dsn:
            store = PostgresRuntimeStoreV2.from_dsn(dsn)
            try:
                store.migrate()
                self.assertEqual(store.state_get("trading_state", "missing"), "LOCKED")
                now = datetime.now(UTC)
                event = DomainEvent(
                    "v2-pg-integration-event",
                    "V2IntegrationContract",
                    2,
                    now,
                    now,
                    "v2-pg-integration-idempotency",
                    "",
                    "integration",
                    {"status": "ok"},
                )
                store.append_event(event)
                self.assertTrue(store.verify_event_chain())
            finally:
                store.close()

    def test_concurrent_postgres_proposals_cannot_double_spend_reservation(self) -> None:
        with self._temporary_database() as dsn:
            migration_store = PostgresRuntimeStoreV2.from_dsn(dsn)
            try:
                migration_store.migrate()
            finally:
                migration_store.close()
            config = load_config_v2("config/v2-demo-execution.json")
            now = datetime.now(UTC)
            run_id = uuid4().hex
            quote = QuoteProvenance(
                "AAPL",
                Decimal("99.9"),
                Decimal("100"),
                now,
                now,
                "postgres-integration",
                run_id,
                "market-" + run_id,
                "broker-" + run_id,
            )
            broker = BrokerTruth(
                Decimal("1000"),
                Decimal("1000"),
                Decimal("1000"),
                Decimal("0"),
                Decimal("0"),
                0,
                Decimal("0"),
                Decimal("0"),
                Decimal("0"),
                Decimal("0"),
                "broker-" + run_id,
                now,
            )

            def submit(index: int) -> str:
                store = PostgresRuntimeStoreV2.from_dsn(dsn)
                try:
                    store.require_schema()
                    kernel = UnifiedTradingKernel(store, GlobalRiskKernel(config.mandate))
                    intent = IntentEnvelope(
                        f"intent-pg-reservation-{run_id}-{index}",
                        "master_1000",
                        "A_deterministic",
                        "postgres-reservation-test",
                        "v2",
                        "AAPL",
                        Side.BUY,
                        Decimal("600"),
                        Decimal("0.8"),
                        Decimal("0.6"),
                        Decimal("0.01"),
                        Decimal("0.04"),
                        3600,
                        now,
                        now,
                        now + timedelta(minutes=5),
                        Decimal("99.9"),
                        Decimal("100"),
                        Decimal("50"),
                        Decimal("25"),
                        "market-" + run_id,
                        correlation_id=f"pg-reservation-{run_id}-{index}",
                    )
                    risk, command = kernel.submit_open_intent(intent, quote, broker, now=now)
                    return "saved" if risk.approved and command is not None else "risk_rejected"
                except PermissionError as exc:
                    return type(exc).__name__ + ":" + str(exc)
                finally:
                    store.close()

            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = tuple(pool.map(submit, (1, 2)))
            self.assertEqual(outcomes.count("saved"), 1)
            self.assertEqual(
                sum("atomic notional reservation budget exceeded" in item for item in outcomes),
                1,
            )


if __name__ == "__main__":
    unittest.main()
