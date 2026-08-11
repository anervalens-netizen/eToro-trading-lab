from __future__ import annotations

import inspect
import os
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from etoro_agent import (
    etoro_api_current_v2,
    etoro_api_v2,
    executor_v2,
    sol_runner_v2,
    ws_market_v2,
)
from etoro_agent.config_v2 import load_config_v2
from etoro_agent.domain_v2 import IntentEnvelope, QuoteProvenance, Side
from etoro_agent.kernel_v2 import UnifiedTradingKernel
from etoro_agent.risk_seal_v2 import RiskCommandSignerV2, risk_mandate_hash
from etoro_agent.risk_signer_ipc_v2 import (
    RiskSignerServerV2,
    SocketRiskCommandSignerV2,
    validate_signing_request,
)
from etoro_agent.risk_v2 import BrokerTruth, GlobalRiskKernel
from etoro_agent.runtime_store_v2 import RuntimeStoreV2


class V2SecurityBoundaryTests(unittest.TestCase):
    def test_v2_execution_source_contains_no_real_execution_route(self) -> None:
        forbidden = "/trading/execution/" + "real/"
        self.assertNotIn(forbidden, inspect.getsource(etoro_api_v2))
        self.assertNotIn(forbidden, inspect.getsource(etoro_api_current_v2))
        self.assertNotIn(forbidden, inspect.getsource(executor_v2))

    def test_executor_and_market_services_use_separate_user_keys(self) -> None:
        root = Path(__file__).resolve().parents[1] / "ops" / "systemd"
        executor = (root / "etoro-v2-executor-postgres.service").read_text(encoding="utf-8")
        market = (root / "etoro-v2-market.service").read_text(encoding="utf-8")
        self.assertIn("etoro-demo-write-user-key", executor)
        self.assertNotIn("etoro-demo-read-user-key", executor)
        self.assertIn("etoro-demo-read-user-key", market)
        self.assertNotIn("etoro-demo-write-user-key", market)
        self.assertIn("postgres-v2-collector-dsn", market)
        self.assertIn("ENABLE_DEMO_EXECUTION", executor)
        self.assertIn("v2-risk-verifying.pub", executor)
        self.assertNotIn("v2-risk-signing.key", executor)

    def test_private_risk_key_is_loaded_only_by_no_network_signer(self) -> None:
        root = Path(__file__).resolve().parents[1] / "ops" / "systemd"
        decision = (root / "etoro-v2-decision-apply-execution.service").read_text(encoding="utf-8")
        executor = (root / "etoro-v2-executor-postgres.service").read_text(encoding="utf-8")
        signer = (root / "etoro-v2-signer.service").read_text(encoding="utf-8")
        self.assertNotIn("v2-risk-signing.key", decision)
        self.assertIn("v2-risk-verifying.pub", decision)
        self.assertIn("v2-risk-signing.key", signer)
        self.assertIn("PrivateNetwork=yes", signer)
        self.assertIn("IPAddressDeny=any", signer)
        self.assertNotIn("etoro-demo-read-user-key", signer)
        self.assertNotIn("etoro-demo-write-user-key", signer)
        self.assertNotIn("postgres-v2", signer)
        self.assertIn("v2-risk-verifying.pub", executor)
        self.assertNotIn("v2-risk-signing.key", executor)

    def test_critical_services_use_distinct_os_identities_and_release_path(self) -> None:
        root = Path(__file__).resolve().parents[1] / "ops" / "systemd"
        expected = {
            "etoro-v2-market.service": "User=etoro-collector",
            "etoro-v2-decision-apply.service": "User=etoro-engine",
            "etoro-v2-decision-apply-execution.service": "User=etoro-engine",
            "etoro-v2-signer.service": "User=etoro-signer",
            "etoro-v2-executor-postgres.service": "User=etoro-executor",
        }
        for name, identity in expected.items():
            unit = (root / name).read_text(encoding="utf-8")
            self.assertIn(identity, unit)
            self.assertNotIn("User=etoro-agent", unit)
            self.assertIn("/opt/etoro-v2/current", unit)

    @staticmethod
    def _unsigned_open(folder: str, now: datetime):
        config = load_config_v2("config/v2-demo-execution.json")
        store = RuntimeStoreV2(Path(folder) / "runtime.sqlite3")
        kernel = UnifiedTradingKernel(store, GlobalRiskKernel(config.mandate))
        intent = IntentEnvelope(
            "intent-signer",
            "master_1000",
            "D_sol_plus_critic",
            "test",
            "v2",
            "AAPL",
            Side.BUY,
            Decimal("100"),
            Decimal("0.8"),
            Decimal("0.6"),
            Decimal("0.02"),
            Decimal("0.04"),
            3600,
            now,
            now,
            now + timedelta(minutes=5),
            Decimal("99.9"),
            Decimal("100"),
            Decimal("50"),
            Decimal("25"),
            "market",
            correlation_id="packet-signer",
        )
        quote = QuoteProvenance(
            "AAPL",
            Decimal("99.9"),
            Decimal("100"),
            now,
            now,
            "test",
            "quote-signer",
            "market",
            "broker",
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
            "broker",
            now,
        )
        _, signed = kernel.submit_open_intent(intent, quote, broker, now=now)
        assert signed is not None
        store.close()
        return config, replace(signed, risk_payload_hash="", risk_seal="")

    def test_isolated_signer_revalidates_mandate_and_ipc_signature(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            now = datetime.now(UTC)
            config, command = self._unsigned_open(folder, now)
            signer = RiskCommandSignerV2.generate()
            socket_path = Path(folder) / "risk-signer.sock"
            server = RiskSignerServerV2(
                socket_path=socket_path,
                config=config,
                signer=signer,
                allowed_peer_uid=os.getuid(),
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.assertTrue(server.wait_until_ready(2))
            client = SocketRiskCommandSignerV2(
                socket_path,
                signer._private_key.public_key(),
                expected_risk_config_hash=risk_mandate_hash(config.mandate),
            )
            sealed = client.seal(command)
            self.assertTrue(
                client.verifier(expected_risk_config_hash=command.risk_config_hash).verify(sealed)
            )
            server.stop()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())

    def test_isolated_signer_rejects_budget_escalation(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            now = datetime.now(UTC)
            config, command = self._unsigned_open(folder, now)
            escalated = replace(
                command,
                amount_usd=Decimal("1001"),
                available_notional_budget_usd=Decimal("1001"),
            )
            with self.assertRaisesRegex(PermissionError, "notional"):
                validate_signing_request(escalated, config, now=now)

    def test_postgres_backup_is_mandatory_and_credential_backed(self) -> None:
        root = Path(__file__).resolve().parents[1]
        script = (root / "ops/backup/backup-v2.sh").read_text(encoding="utf-8")
        drill = (root / "ops/backup/restore-drill-v2.sh").read_text(encoding="utf-8")
        unit = (root / "ops/systemd/etoro-v2-backup.service").read_text(encoding="utf-8")
        self.assertIn("postgres_service_unavailable", script)
        self.assertIn("pg_dump_unavailable", script)
        self.assertNotIn("&& command -v pg_dump", script)
        self.assertIn('"opt/etoro-v2/current/wheelhouse"', script)
        self.assertIn("offline_wheelhouse_missing", script)
        self.assertIn("sha256sum --check --strict WHEELHOUSE_SHA256SUMS.txt", drill)
        self.assertIn('release["commit"] == candidate["commit"]', drill)
        self.assertIn("LoadCredential=postgres-v2-pgservice", unit)
        self.assertIn("setfacl -m u:andrei:r--", unit)
        replicate = (root / "ops/backup/replicate-offhost-v2.sh").read_text(encoding="utf-8")
        offhost = (root / "ops/systemd/etoro-v2-offhost-backup.service").read_text(encoding="utf-8")
        self.assertIn("destination_not_remote", replicate)
        self.assertIn("immutable_conflict", replicate)
        self.assertIn("RequiresMountsFor=/mnt/nas", offhost)
        self.assertIn("ReadOnlyPaths=/opt/etoro-v2/current", offhost)

    def test_dashboard_has_no_inet_socket_and_anchor_has_no_network(self) -> None:
        root = Path(__file__).resolve().parents[1] / "ops" / "systemd"
        dashboard = (root / "etoro-v2-dashboard.service").read_text(encoding="utf-8")
        anchor = (root / "etoro-v2-anchor.service").read_text(encoding="utf-8")
        role_apply = (root / "etoro-v2-role-apply.service").read_text(encoding="utf-8")
        self.assertIn("RestrictAddressFamilies=AF_UNIX\n", dashboard)
        self.assertIn("RestrictAddressFamilies=AF_UNIX\n", anchor)
        self.assertIn("setfacl -m u:andrei:r--", anchor)
        self.assertIn("RestrictAddressFamilies=AF_UNIX\n", role_apply)
        self.assertNotIn("AF_INET", dashboard)
        self.assertNotIn("AF_INET", anchor)
        self.assertNotIn("AF_INET", role_apply)

    def test_websocket_is_pinned_to_official_host(self) -> None:
        self.assertEqual(ws_market_v2.ETORO_WS_URL, "wss://ws.etoro.com/ws")

    def test_v2_sol_remote_execution_targets_are_fixed(self) -> None:
        self.assertEqual(sol_runner_v2.REMOTE_HOST, "andrei@server")
        self.assertEqual(
            str(sol_runner_v2.SSH_IDENTITY),
            "/opt/Mobiup/.ssh/id_ed25519_mobiup_primary_admin",
        )
        unit = (
            Path(__file__).resolve().parents[1] / "ops/systemd/etoro-v2-sol-runner.service"
        ).read_text(encoding="utf-8")
        self.assertNotIn("ETORO_V2_REMOTE_HOST", unit)
        self.assertNotIn("ETORO_V2_CODEX_NATIVE", unit)
        self.assertNotIn(
            "dangerously-bypass-approvals-and-sandbox", inspect.getsource(sol_runner_v2)
        )
        self.assertIn("LoadCredential=postgres-v2-dsn", inspect.getsource(sol_runner_v2))
        self.assertIn("PrivateNetwork=yes", inspect.getsource(sol_runner_v2))
        self.assertIn('"read-only"', inspect.getsource(sol_runner_v2))

    def test_long_running_services_have_readiness_and_watchdogs(self) -> None:
        root = Path(__file__).resolve().parents[1] / "ops" / "systemd"
        expected = {
            "etoro-v2-market.service": "WatchdogSec=180",
            "etoro-v2-coordinator.service": "WatchdogSec=180",
            "etoro-v2-decision-apply.service": "WatchdogSec=180",
            "etoro-v2-decision-apply-execution.service": "WatchdogSec=180",
            "etoro-v2-executor-postgres.service": "WatchdogSec=180",
            "etoro-v2-reconciliation.service": "WatchdogSec=180",
            "etoro-v2-role-apply.service": "WatchdogSec=180",
            "etoro-v2-signer.service": "WatchdogSec=60",
        }
        for name, watchdog in expected.items():
            unit = (root / name).read_text(encoding="utf-8")
            self.assertIn("Type=notify", unit)
            self.assertIn(watchdog, unit)

    def test_release_provisioning_and_restore_drill_are_fail_closed(self) -> None:
        root = Path(__file__).resolve().parents[1]
        release = (root / "ops/deploy/install-v2-release.sh").read_text(encoding="utf-8")
        provision = (root / "ops/deploy/provision-v2-host.sh").read_text(encoding="utf-8")
        boundary = (root / "ops/security/verify-v2-boundaries.sh").read_text(encoding="utf-8")
        restore = (root / "ops/systemd/etoro-v2-restore-drill.service").read_text(encoding="utf-8")
        self.assertIn("requirements.lock", release)
        self.assertIn("RELEASE.json", release)
        self.assertNotIn('pip" download', release)
        self.assertIn("--no-index", release)
        self.assertIn("bundle_candidate_mismatch", release)
        self.assertIn("WHEELHOUSE_SHA256SUMS.txt", release)
        self.assertIn('chmod -R u=rwX,go=rX "$stage"', release)
        self.assertIn("etoro-v2-owner", provision)
        self.assertIn("etoro-collector", provision)
        self.assertIn("setfacl -m u:etoro-observer:--x,u:postgres:--x", provision)
        self.assertIn("executor=disabled", provision)
        self.assertIn("executor_reached_signer_socket", boundary)
        self.assertIn("ETORO_V2_ALLOW_RESTORE_DRILL=YES", restore)


if __name__ == "__main__":
    unittest.main()
