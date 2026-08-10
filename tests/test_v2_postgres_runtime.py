from __future__ import annotations

import os
import unittest
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal

from etoro_agent.codec_v2 import decode_dataclass
from etoro_agent.domain_v2 import DomainEvent, QuoteProvenance
from etoro_agent.postgres_runtime_v2 import PostgresRuntimeStoreV2
from etoro_agent.postgres_store_v2 import psycopg_available


class V2CodecTests(unittest.TestCase):
    def test_domain_codec_restores_decimal_datetime_and_nested_types(self) -> None:
        now = datetime(2026, 8, 10, 12, tzinfo=timezone.utc)
        quote = QuoteProvenance(
            "AAPL", Decimal("99.9"), Decimal("100"), now, now,
            "test", "1", "m" * 64, "b" * 64,
        )
        restored = decode_dataclass(QuoteProvenance, asdict(quote))
        self.assertEqual(restored, quote)
        self.assertIsInstance(restored.bid, Decimal)
        self.assertEqual(restored.quote_observed_at.tzinfo, timezone.utc)


@unittest.skipUnless(bool(os.getenv("ETORO_TEST_POSTGRES_DSN")) and psycopg_available(), "optional v2 PostgreSQL integration DSN absent")
class V2PostgresRuntimeIntegrationTests(unittest.TestCase):
    def test_migration_fail_closed_state_and_hash_chain(self) -> None:
        store = PostgresRuntimeStoreV2.from_dsn(os.environ["ETORO_TEST_POSTGRES_DSN"])
        try:
            store.migrate()
            self.assertEqual(store.state_get("trading_state", "missing"), "LOCKED")
            now = datetime.now(timezone.utc)
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


if __name__ == "__main__":
    unittest.main()
