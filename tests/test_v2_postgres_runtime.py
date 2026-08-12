from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Barrier
from unittest.mock import Mock, patch
from uuid import uuid4

from etoro_agent.ai_store_postgres_v2 import CanonicalPostgresAIStoreV2
from etoro_agent.ai_v2 import AIRole, DecisionPacketV2
from etoro_agent.broker_truth_v2 import broker_truth_v2
from etoro_agent.codec_v2 import decode_dataclass
from etoro_agent.config_v2 import load_config_v2
from etoro_agent.decision_apply_service_v2 import DecisionApplyWorkerV2
from etoro_agent.domain_v2 import (
    AuditIntegrityError,
    DomainEvent,
    Fill,
    IntentEnvelope,
    OrderStatus,
    QuoteProvenance,
    Side,
)
from etoro_agent.etoro_api_current_v2 import BrokerAccountSnapshotV2
from etoro_agent.kernel_v2 import UnifiedTradingKernel
from etoro_agent.postgres_runtime_v2 import PostgresRuntimeStoreV2
from etoro_agent.postgres_store_impl_v2 import (
    AI_SCHEMA_PATH,
    SCHEMA_PATH,
    SCHEMA_VERSION,
    ZERO_HASH,
)
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
        import psycopg

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
                store.market_heartbeat(
                    "synchronizing",
                    {"real_money": False, "transport_connected": True},
                    at=now,
                )
                with store.connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT service,status FROM v2_service_heartbeats WHERE service='v2-market'"
                    )
                    self.assertEqual(tuple(cursor.fetchone()), ("v2-market", "synchronizing"))
                store.market_heartbeat("healthy", {"real_money": False}, at=now)
                with self.assertRaises(psycopg.errors.RaiseException):
                    store.market_heartbeat("invented", {"real_money": False}, at=now)
            finally:
                store.close()

    def test_exact_login_roles_own_only_their_service_heartbeat(self) -> None:
        import psycopg
        from psycopg import sql
        from psycopg.conninfo import conninfo_to_dict, make_conninfo

        service_by_role = {
            "etoro-candidate": "v2-coordinator",
            "etoro-ai": "v2-role-apply",
            "etoro-decision": "v2-decision-shadow",
            "etoro-decision-exec": "v2-decision-apply",
            "etoro-exit": "v2-exit-manager",
            "etoro-reconciler": "v2-reconciliation",
            "etoro-executor": "v2-demo-executor",
        }
        supporting_roles = ("etoro-engine", "etoro-control", "etoro-observer", "etoro-collector")
        roles = (*service_by_role, *supporting_roles)
        password = uuid4().hex
        base_dsn = os.environ["ETORO_TEST_POSTGRES_DSN"]
        admin = psycopg.connect(base_dsn, autocommit=True)
        try:
            for role in roles:
                admin.execute(
                    sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                        sql.Identifier(role), sql.Literal(password)
                    )
                )
        finally:
            admin.close()
        try:
            with self._temporary_database() as dsn:
                bootstrap = PostgresRuntimeStoreV2.from_dsn(dsn)
                try:
                    bootstrap.migrate()
                finally:
                    bootstrap.close()
                database_name = conninfo_to_dict(dsn)["dbname"]
                grants = (
                    Path(__file__).resolve().parents[1] / "ops/postgres/grants_v2.sql"
                ).read_text(encoding="utf-8")
                grants = grants.replace("etoro_v2", database_name)
                database = psycopg.connect(dsn, autocommit=True)
                try:
                    database.execute(grants, prepare=False)
                finally:
                    database.close()
                base_parameters = conninfo_to_dict(dsn)

                def restricted_dsn(role: str) -> str:
                    return make_conninfo(
                        **{
                            **base_parameters,
                            "user": role,
                            "password": password,
                        }
                    )

                now = datetime.now(UTC)
                for role, owned_service in service_by_role.items():
                    store = PostgresRuntimeStoreV2.from_dsn(restricted_dsn(role))
                    try:
                        store.require_schema()
                        store.heartbeat(owned_service, "starting", {"role": role}, at=now)
                        store.heartbeat(owned_service, "healthy", {"role": role}, at=now)
                        with store.connection.cursor() as cursor:
                            cursor.execute("SELECT session_user")
                            self.assertEqual(cursor.fetchone()[0], role)
                            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                                cursor.execute(
                                    "INSERT INTO v2_service_heartbeats VALUES(%s,%s,%s,now())",
                                    (owned_service, "spoof", "{}"),
                                )
                        spoofed_service = next(
                            service
                            for service in service_by_role.values()
                            if service != owned_service
                        )
                        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                            store.heartbeat(spoofed_service, "spoof", {"role": role}, at=now)
                    finally:
                        store.close()

                verify = psycopg.connect(dsn, autocommit=True)
                try:
                    for role, owned_service in service_by_role.items():
                        row = verify.execute(
                            """SELECT COUNT(*),MIN(status),MIN(details->>'role')
                               FROM v2_service_heartbeats WHERE service=%s""",
                            (owned_service,),
                        ).fetchone()
                        self.assertEqual(tuple(row or ()), (1, "healthy", role))
                finally:
                    verify.close()

                collector = PostgresRuntimeStoreV2.from_dsn(restricted_dsn("etoro-collector"))
                try:
                    collector.require_schema()
                    collector.market_heartbeat("healthy", {"real_money": False}, at=now)
                    with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                        collector.connection.execute(
                            "INSERT INTO v2_service_heartbeats VALUES('v2-market','spoof','{}',now())"
                        )
                finally:
                    collector.close()

                observer = psycopg.connect(restricted_dsn("etoro-observer"), autocommit=True)
                try:
                    self.assertGreater(
                        observer.execute("SELECT COUNT(*) FROM v2_service_heartbeats").fetchone()[
                            0
                        ],
                        0,
                    )
                    with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                        observer.execute(
                            "INSERT INTO v2_service_heartbeats VALUES('observer','spoof','{}',now())"
                        )
                finally:
                    observer.close()
        finally:
            admin = psycopg.connect(base_dsn, autocommit=True)
            try:
                for role in reversed(roles):
                    admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))
            finally:
                admin.close()

    def test_failed_candidate_marker_rollback_keeps_exact_old_runtime_usable(self) -> None:
        import psycopg
        from psycopg import sql

        repository = Path(__file__).resolve().parents[1]
        baseline_sha = "2872f0e6e19298520092fa22a7c98dbb3cb90c6c"
        with tempfile.TemporaryDirectory() as folder:
            old_checkout = Path(folder) / "baseline"
            old_checkout.mkdir()
            archive = subprocess.run(
                ["git", "-C", str(repository), "archive", baseline_sha],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["tar", "-xf", "-", "-C", str(old_checkout)],
                input=archive.stdout,
                check=True,
            )

            def old_runtime_probe(
                dsn: str,
                *,
                migrate: bool = False,
                economic_role: str | None = None,
            ) -> None:
                probe = (
                    "from etoro_agent.postgres_runtime_v2 import PostgresRuntimeStoreV2\n"
                    "from etoro_agent.domain_v2 import (\n"
                    "    BrokerOrder,DomainEvent,ExitReason,Fill,OrderStatus,\n"
                    "    PositionState,PositionStatus,Side\n"
                    ")\n"
                    "from dataclasses import asdict\n"
                    "from datetime import UTC,datetime,timedelta\n"
                    "from decimal import Decimal\n"
                    "from psycopg import sql\n"
                    "import os\n"
                    "store=PostgresRuntimeStoreV2.from_dsn(os.environ['PROBE_DSN'])\n"
                    "try:\n"
                    f"    {'store.migrate()' if migrate else 'store.require_schema()'}\n"
                    "    store.require_schema()\n"
                    "    assert store.state_get('trading_state', 'missing') == 'LOCKED'\n"
                    "    assert store.positions('master_1000', open_only=True) == ()\n"
                    "    role=os.environ.get('PROBE_ECONOMIC_ROLE')\n"
                    "    if role:\n"
                    "        store.connection.execute(sql.SQL('SET ROLE {}').format(sql.Identifier(role)))\n"
                    "        now=datetime.now(UTC)\n"
                    "        position=PositionState(\n"
                    "            'legacy-position','master_1000','legacy-strategy','legacy-lane',\n"
                    "            'v2','legacy-intent','AAPL',Side.BUY,Decimal('0'),\n"
                    "            Decimal('100'),now-timedelta(hours=1),now-timedelta(hours=1),\n"
                    "            Decimal('95'),Decimal('110'),Decimal('0.05'),Decimal('0.10'),\n"
                    "            3600,now,realized_pnl=Decimal('10'),\n"
                    "            status=PositionStatus.CLOSED,exit_reason=ExitReason.AGENT_CLOSE,\n"
                    "            broker_position_id='legacy-broker-position',last_mark=Decimal('110')\n"
                    "        )\n"
                    "        fill=Fill(\n"
                    "            'legacy-close-fill','legacy-close-command','legacy-client-order',\n"
                    "            'legacy-broker-order','legacy-broker-position','AAPL',Side.SELL,\n"
                    "            Decimal('1'),Decimal('110'),Decimal('0'),Decimal('0'),now,now,\n"
                    "            'legacy-close-fill-idempotency'\n"
                    "        )\n"
                    "        order=BrokerOrder(\n"
                    "            'legacy-close-command','legacy-client-order',OrderStatus.FILLED,\n"
                    "            submitted_at=now,acknowledged_at=now,\n"
                    "            broker_order_id='legacy-broker-order',\n"
                    "            broker_position_id='legacy-broker-position',\n"
                    "            filled_quantity=Decimal('1'),average_fill_price=Decimal('110'),\n"
                    "            last_update_at=now\n"
                    "        )\n"
                    "        fill_event=DomainEvent(\n"
                    "            'legacy-close-fill-event','OrderFilled',6,now,now,\n"
                    "            'legacy-close-fill-event','legacy-close-command','legacy-close',\n"
                    "            {'fill_id':fill.fill_id}\n"
                    "        )\n"
                    "        position_event=DomainEvent(\n"
                    "            'legacy-position-closed-event','PositionClosed',6,now,now,\n"
                    "            'legacy-position-closed-event',fill.fill_id,'legacy-close',\n"
                    "            {'position':asdict(position),'realized_delta_usd':'10'}\n"
                    "        )\n"
                    "        assert store.save_fill_position_bundle(\n"
                    "            fill,order,position,fill_event,position_event\n"
                    "        )\n"
                    "finally:\n"
                    "    store.close()\n"
                )
                subprocess.run(
                    [sys.executable, "-c", probe],
                    cwd=old_checkout,
                    env={
                        **os.environ,
                        "PYTHONPATH": str(old_checkout / "src"),
                        "PROBE_DSN": dsn,
                        **(
                            {"PROBE_ECONOMIC_ROLE": economic_role}
                            if economic_role is not None
                            else {}
                        ),
                    },
                    text=True,
                    capture_output=True,
                    check=True,
                )

            with self._temporary_database() as dsn:
                old_runtime_probe(dsn, migrate=True)
                current = PostgresRuntimeStoreV2.from_dsn(dsn)
                role = "etoro-engine-rollback-" + uuid4().hex[:8]
                try:
                    current.migrate()
                    with current.connection.cursor() as cursor:
                        cursor.execute("SELECT value FROM v2_meta WHERE key='schema_version'")
                        self.assertEqual(tuple(cursor.fetchone() or ()), (str(SCHEMA_VERSION),))
                        cursor.execute(
                            "SELECT count(*) FROM v2_schema_migrations WHERE version=%s",
                            (SCHEMA_VERSION,),
                        )
                        self.assertEqual(tuple(cursor.fetchone() or ()), (1,))

                    admin = psycopg.connect(dsn, autocommit=True)
                    try:
                        admin.execute(
                            sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role))
                        )
                        admin.execute(
                            sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(
                                sql.Identifier(role)
                            )
                        )
                        admin.execute(
                            sql.SQL(
                                "GRANT SELECT ON v2_meta,v2_schema_migrations,v2_trading_state,"
                                "v2_intents,v2_order_commands,v2_broker_orders,"
                                "v2_risk_reservations,v2_fills,v2_positions,v2_events TO {}"
                            ).format(sql.Identifier(role))
                        )
                        admin.execute(
                            sql.SQL(
                                "GRANT INSERT ON v2_fills,v2_events TO {}; "
                                "GRANT INSERT,UPDATE ON v2_positions TO {}; "
                                "GRANT UPDATE ON v2_broker_orders,v2_risk_reservations TO {}; "
                                "GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO {}"
                            ).format(
                                sql.Identifier(role),
                                sql.Identifier(role),
                                sql.Identifier(role),
                                sql.Identifier(role),
                            )
                        )
                        now = datetime.now(UTC)
                        admin.execute(
                            """INSERT INTO v2_intents(
                                   intent_id,portfolio_id,lane_id,strategy_id,state,envelope,
                                   envelope_hash,created_at,expires_at,updated_at
                               ) VALUES(
                                   'legacy-intent','master_1000','legacy-lane','legacy-strategy',
                                   'CONSUMED','{}'::jsonb,%s,%s,%s,%s
                               )""",
                            ("a" * 64, now, now + timedelta(hours=1), now),
                        )
                        admin.execute(
                            """INSERT INTO v2_order_commands(
                                   order_command_id,intent_id,proposal_id,client_order_id,
                                   portfolio_id,symbol,reduce_only,idempotency_key,command,
                                   command_hash,created_at,expires_at
                               ) VALUES(
                                   'legacy-close-command','legacy-intent','legacy-proposal',
                                   '00000000-0000-4000-8000-000000000001','master_1000',
                                   'AAPL',TRUE,'legacy-close-command','{}'::jsonb,%s,%s,%s
                               )""",
                            ("b" * 64, now, now + timedelta(hours=1)),
                        )
                        admin.execute(
                            """INSERT INTO v2_broker_orders(
                                   order_command_id,status,broker_order_id,broker_position_id,
                                   filled_quantity,state,updated_at
                               ) VALUES(
                                   'legacy-close-command','ACKNOWLEDGED','legacy-broker-order',
                                   'legacy-broker-position',0,'{}'::jsonb,%s
                               )""",
                            (now,),
                        )
                        admin.execute(
                            """INSERT INTO v2_risk_reservations(
                                   order_command_id,reserved_notional_usd,reserved_loss_usd,
                                   state,created_at
                               ) VALUES('legacy-close-command',100,10,'ACTIVE',%s)""",
                            (now,),
                        )

                        # Candidate restart failed: restore only the compatibility marker.
                        with admin.transaction():
                            admin.execute(
                                "UPDATE v2_meta SET value='6',updated_at=now() "
                                "WHERE key='schema_version'"
                            )
                        old_runtime_probe(dsn, economic_role=role)

                        # Representative old coordinator reads and an old-role SQL action work.
                        with admin.transaction():
                            admin.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))
                            self.assertEqual(
                                tuple(
                                    admin.execute(
                                        "SELECT state FROM v2_trading_state WHERE singleton=TRUE"
                                    ).fetchone()
                                    or ()
                                ),
                                ("LOCKED",),
                            )
                            self.assertEqual(
                                tuple(
                                    admin.execute(
                                        "SELECT status FROM v2_positions "
                                        "WHERE position_id='legacy-position'"
                                    ).fetchone()
                                    or ()
                                ),
                                ("CLOSED",),
                            )
                            self.assertEqual(
                                tuple(
                                    admin.execute(
                                        "SELECT count(*) FROM v2_fills "
                                        "WHERE fill_id='legacy-close-fill'"
                                    ).fetchone()
                                    or ()
                                ),
                                (1,),
                            )
                            self.assertEqual(
                                tuple(
                                    admin.execute(
                                        "SELECT count(*) FROM v2_events "
                                        "WHERE event_id='legacy-position-closed-event'"
                                    ).fetchone()
                                    or ()
                                ),
                                (1,),
                            )

                        # The additive candidate migration remains; forward retry
                        # reasserts the current candidate marker.
                        current.migrate()
                        current.require_schema()
                        self.assertEqual(current.state_get("schema_version"), str(SCHEMA_VERSION))
                        admin.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
                        try:
                            with self.assertRaises(psycopg.errors.RaiseException):
                                admin.execute(
                                    """INSERT INTO v2_events(
                                           event_id,event_type,schema_version,event_time,
                                           processing_time,idempotency_key,causation_id,
                                           correlation_id,payload,canonical_body,
                                           canonical_body_hash,previous_hash,event_hash
                                       ) VALUES(
                                           'legacy-event-after-forward','PositionClosed',6,
                                           now(),now(),'legacy-event-after-forward',
                                           'legacy-close-fill','legacy-close',
                                           '{"position":{"position_id":"legacy-position"},
                                           "realized_delta_usd":"10"}'::jsonb,'{}',%s,%s,%s
                                       )""",
                                    ("c" * 64, "d" * 64, "e" * 64),
                                )
                        finally:
                            admin.execute("RESET ROLE")
                    finally:
                        try:
                            admin.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
                            admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
                        finally:
                            admin.close()
                finally:
                    current.close()

    def test_service_grants_preserve_economic_owner_and_audit_fail_safe(self) -> None:
        import psycopg
        from psycopg import sql

        role_bases = (
            "etoro-engine",
            "etoro-candidate",
            "etoro-ai",
            "etoro-decision",
            "etoro-decision-exec",
            "etoro-exit",
            "etoro-reconciler",
            "etoro-control",
            "etoro-executor",
            "etoro-observer",
            "etoro-collector",
        )
        suffix = uuid4().hex[:8]
        roles = {name: f"{name}-{suffix}" for name in role_bases}
        base_dsn = os.environ["ETORO_TEST_POSTGRES_DSN"]
        admin = psycopg.connect(base_dsn, autocommit=True)
        try:
            for role in roles.values():
                admin.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role)))
            with self._temporary_database() as dsn:
                bootstrap = PostgresRuntimeStoreV2.from_dsn(dsn)
                try:
                    bootstrap.migrate()
                finally:
                    bootstrap.close()
                database_name = psycopg.conninfo.conninfo_to_dict(dsn)["dbname"]
                grants = (
                    Path(__file__).resolve().parents[1] / "ops/postgres/grants_v2.sql"
                ).read_text(encoding="utf-8")
                grants = grants.replace("etoro_v2", database_name)
                for base, role in roles.items():
                    grants = grants.replace(f'"{base}"', f'"{role}"')
                database = psycopg.connect(dsn, autocommit=True)
                try:
                    database.execute(grants, prepare=False)
                    for role_key in (
                        "etoro-decision",
                        "etoro-decision-exec",
                        "etoro-exit",
                        "etoro-executor",
                    ):
                        for table in ("v2_positions", "v2_reconciliation_cases", "v2_fills"):
                            for privilege in ("INSERT", "UPDATE"):
                                allowed = database.execute(
                                    "SELECT has_table_privilege(%s,%s,%s)",
                                    (roles[role_key], table, privilege),
                                ).fetchone()
                                self.assertEqual(tuple(allowed or ()), (False,))
                    for table in ("v2_positions", "v2_reconciliation_cases"):
                        for privilege in ("INSERT", "UPDATE"):
                            allowed = database.execute(
                                "SELECT has_table_privilege(%s,%s,%s)",
                                (roles["etoro-reconciler"], table, privilege),
                            ).fetchone()
                            self.assertEqual(tuple(allowed or ()), (True,))
                    self.assertEqual(
                        tuple(
                            database.execute(
                                "SELECT has_table_privilege(%s,'v2_fills','INSERT')",
                                (roles["etoro-reconciler"],),
                            ).fetchone()
                            or ()
                        ),
                        (True,),
                    )

                    for role_key in (
                        "etoro-decision",
                        "etoro-decision-exec",
                        "etoro-exit",
                        "etoro-reconciler",
                        "etoro-control",
                        "etoro-executor",
                    ):
                        direct_state = database.execute(
                            "SELECT has_table_privilege(%s,'v2_trading_state','UPDATE')",
                            (roles[role_key],),
                        ).fetchone()
                        direct_peak = database.execute(
                            "SELECT has_table_privilege(%s,'v2_meta','UPDATE')",
                            (roles[role_key],),
                        ).fetchone()
                        self.assertEqual(tuple(direct_state or ()), (False,))
                        self.assertEqual(tuple(direct_peak or ()), (False,))

                    for role_key in ("etoro-decision-exec", "etoro-exit", "etoro-executor"):
                        peak_function = database.execute(
                            "SELECT has_function_privilege(%s,"
                            "'v2_update_peak_equity(numeric)','EXECUTE')",
                            (roles[role_key],),
                        ).fetchone()
                        self.assertEqual(tuple(peak_function or ()), (True,))
                    shadow_peak = database.execute(
                        "SELECT has_function_privilege(%s,"
                        "'v2_update_peak_equity(numeric)','EXECUTE')",
                        (roles["etoro-decision"],),
                    ).fetchone()
                    self.assertEqual(tuple(shadow_peak or ()), (False,))

                    for table in (
                        "v2_intents",
                        "v2_decisions",
                        "v2_order_commands",
                        "v2_broker_orders",
                        "v2_risk_reservations",
                        "v2_outbox",
                        "v2_events",
                    ):
                        shadow_insert = database.execute(
                            "SELECT has_table_privilege(%s,%s,'INSERT')",
                            (roles["etoro-decision"], table),
                        ).fetchone()
                        self.assertEqual(tuple(shadow_insert or ()), (False,))
                    for table in ("v2_intents", "v2_order_commands", "v2_outbox", "v2_events"):
                        execution_insert = database.execute(
                            "SELECT has_table_privilege(%s,%s,'INSERT')",
                            (roles["etoro-decision-exec"], table),
                        ).fetchone()
                        self.assertEqual(tuple(execution_insert or ()), (True,))

                    exit_fill_read = database.execute(
                        "SELECT has_table_privilege(%s,'v2_fills','SELECT')",
                        (roles["etoro-exit"],),
                    ).fetchone()
                    self.assertEqual(tuple(exit_fill_read or ()), (True,))

                    for role_key, allowed in (
                        ("etoro-candidate", True),
                        ("etoro-ai", True),
                        ("etoro-reconciler", True),
                        ("etoro-observer", False),
                    ):
                        meta_function = database.execute(
                            "SELECT has_function_privilege(%s,"
                            "'v2_set_runtime_meta(text,text,timestamp with time zone)',"
                            "'EXECUTE')",
                            (roles[role_key],),
                        ).fetchone()
                        self.assertEqual(tuple(meta_function or ()), (allowed,))

                    restricted_candidate = psycopg.connect(dsn, autocommit=True)
                    restricted_candidate.execute(
                        sql.SQL("SET ROLE {}").format(sql.Identifier(roles["etoro-candidate"]))
                    )
                    candidate_store = PostgresRuntimeStoreV2(restricted_candidate)
                    try:
                        marker_key = "v2_coordinator_bar:entry_review:aapl"
                        marker_value = "f" * 64
                        candidate_store.state_set(marker_key, marker_value)
                        self.assertEqual(
                            candidate_store.state_get(marker_key, "missing"),
                            marker_value,
                        )
                        for forbidden_key in (
                            "last_coordinated_bar:shadow:1",
                            "v2_coordinator_bar:invalid:aapl",
                            "latest_critic_v2:tampered",
                        ):
                            with self.assertRaises(psycopg.errors.RaiseException):
                                candidate_store.state_set(forbidden_key, "tampered")
                    finally:
                        candidate_store.close()

                    restricted_ai = psycopg.connect(dsn, autocommit=True)
                    restricted_ai.execute(
                        sql.SQL("SET ROLE {}").format(sql.Identifier(roles["etoro-ai"]))
                    )
                    try:
                        restricted_ai.execute(
                            "SELECT v2_set_runtime_meta(%s,%s,now())",
                            ("latest_critic_v2:sol_critic", "{}"),
                        )
                        with self.assertRaises(psycopg.errors.RaiseException):
                            restricted_ai.execute(
                                "SELECT v2_set_runtime_meta(%s,%s,now())",
                                ("last_coordinated_bar:shadow:1", "tampered"),
                            )
                    finally:
                        restricted_ai.close()

                    now = datetime.now(UTC)
                    exit_connection = psycopg.connect(dsn, autocommit=True)
                    exit_connection.execute(
                        sql.SQL("SET ROLE {}").format(sql.Identifier(roles["etoro-exit"]))
                    )
                    exit_store = PostgresRuntimeStoreV2(exit_connection)
                    try:
                        truth = broker_truth_v2(
                            exit_store,
                            Mock(),
                            config=load_config_v2("config/v2-demo-execution.json"),
                            now=now,
                            snapshot=BrokerAccountSnapshotV2(
                                "test-v1",
                                "exit-truth-request",
                                "e" * 64,
                                now,
                                now,
                                now,
                                now,
                                Decimal("1000"),
                                Decimal("1000"),
                                Decimal("0"),
                                Decimal("0"),
                                Decimal("1000"),
                                Decimal("0"),
                                Decimal("0"),
                                Decimal("0"),
                                (),
                                (),
                                (),
                                (),
                            ),
                        )
                        self.assertTrue(truth.reconciliation_ok)
                        self.assertEqual(truth.peak_equity_usd, Decimal("1000"))
                    finally:
                        exit_store.close()

                    config = load_config_v2("config/v2-demo-execution.json")
                    restricted_control = psycopg.connect(dsn, autocommit=True)
                    restricted_control.execute(
                        sql.SQL("SET ROLE {}").format(sql.Identifier(roles["etoro-control"]))
                    )
                    control_store = PostgresRuntimeStoreV2(restricted_control)
                    try:
                        control_store.require_schema()
                        self.assertEqual(
                            control_store.lock_and_invalidate_unstarted(
                                actor="etoro-control",
                                reason="execution gate removed",
                                at=datetime.now(UTC),
                            ),
                            0,
                        )
                    finally:
                        control_store.close()

                    for role_key in ("etoro-decision-exec", "etoro-exit"):
                        setup = PostgresRuntimeStoreV2.from_dsn(dsn)
                        order_id = f"gate-{role_key}-{suffix}"
                        now = datetime.now(UTC)
                        try:
                            setup.set_trading_state(
                                "ACTIVE", actor="integration-setup", reason=role_key
                            )
                            kernel = UnifiedTradingKernel(setup, GlobalRiskKernel(config.mandate))
                            intent = IntentEnvelope(
                                f"intent-{order_id}",
                                "master_1000",
                                "A_deterministic",
                                "grant-integration",
                                "v2",
                                "AAPL",
                                Side.BUY,
                                Decimal("50"),
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
                                f"market-{order_id}",
                                correlation_id=order_id,
                            )
                            quote = QuoteProvenance(
                                "AAPL",
                                Decimal("99.9"),
                                Decimal("100"),
                                now,
                                now,
                                "grant-integration",
                                order_id,
                                f"market-{order_id}",
                                f"broker-{order_id}",
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
                                f"broker-{order_id}",
                                now,
                            )
                            approved, command = kernel.submit_open_intent(
                                intent, quote, broker, now=now
                            )
                            self.assertTrue(approved.approved)
                            self.assertIsNotNone(command)
                        finally:
                            setup.close()

                        restricted = psycopg.connect(dsn, autocommit=True)
                        restricted.execute(
                            sql.SQL("SET ROLE {}").format(sql.Identifier(roles[role_key]))
                        )
                        role_store = PostgresRuntimeStoreV2(restricted)
                        try:
                            self.assertEqual(
                                role_store.lock_and_invalidate_unstarted(
                                    actor=role_key,
                                    reason="execution gate removed",
                                    at=datetime.now(UTC),
                                ),
                                1,
                            )
                        finally:
                            role_store.close()
                        state = database.execute(
                            "SELECT state FROM v2_trading_state WHERE singleton=TRUE"
                        ).fetchone()
                        status = database.execute(
                            "SELECT status FROM v2_broker_orders WHERE order_command_id=%s",
                            (command.order_command_id,),
                        ).fetchone()
                        self.assertEqual(tuple(state or ()), ("LOCKED",))
                        self.assertEqual(tuple(status or ()), ("REJECTED",))

                    database.execute("SELECT v2_update_peak_equity(200)")
                    restricted_executor = psycopg.connect(dsn, autocommit=True)
                    restricted_executor.execute(
                        sql.SQL("SET ROLE {}").format(sql.Identifier(roles["etoro-executor"]))
                    )
                    try:
                        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                            restricted_executor.execute(
                                "UPDATE v2_meta SET value='1' WHERE key=%s",
                                ("broker_peak_" + "equity_v2",),
                            )
                        restricted_executor.rollback()
                        peak = restricted_executor.execute(
                            "SELECT v2_update_peak_equity(100)"
                        ).fetchone()
                        self.assertEqual(tuple(peak or ()), (Decimal("1000"),))
                        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                            restricted_executor.execute(
                                "UPDATE v2_trading_state SET state='ACTIVE' WHERE singleton=TRUE"
                            )
                        restricted_executor.rollback()
                        with self.assertRaises(psycopg.errors.RaiseException):
                            restricted_executor.execute(
                                "SELECT * FROM v2_transition_trading_state"
                                "('ACTIVE','executor','forbidden',now())"
                            )
                    finally:
                        restricted_executor.close()

                    shadow_packet_id = f"shadow-decision-{suffix}"
                    shadow_time = datetime.now(UTC)
                    shadow_admin_queue = CanonicalPostgresAIStoreV2(
                        PostgresRuntimeStoreV2(database)
                    )
                    self.assertTrue(
                        shadow_admin_queue.queue(
                            DecisionPacketV2(
                                shadow_packet_id,
                                shadow_time.isoformat(),
                                (shadow_time + timedelta(minutes=10)).isoformat(),
                                "C_sol_direct",
                                "ENTRY_REVIEW",
                                ("market",),
                                "feature",
                                "b" * 64,
                                "r" * 64,
                                {},
                                (),
                                None,
                                ("evidence",),
                            ),
                            AIRole.PORTFOLIO_DECIDER,
                        )
                    )
                    database.execute(
                        """UPDATE v2_ai_packets SET state='DECIDED',output='{}'::jsonb,
                           model='test',prompt_hash=%s,updated_at=%s WHERE packet_id=%s""",
                        ("s" * 64, shadow_time, shadow_packet_id),
                    )
                    shadow_connection = psycopg.connect(dsn, autocommit=True)
                    shadow_connection.execute(
                        sql.SQL("SET ROLE {}").format(sql.Identifier(roles["etoro-decision"]))
                    )
                    shadow_store = PostgresRuntimeStoreV2(shadow_connection)
                    shadow_queue = CanonicalPostgresAIStoreV2(shadow_store)
                    try:
                        shadow_claim = shadow_queue.claim_decided(
                            "shadow-role-worker",
                            AIRole.PORTFOLIO_DECIDER,
                            now=shadow_time + timedelta(seconds=1),
                            authority_mode="SHADOW",
                            execution_epoch=None,
                        )
                        self.assertIsNotNone(shadow_claim)
                        assert shadow_claim is not None
                        shadow_queue.mark_applied(
                            shadow_packet_id,
                            str(shadow_claim["apply_claim_token"]),
                            {"status": "shadow_only", "broker_write": False},
                            now=shadow_time + timedelta(seconds=2),
                        )
                        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                            shadow_store.append_event(
                                DomainEvent(
                                    event_id="forged-shadow-economic",
                                    event_type="OrderCommandCreated",
                                    schema_version=8,
                                    event_time=shadow_time,
                                    processing_time=shadow_time,
                                    idempotency_key="forged-shadow-economic",
                                    causation_id=shadow_packet_id,
                                    correlation_id=shadow_packet_id,
                                    payload={"broker_write": True},
                                )
                            )
                    finally:
                        shadow_store.close()
                    shadow_state = database.execute(
                        "SELECT state,applied_effect->>'broker_write' FROM v2_ai_packets "
                        "WHERE packet_id=%s",
                        (shadow_packet_id,),
                    ).fetchone()
                    self.assertEqual(tuple(shadow_state or ()), ("APPLIED", "false"))

                    protected_before = database.execute(
                        """SELECT
                           (SELECT COUNT(*) FROM v2_positions),
                           (SELECT COUNT(*) FROM v2_fills),
                           (SELECT COUNT(*) FROM v2_pnl_daily),
                           (SELECT COUNT(*) FROM v2_events
                            WHERE event_type IN ('PositionClosed','PositionReduced')),
                           (SELECT COALESCE(SUM(reserved_loss_usd),0)
                            FROM v2_risk_reservations WHERE state='ACTIVE')"""
                    ).fetchone()
                    for role_key in ("etoro-candidate", "etoro-ai"):
                        restricted = psycopg.connect(dsn, autocommit=True)
                        restricted.execute(
                            sql.SQL("SET ROLE {}").format(sql.Identifier(roles[role_key]))
                        )
                        role_store = PostgresRuntimeStoreV2(restricted)
                        try:
                            for event_type in ("PositionClosed", "PositionReduced"):
                                forged = DomainEvent(
                                    event_id=f"forged-{role_key}-{event_type}",
                                    event_type=event_type,
                                    schema_version=8,
                                    event_time=datetime.now(UTC),
                                    processing_time=datetime.now(UTC),
                                    idempotency_key=f"forged-{role_key}-{event_type}",
                                    causation_id="",
                                    correlation_id=role_key,
                                    payload={
                                        "position_id": "forged",
                                        "realized_pnl_usd": "999999",
                                    },
                                )
                                with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                                    role_store.append_event(forged)
                        finally:
                            role_store.close()
                        self.assertEqual(
                            tuple(
                                database.execute(
                                    "SELECT has_table_privilege(%s,'v2_events','INSERT')",
                                    (roles[role_key],),
                                ).fetchone()
                                or ()
                            ),
                            (False,),
                        )
                    self.assertEqual(
                        tuple(
                            database.execute(
                                "SELECT has_function_privilege(%s,"
                                "'v2_append_ai_telemetry_event(text,text,integer,"
                                "timestamp with time zone,timestamp with time zone,text,text,"
                                "text,jsonb,text)','EXECUTE')",
                                (roles["etoro-candidate"],),
                            ).fetchone()
                            or ()
                        ),
                        (False,),
                    )
                    self.assertEqual(
                        tuple(
                            database.execute(
                                "SELECT has_function_privilege(%s,"
                                "'v2_append_ai_telemetry_event(text,text,integer,"
                                "timestamp with time zone,timestamp with time zone,text,text,"
                                "text,jsonb,text)','EXECUTE')",
                                (roles["etoro-ai"],),
                            ).fetchone()
                            or ()
                        ),
                        (True,),
                    )
                    for role_key in ("etoro-decision", "etoro-decision-exec"):
                        telemetry_function = database.execute(
                            "SELECT has_function_privilege(%s,"
                            "'v2_append_ai_telemetry_event(text,text,integer,"
                            "timestamp with time zone,timestamp with time zone,text,text,"
                            "text,jsonb,text)','EXECUTE')",
                            (roles[role_key],),
                        ).fetchone()
                        self.assertEqual(tuple(telemetry_function or ()), (True,))
                    for role_key in ("etoro-decision", "etoro-decision-exec"):
                        authority_function = database.execute(
                            "SELECT has_function_privilege(%s,'v2_lock_ai_authority()','EXECUTE')",
                            (roles[role_key],),
                        ).fetchone()
                        self.assertEqual(tuple(authority_function or ()), (True,))
                    restricted_decision = psycopg.connect(dsn, autocommit=True)
                    restricted_decision.execute(
                        sql.SQL("SET ROLE {}").format(sql.Identifier(roles["etoro-decision-exec"]))
                    )
                    decision_store = PostgresRuntimeStoreV2(restricted_decision)
                    forged_time = datetime.now(UTC)
                    try:
                        with self.assertRaises(psycopg.errors.RaiseException):
                            decision_store.append_event(
                                DomainEvent(
                                    event_id="forged-decision-position-close",
                                    event_type="PositionClosed",
                                    schema_version=8,
                                    event_time=forged_time,
                                    processing_time=forged_time,
                                    idempotency_key="forged-decision-position-close",
                                    causation_id="forged-fill",
                                    correlation_id="forged-position",
                                    payload={
                                        "position": {"position_id": "forged-position"},
                                        "realized_delta_usd": "999999",
                                    },
                                )
                            )
                    finally:
                        decision_store.close()
                    protected_after = database.execute(
                        """SELECT
                           (SELECT COUNT(*) FROM v2_positions),
                           (SELECT COUNT(*) FROM v2_fills),
                           (SELECT COUNT(*) FROM v2_pnl_daily),
                           (SELECT COUNT(*) FROM v2_events
                            WHERE event_type IN ('PositionClosed','PositionReduced')),
                           (SELECT COALESCE(SUM(reserved_loss_usd),0)
                            FROM v2_risk_reservations WHERE state='ACTIVE')"""
                    ).fetchone()
                    self.assertEqual(protected_after, protected_before)

                    packet_id = f"role-ai-poison-{suffix}"
                    packet_time = datetime.now(UTC)
                    admin_queue = CanonicalPostgresAIStoreV2(PostgresRuntimeStoreV2(database))
                    self.assertTrue(
                        admin_queue.queue(
                            DecisionPacketV2(
                                packet_id,
                                packet_time.isoformat(),
                                (packet_time + timedelta(minutes=10)).isoformat(),
                                "C_sol_direct",
                                "ENTRY_REVIEW",
                                ("market",),
                                "feature",
                                "b" * 64,
                                "r" * 64,
                                {},
                                (),
                                None,
                                ("evidence",),
                            ),
                            AIRole.PORTFOLIO_DECIDER,
                        )
                    )
                    restricted_ai = psycopg.connect(dsn, autocommit=True)
                    restricted_ai.execute(
                        sql.SQL("SET ROLE {}").format(sql.Identifier(roles["etoro-ai"]))
                    )
                    role_store = PostgresRuntimeStoreV2(restricted_ai)
                    role_queue = CanonicalPostgresAIStoreV2(role_store)
                    try:
                        claim = role_queue.claim(
                            "role-ai-worker",
                            AIRole.PORTFOLIO_DECIDER,
                            now=packet_time + timedelta(seconds=1),
                            authority_mode="SHADOW",
                            execution_epoch=None,
                        )
                        self.assertIsNotNone(claim)
                        assert claim is not None
                        role_queue.fail(
                            packet_id,
                            str(claim["claim_token"]),
                            model="test",
                            prompt_hash="p" * 64,
                            run={
                                "run_id": f"run-{suffix}",
                                "status": "ERROR",
                                "latency_ms": 1,
                                "input_tokens": 1,
                                "output_tokens": 0,
                                "reasoning_tokens": 0,
                                "error_type": "ForgedEconomicEvent",
                            },
                            retryable=False,
                            now=packet_time + timedelta(seconds=2),
                        )
                        telemetry = restricted_ai.execute(
                            """SELECT event_type,payload->>'actor',payload->>'packet_id'
                               FROM v2_events WHERE idempotency_key=%s""",
                            (f"ai-dead-letter:{packet_id}:inference:1",),
                        ).fetchone()
                        self.assertEqual(
                            tuple(telemetry or ()),
                            ("AIPacketDeadLettered", roles["etoro-ai"], packet_id),
                        )
                        forged_telemetry = DomainEvent(
                            event_id="evt-" + "f" * 24,
                            event_type="PositionReduced",
                            schema_version=8,
                            event_time=packet_time + timedelta(seconds=2),
                            processing_time=packet_time + timedelta(seconds=2),
                            idempotency_key="forged-ai-economic-event",
                            causation_id=packet_id,
                            correlation_id=packet_id,
                            payload={"actor": roles["etoro-ai"], "packet_id": packet_id},
                        )
                        with self.assertRaises(psycopg.errors.RaiseException):
                            role_store.append_ai_telemetry_event(forged_telemetry)
                        missing_actor = DomainEvent(
                            event_id="evt-" + "e" * 24,
                            event_type="AIPacketDeadLettered",
                            schema_version=4,
                            event_time=packet_time + timedelta(seconds=2),
                            processing_time=packet_time + timedelta(seconds=2),
                            idempotency_key="forged-ai-missing-actor",
                            causation_id=packet_id,
                            correlation_id=packet_id,
                            payload={
                                "packet_id": packet_id,
                                "stage": "inference",
                                "reason": "inference_terminal:ForgedEconomicEvent",
                                "attempt": 1,
                            },
                        )
                        with self.assertRaises(psycopg.errors.RaiseException):
                            role_store.append_ai_telemetry_event(missing_actor)
                        event_key = f"ai-dead-letter:{packet_id}:inference:1"
                        conflicting = DomainEvent(
                            event_id="evt-" + hashlib.sha256(event_key.encode()).hexdigest()[:24],
                            event_type="AIPacketDeadLettered",
                            schema_version=4,
                            event_time=packet_time + timedelta(seconds=3),
                            processing_time=packet_time + timedelta(seconds=3),
                            idempotency_key=event_key,
                            causation_id=packet_id,
                            correlation_id=packet_id,
                            payload={
                                "actor": roles["etoro-ai"],
                                "packet_id": packet_id,
                                "stage": "inference",
                                "reason": "inference_terminal:ForgedEconomicEvent",
                                "attempt": 1,
                            },
                        )
                        with self.assertRaises(AuditIntegrityError):
                            role_store.append_ai_telemetry_event(conflicting)
                    finally:
                        role_store.close()
                    state = database.execute(
                        "SELECT state FROM v2_trading_state WHERE singleton=TRUE"
                    ).fetchone()
                    marker = database.execute(
                        "SELECT value FROM v2_meta WHERE key='audit_integrity_failure'"
                    ).fetchone()
                    self.assertEqual(tuple(state or ()), ("LOCKED",))
                    self.assertEqual(tuple(marker or ()), ("event_idempotency_conflict",))
                finally:
                    database.close()
        finally:
            for role in reversed(tuple(roles.values())):
                admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))
            admin.close()

    def test_peak_equity_is_atomic_and_monotonic_under_concurrency(self) -> None:
        with self._temporary_database() as dsn:
            bootstrap = PostgresRuntimeStoreV2.from_dsn(dsn)
            try:
                bootstrap.migrate()
                self.assertEqual(bootstrap.update_peak_equity(Decimal("200")), Decimal("200"))
            finally:
                bootstrap.close()
            barrier = Barrier(2)

            def update(value: str) -> Decimal:
                store = PostgresRuntimeStoreV2.from_dsn(dsn)
                try:
                    barrier.wait(timeout=5)
                    return store.update_peak_equity(Decimal(value))
                finally:
                    store.close()

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = tuple(pool.map(update, ("100", "300")))
            verifier = PostgresRuntimeStoreV2.from_dsn(dsn)
            try:
                self.assertEqual(
                    Decimal(verifier.state_get("broker_peak_equity_v2")), Decimal("300")
                )
                self.assertIn(Decimal("300"), results)
            finally:
                verifier.close()

    def test_migration_backfills_populated_append_only_events(self) -> None:
        import psycopg

        with self._temporary_database() as dsn:
            body = '{"event_type":"LegacyV2Event","payload":{"status":"existing"}}'
            body_hash = hashlib.sha256(body.encode()).hexdigest()
            event_hash = hashlib.sha256((ZERO_HASH + body).encode()).hexdigest()
            connection = psycopg.connect(dsn, autocommit=True)
            try:
                with connection.transaction(), connection.cursor() as cursor:
                    cursor.execute(
                        """CREATE TABLE v2_schema_migrations(
                           version INTEGER PRIMARY KEY CHECK(version>0),
                           name TEXT NOT NULL UNIQUE,
                           sha256 CHAR(64) NOT NULL CHECK(sha256 ~ '^[0-9a-f]{64}$'),
                           applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"""
                    )
                    for version, name, path in (
                        (1, "core", SCHEMA_PATH),
                        (2, "ai_queue", AI_SCHEMA_PATH),
                    ):
                        schema = path.read_text(encoding="utf-8")
                        cursor.execute(schema, prepare=False)
                        cursor.execute(
                            """INSERT INTO v2_schema_migrations(version,name,sha256)
                               VALUES(%s,%s,%s)""",
                            (version, name, hashlib.sha256(schema.encode()).hexdigest()),
                        )
                    cursor.execute(
                        """INSERT INTO v2_events(
                           event_id,event_type,schema_version,event_time,processing_time,
                           idempotency_key,causation_id,correlation_id,payload,canonical_body,
                           previous_hash,event_hash)
                           VALUES('legacy-v2-event','LegacyV2Event',2,now(),now(),
                           'legacy-v2-event','','migration-test','{}'::jsonb,%s,%s,%s)""",
                        (body, ZERO_HASH, event_hash),
                    )
            finally:
                connection.close()

            store = PostgresRuntimeStoreV2.from_dsn(dsn)
            try:
                store.migrate()
                with store.connection.cursor() as cursor:
                    cursor.execute(
                        """SELECT canonical_body_hash FROM v2_events
                           WHERE event_id='legacy-v2-event'"""
                    )
                    self.assertEqual(str(cursor.fetchone()[0]).strip(), body_hash)
                    cursor.execute(
                        """SELECT COUNT(*) FROM pg_trigger
                           WHERE tgrelid='v2_events'::regclass
                           AND tgname='v2_events_append_only' AND NOT tgisinternal"""
                    )
                    self.assertEqual(int(cursor.fetchone()[0]), 1)
                self.assertTrue(store.verify_event_chain())
                with (
                    self.assertRaises(psycopg.errors.RaiseException),
                    store.connection.transaction(),
                    store.connection.cursor() as cursor,
                ):
                    cursor.execute(
                        "UPDATE v2_events SET payload='{}'::jsonb WHERE event_id='legacy-v2-event'"
                    )
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

    def test_concurrent_distinct_fills_serialize_position_projection(self) -> None:
        with self._temporary_database() as dsn:
            setup_store = PostgresRuntimeStoreV2.from_dsn(dsn)
            config = load_config_v2("config/v2-demo-execution.json")
            now = datetime.now(UTC)
            run_id = uuid4().hex
            try:
                setup_store.migrate()
                kernel = UnifiedTradingKernel(setup_store, GlobalRiskKernel(config.mandate))
                intent = IntentEnvelope(
                    f"intent-pg-fills-{run_id}",
                    "master_1000",
                    "A_deterministic",
                    "postgres-fill-concurrency",
                    "v2",
                    "AAPL",
                    Side.BUY,
                    Decimal("100"),
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
                    correlation_id="fill-concurrency-" + run_id,
                )
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
                decision, command = kernel.submit_open_intent(intent, quote, broker, now=now)
                self.assertTrue(decision.approved)
                self.assertIsNotNone(command)
                assert command is not None
                kernel.begin_submit(command.order_command_id, now)
                kernel.acknowledge(
                    command.order_command_id,
                    at=now,
                    broker_order_id="broker-order-" + run_id,
                    broker_position_id="broker-position-" + run_id,
                )
            finally:
                setup_store.close()

            barrier = Barrier(2)

            def apply(index: int) -> Decimal:
                store = PostgresRuntimeStoreV2.from_dsn(dsn)
                try:
                    store.require_schema()
                    kernel = UnifiedTradingKernel(store, GlobalRiskKernel(config.mandate))
                    barrier.wait(timeout=5)
                    position = kernel.apply_fill(
                        Fill(
                            f"fill-pg-concurrent-{run_id}-{index}",
                            command.order_command_id,
                            command.client_order_id,
                            "broker-order-" + run_id,
                            "broker-position-" + run_id,
                            "AAPL",
                            Side.BUY,
                            Decimal("0.4") if index == 1 else Decimal("0.6"),
                            Decimal("100"),
                            Decimal("0.01") if index == 1 else Decimal("0.02"),
                            Decimal("0"),
                            now + timedelta(seconds=index),
                            now + timedelta(seconds=index),
                            f"fill-pg-concurrent-{run_id}-{index}",
                        ),
                        final=False,
                    )
                    return position.quantity
                finally:
                    store.close()

            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = tuple(pool.map(apply, (1, 2)))
            self.assertEqual(len(outcomes), 2)

            verify = PostgresRuntimeStoreV2.from_dsn(dsn)
            try:
                positions = verify.positions("master_1000", open_only=True)
                self.assertEqual(len(positions), 1)
                self.assertEqual(positions[0].quantity, Decimal("1.0"))
                self.assertEqual(positions[0].fees_accrued, Decimal("0.03"))
                order = verify.broker_order(command.order_command_id)
                self.assertEqual(order.status, OrderStatus.PARTIALLY_FILLED)
                self.assertEqual(order.filled_quantity, Decimal("1.0"))
                self.assertEqual(len(verify.fills_for_order(command.order_command_id)), 2)
                with verify.connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT version FROM v2_positions WHERE position_id=%s",
                        (positions[0].position_id,),
                    )
                    self.assertEqual(int(cursor.fetchone()[0]), 2)
            finally:
                verify.close()

    def test_postgres_outbox_retry_ceiling_quarantines_and_unblocks_fifo(self) -> None:
        with self._temporary_database() as dsn:
            store = PostgresRuntimeStoreV2.from_dsn(dsn)
            try:
                store.migrate()
                now = datetime.now(UTC)
                with store.connection.cursor() as cursor:
                    cursor.execute(
                        """INSERT INTO v2_outbox(
                               outbox_id,topic,payload,idempotency_key,created_at
                           ) VALUES
                           ('outbox-poison','broker.submit','{}'::jsonb,'poison',%s),
                           ('outbox-good','broker.submit','{}'::jsonb,'good',%s)""",
                        (now, now + timedelta(seconds=1)),
                    )
                for attempt in range(1, 4):
                    claimed = store.claim_outbox(
                        "pg-executor",
                        now=now + timedelta(seconds=attempt),
                        limit=1,
                    )
                    self.assertEqual(len(claimed), 1)
                    self.assertEqual(claimed[0]["outbox_id"], "outbox-poison")
                    self.assertEqual(claimed[0]["attempt"], attempt)
                    store.release_outbox_claim(
                        "outbox-poison",
                        str(claimed[0]["claim_token"]),
                        error_type="RuntimeError",
                    )

                next_claim = store.claim_outbox(
                    "pg-executor",
                    now=now + timedelta(seconds=4),
                    limit=1,
                )
                self.assertEqual(len(next_claim), 1)
                self.assertEqual(next_claim[0]["outbox_id"], "outbox-good")
                store.mark_outbox_delivered(
                    "outbox-good",
                    str(next_claim[0]["claim_token"]),
                    now + timedelta(seconds=4),
                )
                with store.connection.cursor() as cursor:
                    cursor.execute(
                        """SELECT delivered_at,last_error_type FROM v2_outbox
                           WHERE outbox_id='outbox-poison'"""
                    )
                    delivered_at, marker = cursor.fetchone()
                    self.assertIsNotNone(delivered_at)
                    self.assertTrue(str(marker).startswith("QUARANTINED:RuntimeError:"))
                    cursor.execute(
                        """SELECT payload FROM v2_events
                           WHERE event_type='OutboxQuarantined'"""
                    )
                    payload = cursor.fetchone()[0]
                    self.assertEqual(payload["attempt"], 3)
                    self.assertFalse(payload["network_write_attempted"])
                    self.assertTrue(payload["manual_replay_requires_new_signed_command"])
            finally:
                store.close()

    def test_postgres_outbox_reclaim_clears_stale_pre_submit_classification(self) -> None:
        with self._temporary_database() as dsn:
            store = PostgresRuntimeStoreV2.from_dsn(dsn)
            try:
                store.migrate()
                now = datetime.now(UTC)
                with store.connection.cursor() as cursor:
                    cursor.execute(
                        """INSERT INTO v2_outbox(
                               outbox_id,topic,payload,idempotency_key,created_at
                           ) VALUES(
                               'outbox-boundary-race','broker.submit','{}'::jsonb,
                               'boundary-race',%s
                           )""",
                        (now,),
                    )
                for attempt in (1, 2):
                    claim = store.claim_outbox(
                        "pg-executor",
                        now=now + timedelta(seconds=attempt),
                        lease_seconds=10,
                        limit=1,
                    )[0]
                    store.release_outbox_claim(
                        "outbox-boundary-race",
                        str(claim["claim_token"]),
                        error_type="RuntimeError",
                    )

                final_claim = store.claim_outbox(
                    "pg-executor",
                    now=now + timedelta(seconds=3),
                    lease_seconds=10,
                    limit=1,
                )[0]
                self.assertEqual(final_claim["attempt"], 3)
                with store.connection.cursor() as cursor:
                    cursor.execute(
                        """SELECT last_error_type,delivered_at FROM v2_outbox
                           WHERE outbox_id='outbox-boundary-race'"""
                    )
                    self.assertEqual(tuple(cursor.fetchone()), (None, None))

                # Model a process crash after crossing the submit boundary: no
                # release classification is written.  The expired lease must be
                # reclaimed for SUBMITTING -> UNKNOWN handling, never quarantined
                # as a known pre-submit poison item.
                reclaimed = store.claim_outbox(
                    "pg-executor-recovery",
                    now=now + timedelta(seconds=14),
                    lease_seconds=10,
                    limit=1,
                )
                self.assertEqual(len(reclaimed), 1)
                self.assertEqual(reclaimed[0]["outbox_id"], "outbox-boundary-race")
                self.assertEqual(reclaimed[0]["attempt"], 4)
                with store.connection.cursor() as cursor:
                    cursor.execute(
                        """SELECT delivered_at,last_error_type FROM v2_outbox
                           WHERE outbox_id='outbox-boundary-race'"""
                    )
                    self.assertEqual(tuple(cursor.fetchone()), (None, None))
                    cursor.execute(
                        """SELECT COUNT(*) FROM v2_events
                           WHERE event_type='OutboxQuarantined'"""
                    )
                    self.assertEqual(int(cursor.fetchone()[0]), 0)
            finally:
                store.close()

    def test_postgres_poison_ai_packet_dead_letters_and_unblocks_fifo(self) -> None:
        with self._temporary_database() as dsn:
            store = PostgresRuntimeStoreV2.from_dsn(dsn)
            try:
                store.migrate()
                queue = CanonicalPostgresAIStoreV2(store)
                now = datetime.now(UTC)
                for packet_id, offset in (("pg-poison", 0), ("pg-good", 1)):
                    created = now + timedelta(seconds=offset)
                    queue.queue(
                        DecisionPacketV2(
                            packet_id,
                            created.isoformat(),
                            (now + timedelta(minutes=10)).isoformat(),
                            "C_sol_direct",
                            "ENTRY_REVIEW",
                            ("market",),
                            "feature",
                            "b" * 64,
                            "r" * 64,
                            {},
                            (),
                            None,
                            ("evidence",),
                        ),
                        AIRole.PORTFOLIO_DECIDER,
                    )
                for attempt in range(1, 4):
                    claim = queue.claim(
                        "pg-worker",
                        AIRole.PORTFOLIO_DECIDER,
                        now=now + timedelta(seconds=attempt),
                        authority_mode="SHADOW",
                        execution_epoch=None,
                        max_attempts=3,
                    )
                    self.assertIsNotNone(claim)
                    assert claim is not None
                    self.assertEqual(claim["packet_id"], "pg-poison")
                    queue.fail(
                        "pg-poison",
                        str(claim["claim_token"]),
                        model="test",
                        prompt_hash="p" * 64,
                        run={
                            "run_id": f"pg-run-{attempt}",
                            "status": "ERROR",
                            "latency_ms": 1,
                            "input_tokens": 1,
                            "output_tokens": 0,
                            "reasoning_tokens": 0,
                            "error_type": "InvalidOutput",
                        },
                        retryable=True,
                        now=now + timedelta(seconds=attempt),
                        max_attempts=3,
                    )
                with store.connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT state,terminal_reason FROM v2_ai_packets WHERE packet_id='pg-poison'"
                    )
                    self.assertEqual(
                        tuple(cursor.fetchone()),
                        ("DEAD_LETTER", "inference_retry_exhausted"),
                    )
                    cursor.execute(
                        "SELECT COUNT(*) FROM v2_events WHERE event_type='AIPacketDeadLettered'"
                    )
                    self.assertEqual(int(cursor.fetchone()[0]), 1)
                next_claim = queue.claim(
                    "pg-worker",
                    AIRole.PORTFOLIO_DECIDER,
                    now=now + timedelta(seconds=10),
                    authority_mode="SHADOW",
                    execution_epoch=None,
                    max_attempts=3,
                )
                self.assertIsNotNone(next_claim)
                assert next_claim is not None
                self.assertEqual(next_claim["packet_id"], "pg-good")
            finally:
                store.close()

    def test_inference_claim_expires_closed_authority_without_spending_budget(self) -> None:
        with self._temporary_database() as dsn:
            store = PostgresRuntimeStoreV2.from_dsn(dsn)
            try:
                store.migrate()
                queue = CanonicalPostgresAIStoreV2(store)
                now = datetime.now(UTC)

                def packet(packet_id: str, created_at: datetime) -> DecisionPacketV2:
                    return DecisionPacketV2(
                        packet_id,
                        created_at.isoformat(),
                        (created_at + timedelta(minutes=10)).isoformat(),
                        "C_sol_direct",
                        "ENTRY_REVIEW",
                        ("market",),
                        "feature",
                        "b" * 64,
                        "r" * 64,
                        {},
                        (),
                        None,
                        ("evidence",),
                    )

                shadow = packet("stale-shadow-inference", now)
                self.assertTrue(queue.queue(shadow, AIRole.PORTFOLIO_DECIDER))
                stale_error = packet(
                    "stale-shadow-error-at-retry-ceiling",
                    now + timedelta(milliseconds=1),
                )
                self.assertTrue(queue.queue(stale_error, AIRole.PORTFOLIO_DECIDER))
                with store.connection.cursor() as cursor:
                    cursor.execute(
                        """UPDATE v2_ai_packets SET state='ERROR',attempt_count=3
                           WHERE packet_id=%s""",
                        (stale_error.packet_id,),
                    )
                store.set_trading_state(
                    "ACTIVE",
                    actor="test",
                    reason="open execution epoch",
                    at=now + timedelta(seconds=1),
                )
                epoch = int(store.trading_state_snapshot()["version"])

                self.assertIsNone(
                    queue.claim(
                        "execution-inference",
                        AIRole.PORTFOLIO_DECIDER,
                        now=now + timedelta(seconds=2),
                        authority_mode="EXECUTION",
                        execution_epoch=epoch,
                        daily_cap=1,
                    )
                )
                with store.connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT state,terminal_reason FROM v2_ai_packets WHERE packet_id=%s",
                        (shadow.packet_id,),
                    )
                    self.assertEqual(
                        tuple(cursor.fetchone()),
                        ("EXPIRED", "authority_epoch_closed"),
                    )
                    cursor.execute(
                        "SELECT state,terminal_reason FROM v2_ai_packets WHERE packet_id=%s",
                        (stale_error.packet_id,),
                    )
                    self.assertEqual(
                        tuple(cursor.fetchone()),
                        ("EXPIRED", "authority_epoch_closed"),
                    )
                    cursor.execute("SELECT COUNT(*) FROM v2_ai_budget_claims")
                    self.assertEqual(int(cursor.fetchone()[0]), 0)
                    cursor.execute(
                        "SELECT COUNT(*) FROM v2_events WHERE event_type='AIPacketAuthorityExpired'"
                    )
                    self.assertEqual(int(cursor.fetchone()[0]), 2)

                current = packet("current-execution-inference", now + timedelta(seconds=3))
                self.assertTrue(
                    queue.queue(
                        current,
                        AIRole.PORTFOLIO_DECIDER,
                        authority_mode="EXECUTION",
                        execution_epoch=epoch,
                    )
                )
                claim = queue.claim(
                    "execution-inference",
                    AIRole.PORTFOLIO_DECIDER,
                    now=now + timedelta(seconds=4),
                    authority_mode="EXECUTION",
                    execution_epoch=epoch,
                    daily_cap=1,
                )
                self.assertIsNotNone(claim)
                assert claim is not None
                self.assertEqual(claim["packet_id"], current.packet_id)
                with store.connection.cursor() as cursor:
                    cursor.execute("SELECT COUNT(*) FROM v2_ai_budget_claims")
                    self.assertEqual(int(cursor.fetchone()[0]), 1)
            finally:
                store.close()

    def test_shadow_decision_is_quarantined_at_execution_epoch_boundary(self) -> None:
        with self._temporary_database() as dsn:
            store = PostgresRuntimeStoreV2.from_dsn(dsn)
            try:
                store.migrate()
                queue = CanonicalPostgresAIStoreV2(store)
                now = datetime.now(UTC)

                def packet(packet_id: str, created_at: datetime) -> DecisionPacketV2:
                    return DecisionPacketV2(
                        packet_id,
                        created_at.isoformat(),
                        (created_at + timedelta(minutes=10)).isoformat(),
                        "D_sol_plus_critic",
                        "POSITION_REVIEW",
                        ("market",),
                        "feature",
                        "b" * 64,
                        "r" * 64,
                        {},
                        (),
                        {"position_id": "position-1", "symbol": "AAPL"},
                        ("evidence",),
                    )

                shadow = packet("shadow-close", now)
                self.assertTrue(queue.queue(shadow, AIRole.PORTFOLIO_DECIDER))
                with store.connection.cursor() as cursor:
                    cursor.execute(
                        """UPDATE v2_ai_packets SET state='DECIDED',output=%s::jsonb,
                           model='gpt-5.6-sol',prompt_hash=%s,updated_at=%s
                           WHERE packet_id=%s""",
                        ('{"action":"CLOSE"}', "p" * 64, now, shadow.packet_id),
                    )

                worker = DecisionApplyWorkerV2.__new__(DecisionApplyWorkerV2)
                worker.shadow_only = False
                worker.store = store
                worker.queue = queue
                with patch(
                    "etoro_agent.decision_apply_service_v2.execution_gate_present",
                    return_value=True,
                ):
                    self.assertEqual(worker._run_once(), 0)
                with store.connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT state FROM v2_ai_packets WHERE packet_id=%s",
                        (shadow.packet_id,),
                    )
                    self.assertEqual(str(cursor.fetchone()[0]), "DECIDED")
                    cursor.execute("SELECT COUNT(*) FROM v2_order_commands")
                    self.assertEqual(int(cursor.fetchone()[0]), 0)
                    cursor.execute("SELECT COUNT(*) FROM v2_outbox")
                    self.assertEqual(int(cursor.fetchone()[0]), 0)

                store.set_trading_state(
                    "ACTIVE",
                    actor="test",
                    reason="open a fresh execution epoch",
                    at=now + timedelta(seconds=1),
                )
                epoch = int(store.trading_state_snapshot()["version"])
                with patch(
                    "etoro_agent.decision_apply_service_v2.execution_gate_present",
                    return_value=True,
                ):
                    self.assertEqual(worker._run_once(), 0)

                with store.connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT state,terminal_reason FROM v2_ai_packets WHERE packet_id=%s",
                        (shadow.packet_id,),
                    )
                    self.assertEqual(
                        tuple(cursor.fetchone()),
                        ("EXPIRED", "authority_epoch_closed"),
                    )
                    cursor.execute("SELECT COUNT(*) FROM v2_order_commands")
                    self.assertEqual(int(cursor.fetchone()[0]), 0)
                    cursor.execute("SELECT COUNT(*) FROM v2_outbox")
                    self.assertEqual(int(cursor.fetchone()[0]), 0)
                    cursor.execute(
                        "SELECT COUNT(*) FROM v2_events WHERE event_type='AIPacketAuthorityExpired'"
                    )
                    self.assertEqual(int(cursor.fetchone()[0]), 1)

                current = packet("active-close", now + timedelta(seconds=2))
                self.assertTrue(
                    queue.queue(
                        current,
                        AIRole.PORTFOLIO_DECIDER,
                        authority_mode="EXECUTION",
                        execution_epoch=epoch,
                    )
                )
                with store.connection.cursor() as cursor:
                    cursor.execute(
                        """UPDATE v2_ai_packets SET state='DECIDED',output=%s::jsonb,
                           model='gpt-5.6-sol',prompt_hash=%s,updated_at=%s
                           WHERE packet_id=%s""",
                        (
                            '{"action":"CLOSE"}',
                            "q" * 64,
                            now + timedelta(seconds=2),
                            current.packet_id,
                        ),
                    )
                claim = queue.claim_decided(
                    "execution-worker",
                    AIRole.PORTFOLIO_DECIDER,
                    now=now + timedelta(seconds=3),
                    authority_mode="EXECUTION",
                    execution_epoch=epoch,
                )
                self.assertIsNotNone(claim)
                assert claim is not None
                self.assertEqual(claim["packet_id"], current.packet_id)
                self.assertEqual(claim["execution_epoch"], epoch)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
