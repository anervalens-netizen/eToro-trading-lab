from __future__ import annotations

import argparse
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config_v2 import AppConfigV2, load_config_v2
from .decision_apply_service_v2 import _broker_truth, _quote
from .domain_v2 import PositionState, QuoteProvenance
from .etoro_api_current_v2 import EtoroPublicApiDemoClientV2
from .execution_gate_v2 import execution_gate_path, execution_gate_present
from .exits_v2 import ExitContext
from .kernel_v2 import UnifiedTradingKernel
from .postgres_runtime_v2 import PostgresRuntimeStoreV2
from .risk_seal_v2 import risk_mandate_hash
from .risk_signer_ipc_v2 import SocketRiskCommandSignerV2
from .risk_v2 import BrokerTruth, GlobalRiskKernel
from .systemd_notify_v2 import ready, watchdog

BrokerTruthProvider = Callable[[datetime], BrokerTruth]
QuoteProvider = Callable[[PositionState, BrokerTruth, datetime], QuoteProvenance]


class DeterministicExitManagerV2:
    """Evaluate mandatory exits continuously and independently of all AI decisions."""

    def __init__(
        self,
        config: AppConfigV2,
        store: Any,
        kernel: UnifiedTradingKernel,
        broker_truth: BrokerTruthProvider,
        quote: QuoteProvider,
        *,
        execution_gate: Path | None = None,
    ) -> None:
        if not config.live_demo_execution_enabled:
            raise PermissionError("deterministic exit manager requires DEMO execution config")
        self.config = config
        self.store = store
        self.kernel = kernel
        self.broker_truth = broker_truth
        self.quote = quote
        self.execution_gate = execution_gate or execution_gate_path()

    def _gate_allows_execution(self, stage: str) -> bool:
        if execution_gate_present(self.execution_gate):
            return True
        self.store.lock_and_invalidate_unstarted(
            actor="v2-exit-manager",
            reason=f"execution gate absent at {stage}",
        )
        return False

    def run_once(self) -> int:
        if not self._gate_allows_execution("exit_iteration_start"):
            return 0
        now = datetime.now(UTC)
        trading_state = self.store.state_get("trading_state", "LOCKED")
        # LOCKED means lock new exposure.  The execution gate is the separate,
        # manual emergency freeze and was checked immediately above.
        if trading_state not in {"ACTIVE", "HALT_NEW", "REDUCE_ONLY", "LOCKED"}:
            self.store.heartbeat(
                "v2-exit-manager",
                "halted",
                {
                    "positions_evaluated": 0,
                    "exit_commands_created": 0,
                    "trading_state": trading_state,
                    "real_money": False,
                },
                at=now,
            )
            return 0
        positions = self.store.positions(open_only=True)
        if not positions:
            self.store.heartbeat(
                "v2-exit-manager",
                "healthy",
                {"positions_evaluated": 0, "exit_commands_created": 0, "real_money": False},
                at=now,
            )
            return 0
        truth = self.broker_truth(now)
        created = 0
        for position in positions:
            if not self._gate_allows_execution("before_exit_evaluation"):
                break
            observed_quote = self.quote(position, truth, now)
            data_valid = (
                observed_quote.age_seconds(now) <= self.config.mandate.max_quote_age_seconds
                and observed_quote.spread_bps <= self.config.mandate.max_spread_bps
            )
            decision = self.kernel.evaluate_exit(
                position,
                ExitContext(now=now, quote=observed_quote, data_valid=data_valid),
            )
            if not decision.should_exit or decision.reason is None:
                continue
            try:
                self.kernel.create_close_command(
                    position,
                    now=now,
                    reason=decision.reason,
                    broker=truth,
                )
            except ValueError as exc:
                if "active reduce-only" not in str(exc):
                    raise
                continue
            created += 1
        state = self.store.state_get("trading_state", "LOCKED")
        self.store.heartbeat(
            "v2-exit-manager",
            ("healthy" if state in {"ACTIVE", "HALT_NEW", "REDUCE_ONLY", "LOCKED"} else "halted"),
            {
                "positions_evaluated": len(positions),
                "exit_commands_created": created,
                "trading_state": state,
                "real_money": False,
            },
            at=now,
        )
        return created

    def run_forever(self, interval_seconds: int = 2) -> None:
        if interval_seconds < 1:
            raise ValueError("exit-manager interval must be positive")
        ready()
        while True:
            try:
                self.run_once()
                watchdog()
            except Exception as exc:
                self.store.heartbeat(
                    "v2-exit-manager",
                    "error",
                    {"error_type": type(exc).__name__, "real_money": False},
                )
                print(f"V2_EXIT_MANAGER_ERROR={type(exc).__name__}", flush=True)
            time.sleep(interval_seconds)


def _dsn(config: AppConfigV2) -> str:
    path = os.getenv("ETORO_V2_POSTGRES_DSN_FILE") or config.postgres_dsn_file
    if not path:
        raise RuntimeError("PostgreSQL DSN credential file is required")
    value = Path(path).read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError("PostgreSQL DSN credential file is empty")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Independent deterministic stop/take/time exit manager for DEMO v2"
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--interval", type=int, default=2)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    config = load_config_v2(args.config)
    signer_socket = os.getenv("ETORO_V2_RISK_SIGNER_SOCKET", "")
    verifying_key_path = os.getenv("ETORO_V2_RISK_VERIFYING_KEY_FILE", "")
    if not signer_socket or not verifying_key_path:
        raise RuntimeError("v2 isolated signer socket and public verifying key are required")
    store = PostgresRuntimeStoreV2.from_dsn(_dsn(config))
    store.require_schema()
    client = EtoroPublicApiDemoClientV2()
    client.verify_isolated_demo_read_scope()
    kernel = UnifiedTradingKernel(
        store,
        GlobalRiskKernel(config.mandate),
        command_signer=SocketRiskCommandSignerV2.from_public_key_file(
            signer_socket,
            verifying_key_path,
            expected_risk_config_hash=risk_mandate_hash(config.mandate),
        ),
    )

    def truth_provider(now: datetime) -> BrokerTruth:
        return _broker_truth(store, client, config=config, now=now)

    def quote_provider(
        position: PositionState, truth: BrokerTruth, now: datetime
    ) -> QuoteProvenance:
        return _quote(
            client,
            symbol=position.symbol,
            instrument_id=config.symbols[position.symbol],
            broker_hash=truth.snapshot_hash,
            received_at=now,
        )

    worker = DeterministicExitManagerV2(
        config,
        store,
        kernel,
        truth_provider,
        quote_provider,
    )
    try:
        if args.once:
            print(f"V2_EXIT_COMMANDS={worker.run_once()}")
        else:
            worker.run_forever(args.interval)
    finally:
        store.close()


if __name__ == "__main__":
    main()
