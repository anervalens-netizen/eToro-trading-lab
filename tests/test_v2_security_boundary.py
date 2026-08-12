from __future__ import annotations

import inspect
import os
import subprocess
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from etoro_agent import (
    etoro_api_current_v2,
    executor_v2,
    sol_model_service_v2,
    sol_runner_v2,
    ws_market_v2,
)
from etoro_agent.config_v2 import load_config_v2
from etoro_agent.domain_v2 import ExitReason, Fill, IntentEnvelope, QuoteProvenance, Side
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
    @staticmethod
    def _run_release_cutover_precondition(
        root: Path,
        *,
        gate_present: bool = False,
        trading_state: str = "LOCKED",
        active_unit: str = "",
    ) -> subprocess.CompletedProcess[str]:
        candidate = "a" * 40
        release_root = root / "release-root"
        (release_root / "releases" / candidate / ".venv" / "bin").mkdir(parents=True)
        (release_root / "releases" / "old").mkdir()
        (release_root / "current").symlink_to("releases/old")
        dsn = root / "control-dsn"
        dsn.write_text("postgresql://test/read-only", encoding="utf-8")
        gate = root / "execution-gate"
        if gate_present:
            gate.write_text("enabled", encoding="utf-8")
        python_bin = release_root / "releases" / candidate / ".venv" / "bin" / "python"
        state_count = root / "state-count"
        state_count.write_text("0\n", encoding="utf-8")
        python_bin.write_text(
            "#!/usr/bin/env bash\n"
            'count=$(cat "$FAKE_STATE_COUNT"); count=$((count+1)); echo "$count" >"$FAKE_STATE_COUNT"\n'
            'IFS=, read -r -a states <<<"${FAKE_TRADING_STATE:?}"\n'
            "index=$((count-1)); (( index < ${#states[@]} )) || index=$((${#states[@]}-1))\n"
            "printf '%s\\n' \"${states[$index]}\"\n",
            encoding="utf-8",
        )
        python_bin.chmod(0o755)
        state_probe_runner = root / "state-probe-runner"
        state_probe_log = root / "state-probe-log"
        state_probe_runner.write_text(
            "#!/usr/bin/env bash\n"
            'printf "%s\\n" "$*" >>"$FAKE_STATE_PROBE_LOG"\n'
            '[[ "$1" == -u && "$2" == etoro-control ]] || exit 97\n'
            "shift 2\n"
            'exec "$@"\n',
            encoding="utf-8",
        )
        state_probe_runner.chmod(0o755)
        systemctl_bin = root / "systemctl"
        systemctl_bin.write_text(
            "#!/usr/bin/env bash\n"
            'if [[ ${2:-} == "${FAKE_ACTIVE_UNIT:-}" ]]; then\n'
            "  printf 'active\\n'; exit 0\n"
            "fi\n"
            "printf 'inactive\\n'; exit 3\n",
            encoding="utf-8",
        )
        systemctl_bin.chmod(0o755)
        installer = Path(__file__).resolve().parents[1] / "ops/deploy/install-v2-release.sh"
        env = {
            **os.environ,
            "ETORO_V2_RELEASE_LIB_ONLY": "1",
            "ETORO_V2_EXECUTION_GATE_FILE": str(gate),
            "ETORO_V2_RELEASE_STATE_DSN_FILE": str(dsn),
            "ETORO_V2_SYSTEMCTL_BIN": str(systemctl_bin),
            "ETORO_V2_STATE_PROBE_RUNNER": str(state_probe_runner),
            "FAKE_TRADING_STATE": trading_state,
            "FAKE_STATE_COUNT": str(state_count),
            "FAKE_STATE_PROBE_LOG": str(state_probe_log),
            "FAKE_ACTIVE_UNIT": active_unit,
        }
        result = subprocess.run(
            [
                "bash",
                "-c",
                'source "$1"; promote_v2_current_symlink "$2" "$3" "$4"',
                "cutover-test",
                str(installer),
                str(release_root),
                candidate,
                str(python_bin),
            ],
            text=True,
            capture_output=True,
            check=False,
            env=env,
        )
        if state_probe_log.exists():
            for invocation in state_probe_log.read_text(encoding="utf-8").splitlines():
                if not invocation.startswith("-u etoro-control "):
                    raise AssertionError(invocation)
        return result

    def test_release_cutover_rejects_gate_active_state_and_active_writers_atomically(self) -> None:
        scenarios = (
            ({"gate_present": True}, "execution_gate_present"),
            ({"trading_state": "ACTIVE"}, "trading_state_not_locked"),
            ({"trading_state": "LOCKED,LOCKED,ACTIVE"}, "trading_state_not_locked"),
            (
                {"active_unit": "etoro-v2-decision-apply-execution.service"},
                "writer_not_inactive",
            ),
            (
                {"active_unit": "etoro-v2-executor-postgres.service"},
                "writer_not_inactive",
            ),
            (
                {"active_unit": "etoro-v2-exit-manager.service"},
                "writer_not_inactive",
            ),
        )
        for inputs, error in scenarios:
            with self.subTest(error=error, inputs=inputs), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                result = self._run_release_cutover_precondition(root, **inputs)
                release_root = root / "release-root"
                self.assertNotEqual(result.returncode, 0, result)
                self.assertIn(error, result.stderr)
                self.assertEqual(os.readlink(release_root / "current"), "releases/old")
                self.assertEqual(list(release_root.glob(".current-*")), [])

    def test_release_bootstrap_supports_fresh_and_upgrade_before_atomic_cutover(self) -> None:
        installer = Path(__file__).resolve().parents[1] / "ops/deploy/install-v2-release.sh"
        for existing_dsn in (False, True):
            with self.subTest(existing_dsn=existing_dsn), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                candidate = "a" * 40
                release_root = root / "release-root"
                release = release_root / "releases" / candidate
                (release / ".venv" / "bin").mkdir(parents=True)
                (release_root / "releases" / "old").mkdir()
                (release_root / "current").symlink_to("releases/old")
                dsn = root / "control-dsn"
                if existing_dsn:
                    dsn.write_text("upgrade-control-dsn\n", encoding="utf-8")
                log = root / "bootstrap-log"
                receipt_log = root / "receipt-log"
                provision = release / "ops" / "deploy" / "provision-v2-host.sh"
                provision.parent.mkdir(parents=True)
                provision.write_text(
                    "#!/usr/bin/env bash\n"
                    'printf "%s\\n" "$2" >"$FAKE_BOOTSTRAP_LOG"\n'
                    '[[ -s "$ETORO_V2_RELEASE_STATE_DSN_FILE" ]] '
                    "&& previous=6 || previous=absent\n"
                    'printf "%s\\n" "$previous" >"$ETORO_V2_SCHEMA_ROLLBACK_RECEIPT"\n'
                    'printf "%s\\n" "$previous" >"$FAKE_RECEIPT_LOG"\n'
                    '[[ -s "$ETORO_V2_RELEASE_STATE_DSN_FILE" ]] || '
                    'printf "fresh-control-dsn\\n" >"$ETORO_V2_RELEASE_STATE_DSN_FILE"\n',
                    encoding="utf-8",
                )
                provision.chmod(0o755)
                python_bin = release / ".venv" / "bin" / "python"
                python_bin.write_text("#!/usr/bin/env bash\necho LOCKED\n", encoding="utf-8")
                python_bin.chmod(0o755)
                state_probe_runner = root / "state-probe-runner"
                state_probe_log = root / "state-probe-log"
                state_probe_runner.write_text(
                    "#!/usr/bin/env bash\n"
                    'printf "%s\\n" "$*" >>"$FAKE_STATE_PROBE_LOG"\n'
                    '[[ "$1" == -u && "$2" == etoro-control ]] || exit 97\n'
                    "shift 2\n"
                    'exec "$@"\n',
                    encoding="utf-8",
                )
                state_probe_runner.chmod(0o755)
                systemctl = root / "systemctl"
                systemctl.write_text(
                    "#!/usr/bin/env bash\n"
                    + ("echo inactive\nexit 3\n" if existing_dsn else "echo unknown\nexit 4\n"),
                    encoding="utf-8",
                )
                systemctl.chmod(0o755)
                result = subprocess.run(
                    [
                        "bash",
                        "-c",
                        'source "$1"; prepare_v2_control_plane "$2" "$3"; '
                        '[[ "$(readlink "$4/current")" == releases/old ]]; '
                        'promote_v2_current_symlink "$4" "$5" "$3"; '
                        "discard_v2_schema_rollback_receipt",
                        "bootstrap-test",
                        str(installer),
                        str(release),
                        str(python_bin),
                        str(release_root),
                        candidate,
                    ],
                    text=True,
                    capture_output=True,
                    check=False,
                    env={
                        **os.environ,
                        "ETORO_V2_RELEASE_LIB_ONLY": "1",
                        "ETORO_V2_RELEASE_STATE_DSN_FILE": str(dsn),
                        "ETORO_V2_EXECUTION_GATE_FILE": str(root / "gate"),
                        "ETORO_V2_SYSTEMCTL_BIN": str(systemctl),
                        "ETORO_V2_STATE_PROBE_RUNNER": str(state_probe_runner),
                        "FAKE_BOOTSTRAP_LOG": str(log),
                        "FAKE_RECEIPT_LOG": str(receipt_log),
                        "FAKE_STATE_PROBE_LOG": str(state_probe_log),
                    },
                )
                self.assertEqual(result.returncode, 0, result)
                self.assertEqual(log.read_text(encoding="utf-8").strip(), "--bootstrap-control")
                self.assertEqual(
                    receipt_log.read_text(encoding="utf-8").strip(),
                    "6" if existing_dsn else "absent",
                )
                self.assertEqual(os.readlink(release_root / "current"), f"releases/{candidate}")
                self.assertTrue(dsn.read_text(encoding="utf-8").strip())
                invocations = state_probe_log.read_text(encoding="utf-8").splitlines()
                self.assertEqual(len(invocations), 4)
                self.assertTrue(
                    all(item.startswith("-u etoro-control ") for item in invocations),
                    invocations,
                )

    def test_provision_bootstrap_rejects_gate_and_active_writer_before_mutation(self) -> None:
        provision = Path(__file__).resolve().parents[1] / "ops/deploy/provision-v2-host.sh"
        scenarios = ((True, "", "execution_gate_present"), (False, "active", "writer_not_inactive"))
        for gate_present, unit_state, expected_error in scenarios:
            with (
                self.subTest(expected_error=expected_error),
                tempfile.TemporaryDirectory() as folder,
            ):
                root = Path(folder)
                gate = root / "gate"
                if gate_present:
                    gate.write_text("enabled\n", encoding="utf-8")
                systemctl = root / "systemctl"
                systemctl.write_text(
                    "#!/usr/bin/env bash\n"
                    + (
                        "echo active\nexit 0\n"
                        if unit_state == "active"
                        else "echo inactive\nexit 3\n"
                    ),
                    encoding="utf-8",
                )
                systemctl.chmod(0o755)
                result = subprocess.run(
                    [
                        "bash",
                        "-c",
                        'source "$1"; assert_v2_provision_quiescent; touch "$2"',
                        "provision-precondition-test",
                        str(provision),
                        str(root / "mutation-marker"),
                    ],
                    text=True,
                    capture_output=True,
                    check=False,
                    env={
                        **os.environ,
                        "ETORO_V2_PROVISION_LIB_ONLY": "1",
                        "ETORO_V2_EXECUTION_GATE_FILE": str(gate),
                        "ETORO_V2_SYSTEMCTL_BIN": str(systemctl),
                    },
                )
                self.assertNotEqual(result.returncode, 0, result)
                self.assertIn(expected_error, result.stderr)
                self.assertFalse((root / "mutation-marker").exists())

    def test_release_restart_replaces_old_pid_and_rejects_stale_identity_or_group(self) -> None:
        installer = Path(__file__).resolve().parents[1] / "ops/deploy/install-v2-release.sh"
        scenarios = (
            ({}, 0, ""),
            ({"FAKE_KEEP_OLD_PID": "1"}, 1, "read_service_pid_not_replaced"),
            ({"FAKE_PROCESS_USER": "etoro-engine"}, 1, "read_service_identity_stale"),
            ({"FAKE_PROCESS_GROUP": "etoro-engine"}, 1, "read_service_primary_group_stale"),
            ({"FAKE_USER_GROUPS": "etoro-collector"}, 1, "read_service_group_missing"),
        )
        for overrides, expected_rc, error in scenarios:
            with self.subTest(overrides=overrides), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                pid_file = root / "pid"
                pid_file.write_text("101\n", encoding="utf-8")
                systemctl = root / "systemctl"
                systemctl.write_text(
                    "#!/usr/bin/env bash\n"
                    "case ${1:-} in\n"
                    "  daemon-reload) exit 0;;\n"
                    "  is-active) [[ ${2:-} == etoro-v2-market.service ]] && { echo active; exit 0; }; echo inactive; exit 3;;\n"
                    '  show) cat "$FAKE_PID_FILE"; exit 0;;\n'
                    '  restart) [[ ${FAKE_KEEP_OLD_PID:-0} == 1 ]] || echo 202 >"$FAKE_PID_FILE"; exit 0;;\n'
                    "esac\nexit 1\n",
                    encoding="utf-8",
                )
                systemctl.chmod(0o755)
                ps = root / "ps"
                ps.write_text(
                    "#!/usr/bin/env bash\n"
                    '[[ ${2:-} == user= ]] && echo "${FAKE_PROCESS_USER:-etoro-collector}" || echo "${FAKE_PROCESS_GROUP:-etoro-collector}"\n',
                    encoding="utf-8",
                )
                ps.chmod(0o755)
                identity = root / "id"
                identity.write_text(
                    '#!/usr/bin/env bash\necho "${FAKE_USER_GROUPS:-etoro-collector etoro-api-clients}"\n',
                    encoding="utf-8",
                )
                identity.chmod(0o755)
                env = {
                    **os.environ,
                    "ETORO_V2_RELEASE_LIB_ONLY": "1",
                    "ETORO_V2_SYSTEMCTL_BIN": str(systemctl),
                    "ETORO_V2_PS_BIN": str(ps),
                    "ETORO_V2_ID_BIN": str(identity),
                    "FAKE_PID_FILE": str(pid_file),
                    **overrides,
                }
                result = subprocess.run(
                    [
                        "bash",
                        "-c",
                        'source "$1"; restart_v2_read_only_services',
                        "test",
                        str(installer),
                    ],
                    text=True,
                    capture_output=True,
                    check=False,
                    env=env,
                )
                self.assertEqual(result.returncode, expected_rc, result)
                if error:
                    self.assertIn(error, result.stderr)
                else:
                    self.assertEqual(pid_file.read_text(encoding="utf-8").strip(), "202")

    def test_release_restart_failure_recovers_all_active_units_on_previous_release(self) -> None:
        installer = Path(__file__).resolve().parents[1] / "ops/deploy/install-v2-release.sh"
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            release_root = root / "release-root"
            (release_root / "releases" / "old" / ".venv" / "bin").mkdir(parents=True)
            (release_root / "releases" / "candidate" / ".venv" / "bin").mkdir(parents=True)
            (release_root / "current").symlink_to("releases/candidate")
            candidate_release = release_root / "releases" / "candidate"
            candidate_units = candidate_release / "ops" / "systemd"
            candidate_units.mkdir(parents=True)
            candidate_configs = candidate_release / "config"
            candidate_configs.mkdir()
            config_dir = root / "config"
            config_dir.mkdir()
            for name in ("v2-demo.json", "v2-demo-execution.json"):
                (candidate_configs / name).write_text("candidate-config\n", encoding="utf-8")
                (config_dir / name).write_text("old-config\n", encoding="utf-8")
            unit_dir = root / "units"
            unit_dir.mkdir()
            identities = {
                "etoro-v2-market.service": "etoro-collector",
                "etoro-v2-coordinator.service": "etoro-candidate",
                "etoro-v2-decision-apply.service": "etoro-decision",
                "etoro-v2-decision-apply-execution.service": "etoro-decision-exec",
                "etoro-v2-role-apply.service": "etoro-ai",
                "etoro-v2-reconciliation.service": "etoro-reconciler",
                "etoro-v2-dashboard.service": "etoro-observer",
                "etoro-v2-anchor.service": "etoro-observer",
            }
            for unit, service_identity in identities.items():
                (unit_dir / unit).write_text("[Service]\nUser=etoro-engine\n", encoding="utf-8")
                (candidate_units / unit).write_text(
                    f"[Service]\nUser={service_identity}\n", encoding="utf-8"
                )
            engine_dsn = root / "postgres-v2-engine-dsn"
            engine_dsn.write_text("old-engine-dsn\n", encoding="utf-8")
            schema_version = root / "schema-version"
            schema_version.write_text("7\n", encoding="utf-8")
            schema_receipt = root / "schema-receipt"
            schema_receipt.write_text("6\n", encoding="utf-8")
            provision = candidate_release / "ops" / "deploy" / "provision-v2-host.sh"
            provision.parent.mkdir(parents=True, exist_ok=True)
            provision.write_text(
                "#!/usr/bin/env bash\n"
                "[[ ${2:-} == --restore-schema-version ]] || exit 2\n"
                'cp "$3" "$FAKE_SCHEMA_VERSION"\n'
                'printf "%s\\n" "$2" >"$FAKE_SCHEMA_RESTORE_LOG"\n',
                encoding="utf-8",
            )
            provision.chmod(0o755)
            schema_restore_log = root / "schema-restore-log"
            state = root / "state"
            state.mkdir()
            for name, pid in (("market", "101"), ("coordinator", "102")):
                (state / f"{name}.pid").write_text(f"{pid}\n", encoding="utf-8")
                (state / f"{name}.target").write_text("old\n", encoding="utf-8")

            systemctl = root / "systemctl"
            systemctl.write_text(
                "#!/usr/bin/env bash\n"
                "unit=${2:-}; [[ ${1:-} == show ]] && unit=${5:-}\n"
                "case ${1:-} in\n"
                "  daemon-reload) exit 0;;\n"
                "  is-active)\n"
                "    case $unit in etoro-v2-market.service|etoro-v2-coordinator.service) echo active; exit 0;; esac\n"
                "    echo inactive; exit 3;;\n"
                "  show)\n"
                "    [[ $unit == etoro-v2-market.service ]] && name=market || name=coordinator\n"
                '    cat "$FAKE_STATE_DIR/$name.pid"; exit 0;;\n'
                "  restart)\n"
                "    [[ $unit == etoro-v2-market.service ]] && { name=market; suffix=1; } || { name=coordinator; suffix=2; }\n"
                '    target=$(basename "$(readlink "$FAKE_RELEASE_ROOT/current")")\n'
                "    if [[ $target == candidate && $name == coordinator ]]; then\n"
                '      grep -Fxq "User=etoro-candidate" "$FAKE_UNIT_DIR/$unit" || exit 9\n'
                '      echo candidate-units-loaded >"$FAKE_STATE_DIR/candidate-loaded"\n'
                "      exit 1\n"
                "    fi\n"
                '    [[ $target == candidate ]] && pid="20$suffix" || pid="30$suffix"\n'
                '    printf "%s\\n" "$pid" >"$FAKE_STATE_DIR/$name.pid"\n'
                '    printf "%s\\n" "$target" >"$FAKE_STATE_DIR/$name.target"\n'
                "    exit 0;;\n"
                '  stop) printf "%s\\n" "$unit" >>"$FAKE_STATE_DIR/stopped"; exit 0;;\n'
                "esac\nexit 1\n",
                encoding="utf-8",
            )
            systemctl.chmod(0o755)
            ps = root / "ps"
            ps.write_text(
                "#!/usr/bin/env bash\n"
                'pid=${4:-}; name=; for path in "$FAKE_STATE_DIR"/*.pid; do '
                '[[ $(cat "$path") == "$pid" ]] && { name=$(basename "$path" .pid); break; }; done\n'
                "[[ $name == market ]] && user=etoro-collector || user=etoro-candidate\n"
                'case ${2:-} in user=|group=) echo "$user";; args=) '
                'target=$(cat "$FAKE_STATE_DIR/$name.target"); '
                'echo "$FAKE_RELEASE_ROOT/releases/$target/.venv/bin/python worker";; esac\n',
                encoding="utf-8",
            )
            ps.chmod(0o755)
            identity = root / "id"
            identity.write_text(
                '#!/usr/bin/env bash\necho "$2 etoro-api-clients"\n', encoding="utf-8"
            )
            identity.chmod(0o755)
            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    'source "$1"; stage_v2_read_only_unit_cutover "$3"; '
                    'stage_v2_runtime_config_cutover "$3"; '
                    'restart_v2_read_only_services "$2" releases/old "$V2_UNIT_BACKUP_DIR" '
                    '"$3" "$4" "$V2_CONFIG_BACKUP_DIR"',
                    "transactional-restart-test",
                    str(installer),
                    str(release_root),
                    str(candidate_release),
                    str(schema_receipt),
                ],
                text=True,
                capture_output=True,
                check=False,
                env={
                    **os.environ,
                    "ETORO_V2_RELEASE_LIB_ONLY": "1",
                    "ETORO_V2_SYSTEMCTL_BIN": str(systemctl),
                    "ETORO_V2_PS_BIN": str(ps),
                    "ETORO_V2_ID_BIN": str(identity),
                    "FAKE_RELEASE_ROOT": str(release_root),
                    "FAKE_STATE_DIR": str(state),
                    "FAKE_UNIT_DIR": str(unit_dir),
                    "ETORO_V2_SYSTEMD_UNIT_DIR": str(unit_dir),
                    "ETORO_V2_CONFIG_DIR": str(config_dir),
                    "FAKE_SCHEMA_VERSION": str(schema_version),
                    "FAKE_SCHEMA_RESTORE_LOG": str(schema_restore_log),
                },
            )
            self.assertNotEqual(result.returncode, 0, result)
            self.assertIn(
                "read_service_restart_failed unit=etoro-v2-coordinator.service", result.stderr
            )
            self.assertIn("ETORO_V2_RELEASE_RECOVERY_OK", result.stderr)
            self.assertEqual(os.readlink(release_root / "current"), "releases/old")
            self.assertEqual((state / "market.pid").read_text(encoding="utf-8").strip(), "301")
            self.assertEqual((state / "coordinator.pid").read_text(encoding="utf-8").strip(), "302")
            self.assertEqual((state / "market.target").read_text(encoding="utf-8").strip(), "old")
            self.assertEqual(
                (state / "coordinator.target").read_text(encoding="utf-8").strip(), "old"
            )
            self.assertTrue((state / "candidate-loaded").exists())
            for unit in identities:
                self.assertIn("User=etoro-engine", (unit_dir / unit).read_text(encoding="utf-8"))
            for name in ("v2-demo.json", "v2-demo-execution.json"):
                self.assertEqual((config_dir / name).read_text(encoding="utf-8"), "old-config\n")
            self.assertEqual(engine_dsn.read_text(encoding="utf-8"), "old-engine-dsn\n")
            self.assertEqual(schema_version.read_text(encoding="utf-8").strip(), "6")
            self.assertEqual(
                schema_restore_log.read_text(encoding="utf-8").strip(),
                "--restore-schema-version",
            )
            self.assertFalse((state / "stopped").exists())

    def test_runtime_config_backup_failure_never_overwrites_previous_config(self) -> None:
        installer = Path(__file__).resolve().parents[1] / "ops/deploy/install-v2-release.sh"
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            release = root / "release"
            config_dir = root / "config"
            (release / "config").mkdir(parents=True)
            config_dir.mkdir()
            for name in ("v2-demo.json", "v2-demo-execution.json"):
                (release / "config" / name).write_text("candidate\n", encoding="utf-8")
                (config_dir / name).write_text("old\n", encoding="utf-8")
            failing_cp = root / "cp"
            failing_cp.write_text("#!/usr/bin/env bash\nexit 91\n", encoding="utf-8")
            failing_cp.chmod(0o755)
            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    'source "$1"; stage_v2_runtime_config_cutover "$2"',
                    "config-backup-failure",
                    str(installer),
                    str(release),
                ],
                text=True,
                capture_output=True,
                check=False,
                env={
                    **os.environ,
                    "ETORO_V2_RELEASE_LIB_ONLY": "1",
                    "ETORO_V2_CONFIG_DIR": str(config_dir),
                    "ETORO_V2_CP_BIN": str(failing_cp),
                },
            )
            self.assertNotEqual(result.returncode, 0, result)
            for name in ("v2-demo.json", "v2-demo-execution.json"):
                self.assertEqual((config_dir / name).read_text(encoding="utf-8"), "old\n")

    def test_runtime_config_install_failure_restores_both_previous_configs(self) -> None:
        installer = Path(__file__).resolve().parents[1] / "ops/deploy/install-v2-release.sh"
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            release = root / "release"
            config_dir = root / "config"
            (release / "config").mkdir(parents=True)
            config_dir.mkdir()
            for name in ("v2-demo.json", "v2-demo-execution.json"):
                (release / "config" / name).write_text("candidate\n", encoding="utf-8")
                (config_dir / name).write_text("old\n", encoding="utf-8")
            install_wrapper = root / "install"
            install_wrapper.write_text(
                "#!/usr/bin/env bash\n"
                '[[ ${1:-} == -d ]] && exec /usr/bin/install "$@"\n'
                "[[ ${@: -1} == *v2-demo-execution.json.* ]] && exit 92\n"
                'exec /usr/bin/install "$@"\n',
                encoding="utf-8",
            )
            install_wrapper.chmod(0o755)
            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    'source "$1"; stage_v2_runtime_config_cutover "$2"',
                    "config-install-failure",
                    str(installer),
                    str(release),
                ],
                text=True,
                capture_output=True,
                check=False,
                env={
                    **os.environ,
                    "ETORO_V2_RELEASE_LIB_ONLY": "1",
                    "ETORO_V2_CONFIG_DIR": str(config_dir),
                    "ETORO_V2_INSTALL_BIN": str(install_wrapper),
                },
            )
            self.assertNotEqual(result.returncode, 0, result)
            for name in ("v2-demo.json", "v2-demo-execution.json"):
                self.assertEqual((config_dir / name).read_text(encoding="utf-8"), "old\n")

    def test_candidate_unit_cutover_precedes_legacy_engine_dsn_retirement(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        installer = repo / "ops/deploy/install-v2-release.sh"
        provision = repo / "ops/deploy/provision-v2-host.sh"
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            release = root / "release"
            candidate_units = release / "ops" / "systemd"
            candidate_units.mkdir(parents=True)
            unit_dir = root / "units"
            unit_dir.mkdir()
            identities = {
                "etoro-v2-market.service": "etoro-collector",
                "etoro-v2-coordinator.service": "etoro-candidate",
                "etoro-v2-decision-apply.service": "etoro-decision",
                "etoro-v2-decision-apply-execution.service": "etoro-decision-exec",
                "etoro-v2-role-apply.service": "etoro-ai",
                "etoro-v2-reconciliation.service": "etoro-reconciler",
                "etoro-v2-dashboard.service": "etoro-observer",
                "etoro-v2-anchor.service": "etoro-observer",
            }
            for unit, service_identity in identities.items():
                (unit_dir / unit).write_text("[Service]\nUser=etoro-engine\n", encoding="utf-8")
                (candidate_units / unit).write_text(
                    f"[Service]\nUser={service_identity}\n", encoding="utf-8"
                )
            engine_dsn = root / "postgres-v2-engine-dsn"
            engine_dsn.write_text("old-engine-dsn\n", encoding="utf-8")
            fake_bin = root / "bin"
            fake_bin.mkdir()
            sudo = fake_bin / "sudo"
            sudo.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            sudo.chmod(0o755)
            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    'source "$1"; stage_v2_read_only_unit_cutover "$3"; '
                    '[[ -s "$ETORO_V2_LEGACY_ENGINE_DSN_FILE" ]]; '
                    'source "$2"; retire_v2_legacy_engine 5434',
                    "candidate-unit-cutover-test",
                    str(installer),
                    str(provision),
                    str(release),
                ],
                text=True,
                capture_output=True,
                check=False,
                env={
                    **os.environ,
                    "PATH": f"{fake_bin}:{os.environ['PATH']}",
                    "ETORO_V2_RELEASE_LIB_ONLY": "1",
                    "ETORO_V2_PROVISION_LIB_ONLY": "1",
                    "ETORO_V2_SYSTEMD_UNIT_DIR": str(unit_dir),
                    "ETORO_V2_LEGACY_ENGINE_DSN_FILE": str(engine_dsn),
                },
            )
            self.assertEqual(result.returncode, 0, result)
            for unit, service_identity in identities.items():
                self.assertIn(
                    f"User={service_identity}",
                    (unit_dir / unit).read_text(encoding="utf-8"),
                )
            self.assertFalse(engine_dsn.exists())

    def test_v2_execution_source_contains_no_real_execution_route(self) -> None:
        forbidden = "/trading/execution/" + "real/"
        self.assertNotIn(forbidden, inspect.getsource(etoro_api_current_v2))
        self.assertNotIn(forbidden, inspect.getsource(executor_v2))
        package_root = Path(etoro_api_current_v2.__file__).resolve().parent
        self.assertFalse((package_root / "etoro_api_v2.py").exists())

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
        self.assertIn("postgres-v2-decision-exec-dsn", decision)
        self.assertNotIn("postgres-v2-decision-dsn", decision)
        self.assertIn("v2-risk-signing.key", signer)
        self.assertIn("PrivateNetwork=yes", signer)
        self.assertIn("IPAddressDeny=any", signer)
        self.assertNotIn("etoro-demo-read-user-key", signer)
        self.assertNotIn("etoro-demo-write-user-key", signer)
        self.assertNotIn("postgres-v2", signer)
        self.assertIn("--allowed-peer-user etoro-decision-exec", signer)
        self.assertNotIn("--allowed-peer-user etoro-decision ", signer)
        self.assertIn("v2-risk-verifying.pub", executor)
        self.assertNotIn("v2-risk-signing.key", executor)

    def test_critical_services_use_distinct_os_identities_and_release_path(self) -> None:
        root = Path(__file__).resolve().parents[1] / "ops" / "systemd"
        expected = {
            "etoro-v2-market.service": "User=etoro-collector",
            "etoro-v2-coordinator.service": "User=etoro-candidate",
            "etoro-v2-role-apply.service": "User=etoro-ai",
            "etoro-v2-decision-apply.service": "User=etoro-decision",
            "etoro-v2-decision-apply-execution.service": "User=etoro-decision-exec",
            "etoro-v2-exit-manager.service": "User=etoro-exit",
            "etoro-v2-reconciliation.service": "User=etoro-reconciler",
            "etoro-v2-execution-gate-lock.service": "User=etoro-control",
            "etoro-v2-signer.service": "User=etoro-signer",
            "etoro-v2-executor-postgres.service": "User=etoro-executor",
        }
        for name, identity in expected.items():
            unit = (root / name).read_text(encoding="utf-8")
            self.assertIn(identity, unit)
            self.assertNotIn("User=etoro-agent", unit)
            self.assertIn("/opt/etoro-v2/current", unit)

    def test_shared_rate_limit_is_cross_identity_writable(self) -> None:
        root = Path(__file__).resolve().parents[1] / "ops" / "systemd"
        tmpfiles = (root / "etoro-v2.tmpfiles").read_text(encoding="utf-8")
        sysusers = (root / "etoro-v2.sysusers").read_text(encoding="utf-8")
        self.assertIn(
            "d /run/etoro-v2-api-rate-limit 2770 root etoro-api-clients -",
            tmpfiles,
        )
        services = {
            "etoro-v2-market.service": "etoro-collector",
            "etoro-v2-coordinator.service": "etoro-candidate",
            "etoro-v2-decision-apply-execution.service": "etoro-decision-exec",
            "etoro-v2-reconciliation.service": "etoro-reconciler",
            "etoro-v2-executor-postgres.service": "etoro-executor",
            "etoro-v2-exit-manager.service": "etoro-exit",
        }
        for name, identity in services.items():
            unit = (root / name).read_text(encoding="utf-8")
            self.assertIn("ETORO_V2_SHARED_RATE_LIMIT_DIR=", unit)
            self.assertIn("etoro-api-clients", unit)
            self.assertIn("UMask=0007", unit)
            self.assertIn(f"m {identity} etoro-api-clients", sysusers)

    def test_postgres_roles_are_service_scoped_and_legacy_engine_cannot_login(self) -> None:
        root = Path(__file__).resolve().parents[1]
        grants = (root / "ops/postgres/grants_v2.sql").read_text(encoding="utf-8")
        provision = (root / "ops/deploy/provision-v2-host.sh").read_text(encoding="utf-8")
        for role in (
            "etoro-candidate",
            "etoro-ai",
            "etoro-decision",
            "etoro-decision-exec",
            "etoro-exit",
            "etoro-reconciler",
            "etoro-control",
        ):
            self.assertIn(f'CREATE ROLE "{role}" LOGIN', provision)
            self.assertIn(f"user={role}", provision)
            self.assertIn(f'"{role}"', grants)
        self.assertIn('ALTER ROLE "etoro-engine" NOLOGIN', provision)
        self.assertIn('REVOKE CONNECT ON DATABASE etoro_v2 FROM "etoro-engine"', grants)
        self.assertIn(
            'user=etoro-decision-exec\\n\' "$pg_port" \\\n'
            "  >/etc/etoro-agent/postgres-v2-decision-exec-dsn",
            provision,
        )
        candidate_grants = "\n".join(
            statement
            for statement in grants.split(";")
            if statement.strip().startswith("GRANT")
            and statement.strip().endswith('TO "etoro-candidate"')
        )
        self.assertTrue(candidate_grants)
        self.assertNotIn("v2_order_commands", candidate_grants)
        self.assertNotIn("v2_outbox", candidate_grants)
        shadow_grants = "\n".join(
            statement
            for statement in grants.split(";")
            if statement.strip().startswith("GRANT")
            and statement.strip().endswith('TO "etoro-decision"')
        )
        self.assertIn("UPDATE ON v2_ai_packets", shadow_grants)
        for table in ("v2_intents", "v2_order_commands", "v2_outbox", "v2_events"):
            self.assertNotIn(table, shadow_grants)
        execution_grants = "\n".join(
            statement
            for statement in grants.split(";")
            if statement.strip().startswith("GRANT")
            and statement.strip().endswith('TO "etoro-decision-exec"')
        )
        for table in ("v2_intents", "v2_order_commands", "v2_outbox", "v2_events"):
            self.assertIn(table, execution_grants)
        for role in ("etoro-decision", "etoro-decision-exec", "etoro-exit", "etoro-executor"):
            mutation_grants = "\n".join(
                statement
                for statement in grants.split(";")
                if statement.strip().startswith("GRANT")
                and statement.strip().endswith(f'TO "{role}"')
                and ("INSERT" in statement or "UPDATE" in statement)
            )
            self.assertNotIn("v2_positions", mutation_grants)
            self.assertNotIn("v2_reconciliation_cases", mutation_grants)
            self.assertNotIn("v2_fills", mutation_grants)
        reconciler_grants = "\n".join(
            statement
            for statement in grants.split(";")
            if statement.strip().startswith("GRANT")
            and statement.strip().endswith('TO "etoro-reconciler"')
        )
        for table in ("v2_positions", "v2_reconciliation_cases", "v2_fills"):
            self.assertIn(table, reconciler_grants)
        self.assertIn("SECURITY DEFINER", (root / "ops/postgres/schema_v7.sql").read_text())
        self.assertIn(
            "GRANT EXECUTE ON FUNCTION v2_trip_audit_integrity_failure()",
            grants,
        )

    @staticmethod
    def _unsigned_open(folder: str, now: datetime):
        config = load_config_v2("config/v2-demo-execution.json")
        store = RuntimeStoreV2(Path(folder) / "runtime.sqlite3")
        kernel = UnifiedTradingKernel(store, GlobalRiskKernel(config.mandate))
        intent, quote, broker = V2SecurityBoundaryTests._unsigned_open_inputs(now)
        store.set_trading_state(
            "ACTIVE",
            actor="test",
            reason="bind isolated signer fixture to execution authority",
            at=now,
        )
        execution_epoch = int(store.trading_state_snapshot()["version"])
        _, signed = kernel.submit_open_intent(
            intent,
            quote,
            broker,
            now=now,
            required_trading_state_version=execution_epoch,
        )
        assert signed is not None
        store.close()
        return config, replace(signed, risk_payload_hash="", risk_seal="")

    @classmethod
    def _unsigned_close(
        cls,
        folder: str,
        now: datetime,
        *,
        units_to_deduct: Decimal | None,
    ):
        config = load_config_v2("config/v2-demo-execution.json")
        store = RuntimeStoreV2(Path(folder) / "runtime.sqlite3")
        kernel = UnifiedTradingKernel(store, GlobalRiskKernel(config.mandate))
        intent, quote, broker = cls._unsigned_open_inputs(now)
        _, opened = kernel.submit_open_intent(
            intent,
            quote,
            broker,
            now=now,
        )
        assert opened is not None
        kernel.begin_submit(opened.order_command_id, now)
        position = kernel.apply_fill(
            Fill(
                "fill-signer-close",
                opened.order_command_id,
                opened.client_order_id,
                "broker-open",
                "12345",
                "AAPL",
                Side.BUY,
                Decimal("1"),
                Decimal("100"),
                Decimal("0"),
                Decimal("0"),
                now,
                now,
                "fill-signer-close",
            ),
            final=True,
        )
        command = kernel.create_close_command(
            position,
            now=now + timedelta(seconds=1),
            reason=ExitReason.REDUCE_ONLY,
            broker=broker,
            units_to_deduct=units_to_deduct,
        )
        store.close()
        return config, replace(command, risk_payload_hash="", risk_seal="")

    @staticmethod
    def _unsigned_open_inputs(now: datetime):
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
        return intent, quote, broker

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
                allowed_peer_uids=frozenset({os.getuid()}),
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

    def test_isolated_signer_accepts_canonical_full_and_partial_close(self) -> None:
        for units in (None, Decimal("0.4")):
            with self.subTest(units=units), tempfile.TemporaryDirectory() as folder:
                now = datetime.now(UTC)
                config, command = self._unsigned_close(
                    folder,
                    now,
                    units_to_deduct=units,
                )
                signer = RiskCommandSignerV2.generate()
                socket_path = Path(folder) / "risk-signer.sock"
                server = RiskSignerServerV2(
                    socket_path=socket_path,
                    config=config,
                    signer=signer,
                    allowed_peer_uids=frozenset({os.getuid()}),
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
                    client.verifier(expected_risk_config_hash=command.risk_config_hash).verify(
                        sealed
                    )
                )
                server.stop()
                thread.join(timeout=2)
                self.assertFalse(thread.is_alive())

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
        self.assertIn(
            "from etoro_agent.postgres_store_impl_v2 import SCHEMA_VERSION",
            drill,
        )
        self.assertIn(
            '[[ "$restored_schema_version" == "$expected_schema_version" ]]',
            drill,
        )
        self.assertNotIn("grep -qx '5'", drill)
        self.assertIn("v2_positions WHERE state->>'quantity' IS NULL", drill)
        self.assertNotIn("v2_positions WHERE quantity IS NULL", drill)
        self.assertNotIn("LAST_RESTORE_DRILL_OK", drill)
        self.assertIn("LoadCredential=postgres-v2-pgservice", unit)
        self.assertIn("setfacl -m u:andrei:r--", unit)
        replicate = (root / "ops/backup/replicate-offhost-v2.sh").read_text(encoding="utf-8")
        offhost = (root / "ops/systemd/etoro-v2-offhost-backup.service").read_text(encoding="utf-8")
        self.assertIn("destination_not_remote", replicate)
        self.assertIn("immutable_conflict", replicate)
        self.assertIn("/var/lib/etoro-v2-offhost", replicate)
        self.assertNotIn("/var/lib/etoro-agent/v2-offhost", replicate)
        self.assertIn("RequiresMountsFor=/mnt/nas", offhost)
        self.assertIn("ReadOnlyPaths=/opt/etoro-v2/current", offhost)
        self.assertIn("Group=etoro-observer", offhost)
        self.assertIn("StateDirectory=etoro-v2-offhost", offhost)
        self.assertNotIn("/var/lib/etoro-agent/v2-offhost", offhost)

    def test_dashboard_has_no_inet_socket_and_anchor_has_no_network(self) -> None:
        root = Path(__file__).resolve().parents[1] / "ops" / "systemd"
        dashboard = (root / "etoro-v2-dashboard.service").read_text(encoding="utf-8")
        anchor = (root / "etoro-v2-anchor.service").read_text(encoding="utf-8")
        role_apply = (root / "etoro-v2-role-apply.service").read_text(encoding="utf-8")
        self.assertIn("RestrictAddressFamilies=AF_UNIX\n", dashboard)
        self.assertIn("/var/lib/etoro-v2-offhost/LAST_OFFHOST_OK", dashboard)
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
        self.assertEqual(
            str(sol_runner_v2.SSH_KNOWN_HOSTS),
            "/run/etoro-v2-sol-runner-known-hosts",
        )
        ssh_argv = sol_runner_v2._ssh("true")
        self.assertIn("StrictHostKeyChecking=yes", ssh_argv)
        self.assertIn(
            "UserKnownHostsFile=/run/etoro-v2-sol-runner-known-hosts",
            ssh_argv,
        )
        self.assertIn("GlobalKnownHostsFile=/dev/null", ssh_argv)
        unit = (
            Path(__file__).resolve().parents[1] / "ops/systemd/etoro-v2-sol-runner.service"
        ).read_text(encoding="utf-8")
        model = (
            Path(__file__).resolve().parents[1] / "ops/systemd/etoro-v2-sol-model@.service"
        ).read_text(encoding="utf-8")
        model_socket = (
            Path(__file__).resolve().parents[1] / "ops/systemd/etoro-v2-sol-model.socket"
        ).read_text(encoding="utf-8")
        self.assertNotIn("ETORO_V2_REMOTE_HOST", unit)
        self.assertNotIn("ETORO_V2_CODEX_NATIVE", unit)
        self.assertIn("ProtectSystem=strict\n", unit)
        self.assertIn("ProtectHome=yes\n", unit)
        self.assertIn("Requires=etoro-v2-sol-model.socket\n", unit)
        self.assertNotIn("CODEX_HOME", unit)
        self.assertIn("ConditionPathExists=/home/andrei/.ssh/known_hosts\n", unit)
        self.assertIn(
            "BindReadOnlyPaths=/home/andrei/.ssh/known_hosts:/run/etoro-v2-sol-runner-known-hosts\n",
            unit,
        )
        self.assertNotIn("sudo", inspect.getsource(sol_runner_v2.run_model))
        self.assertIn("Accept=yes\n", model_socket)
        self.assertIn("ListenStream=/run/etoro-v2-sol-model.sock\n", model_socket)
        self.assertIn("SocketMode=0600\n", model_socket)
        self.assertIn("MaxConnections=1\n", model_socket)
        self.assertIn("NoNewPrivileges=yes\n", model)
        self.assertIn("ProtectSystem=strict\n", model)
        self.assertIn("ProtectHome=tmpfs\n", model)
        self.assertIn("StandardInput=socket\n", model)
        self.assertIn("StandardOutput=socket\n", model)
        self.assertIn("NoExecPaths=/\n", model)
        self.assertIn("ExecStart=/usr/bin/python3.12 -P", model)
        self.assertIn(
            "RuntimeDirectory=etoro-v2-sol-model etoro-v2-sol-model/codex-home\n",
            model,
        )
        self.assertIn("RuntimeDirectoryMode=0700\n", model)
        self.assertIn("RuntimeMaxSec=300\n", model)
        self.assertIn("InaccessiblePaths=-/opt/Mobiup/.ssh\n", model)
        self.assertIn(
            "BindReadOnlyPaths=/home/andrei/.codex/auth.json:/run/etoro-v2-sol-model/codex-home/auth.json\n",
            model,
        )
        self.assertNotIn(
            "dangerously-bypass-approvals-and-sandbox", inspect.getsource(sol_runner_v2)
        )
        self.assertIn("LoadCredential=postgres-v2-dsn", inspect.getsource(sol_runner_v2))
        self.assertIn("PrivateNetwork=yes", inspect.getsource(sol_runner_v2))
        self.assertIn('"read-only"', inspect.getsource(sol_model_service_v2.model_command))

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
        self.assertLess(
            release.index('prepare_v2_control_plane "$release"'),
            release.index('promote_v2_current_symlink "$release_root"'),
        )
        self.assertIn('"$provision" "$release" --bootstrap-control', release)
        self.assertIn("V2_SCHEMA_ROLLBACK_RECEIPT", release)
        self.assertIn("restore_v2_schema_compatibility", release)
        self.assertIn("--restore-schema-version", provision)
        self.assertIn("--single-transaction", provision)
        self.assertIn('chown postgres:postgres "$bootstrap_grants"', provision)
        self.assertLess(
            provision.index('chown postgres:postgres "$bootstrap_grants"'),
            provision.index('--single-transaction -f "$grants_file"'),
        )
        self.assertLess(
            provision.index("previous_schema_version="),
            provision.index("postgres_migrate_v2"),
        )
        self.assertLess(
            release.index('stage_v2_read_only_unit_cutover "$release"'),
            release.index('promote_v2_current_symlink "$release_root"'),
        )
        self.assertLess(
            release.index("restart_v2_read_only_services \\\n"),
            release.index(
                '"$release/ops/deploy/provision-v2-host.sh" "$release" --retire-legacy-engine'
            ),
        )
        self.assertIn("preexisting_trading_state_not_locked", provision)
        self.assertIn("post_migration_trading_state_not_locked", provision)
        self.assertLess(
            provision.index("pre_migration_state="), provision.index("postgres_migrate_v2")
        )
        self.assertLess(
            provision.index("postgres_migrate_v2"), provision.index("post_migration_state=")
        )
        self.assertIn(
            'post_migration_state=$(sudo -u etoro-control "$release/.venv/bin/python"',
            provision,
        )
        self.assertIn("--single-transaction", provision)
        self.assertIn('s/"etoro-engine", //g', provision)
        self.assertIn('retire_v2_legacy_engine "$pg_port"', provision)
        deployment = (root / "V2_DEPLOYMENT.md").read_text(encoding="utf-8")
        self.assertIn("--bootstrap-control", deployment)
        self.assertLess(
            deployment.index("--bootstrap-control"), deployment.index("atomically change")
        )
        self.assertIn('"$release"/ops/systemd/etoro-v2-*.socket', provision)
        self.assertIn("etoro-v2-owner", provision)
        self.assertIn("etoro-collector", provision)
        self.assertIn("setfacl -m u:etoro-observer:--x,u:postgres:--x", provision)
        self.assertIn("executor=disabled", provision)
        self.assertIn("executor_reached_signer_socket", boundary)
        self.assertIn("ETORO_V2_ALLOW_RESTORE_DRILL=YES", restore)
        self.assertIn(
            "ExecStartPost=+/opt/etoro-v2/current/ops/backup/mark-restore-ok-v2.sh",
            restore,
        )
        self.assertIn("ReadWritePaths=/storage/backups/db/etoro/v2", restore)
        marker = (root / "ops/backup/mark-restore-ok-v2.sh").read_text(encoding="utf-8")
        self.assertIn("root_required", marker)
        self.assertIn('mv -f -- "$partial" "$marker"', marker)


if __name__ == "__main__":
    unittest.main()
