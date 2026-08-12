from __future__ import annotations

import argparse
import json
import os
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from .ai_store_postgres_v2 import CanonicalPostgresAIStoreV2
from .ai_v2 import AIRole, DecisionPacketV2, Lane
from .codec_v2 import decode_dataclass
from .config_v2 import load_config_v2
from .execution_gate_v2 import authority_for_state, execution_gate_present
from .postgres_runtime_v2 import PostgresRuntimeStoreV2
from .roles_v2 import gate_decider_with_matching_critic
from .systemd_notify_v2 import ready, watchdog


def _dsn(config_path: str) -> str:
    config = load_config_v2(config_path)
    path = os.getenv("ETORO_V2_POSTGRES_DSN_FILE") or config.postgres_dsn_file
    if not path:
        raise RuntimeError("PostgreSQL DSN credential file is required")
    value = Path(path).read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError("PostgreSQL DSN credential file is empty")
    return value


class RoleApplyWorkerV2:
    def __init__(self, config_path: str) -> None:
        self.store = PostgresRuntimeStoreV2.from_dsn(_dsn(config_path))
        self.store.require_schema()
        self.queue = CanonicalPostgresAIStoreV2(self.store)

    def close(self) -> None:
        self.store.close()

    def _authority(self) -> tuple[str, int | None] | None:
        snapshot = self.store.trading_state_snapshot()
        return authority_for_state(
            str(snapshot["state"]),
            int(snapshot["version"]),
            execution_gate=execution_gate_present(),
        )

    def _run_once(self, limit: int = 20) -> int:
        count = 0
        roles = (AIRole.MARKET_REGIME_ANALYST, AIRole.ADVERSARIAL_CRITIC)
        while count < max(1, min(limit, 100)):
            authority = self._authority()
            if authority is None:
                break
            authority_mode, execution_epoch = authority
            row = None
            for claim_role in roles:
                row = self.queue.claim_decided(
                    "v2-role-apply",
                    claim_role,
                    now=datetime.now(UTC),
                    authority_mode=authority_mode,
                    execution_epoch=execution_epoch,
                )
                if row is not None:
                    break
            if row is None:
                break
            role = str(row["role"])
            packet_id = str(row["packet_id"])
            claim_token = str(row["apply_claim_token"])
            key = (
                "latest_regime_v2:"
                if role == AIRole.MARKET_REGIME_ANALYST.value
                else "latest_critic_v2:"
            ) + str(row["lane"])
            value = json.dumps(
                {
                    "packet_id": row["packet_id"],
                    "packet_hash": row["packet_hash"],
                    "output": row["output"],
                    "model": row["model"],
                    "updated_at": row["updated_at"],
                },
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
            try:
                row_authority = (
                    str(row["authority_mode"]),
                    None if row["execution_epoch"] is None else int(row["execution_epoch"]),
                )
                if self._authority() != row_authority:
                    self.queue.mark_applied(
                        packet_id,
                        claim_token,
                        {
                            "status": "authority_epoch_closed",
                            "broker_write": False,
                            "decider_queued": False,
                        },
                        now=datetime.now(UTC),
                    )
                    count += 1
                    continue
                self.store.state_set(key, value)
                effect: Mapping[str, object] = {"state_key": key}
                if (
                    role == AIRole.ADVERSARIAL_CRITIC.value
                    and str(row["lane"]) == Lane.SOL_CRITIC.value
                ):
                    critic_packet = decode_dataclass(DecisionPacketV2, row["packet"])
                    if (
                        critic_packet.packet_id != packet_id
                        or critic_packet.lane != str(row["lane"])
                        or critic_packet.packet_hash != str(row["packet_hash"])
                    ):
                        raise ValueError("claimed critic packet identity or hash is invalid")
                    decider_packet, gate_effect = gate_decider_with_matching_critic(
                        critic_packet, row["output"]
                    )
                    if decider_packet is not None:
                        try:
                            self.queue.queue(
                                decider_packet,
                                AIRole.PORTFOLIO_DECIDER,
                                authority_mode=str(row["authority_mode"]),
                                execution_epoch=(
                                    None
                                    if row["execution_epoch"] is None
                                    else int(row["execution_epoch"])
                                ),
                            )
                        except PermissionError:
                            gate_effect = {
                                **dict(gate_effect),
                                "decider_queued": False,
                                "reason": "authority_epoch_closed",
                            }
                    effect = {"state_key": key, **dict(gate_effect)}
                self.queue.mark_applied(
                    packet_id,
                    claim_token,
                    effect,
                    now=datetime.now(UTC),
                )
                count += 1
            except Exception:
                self.queue.release_apply_claim(
                    packet_id,
                    claim_token,
                    now=datetime.now(UTC),
                )
                raise
        return count

    def run_once(self, limit: int = 20) -> int:
        try:
            count = self._run_once(limit)
        except Exception as exc:
            self.store.heartbeat(
                "v2-role-apply",
                "error",
                {"error_type": type(exc).__name__, "real_money": False},
            )
            raise
        trading_state = self.store.state_get("trading_state", "LOCKED")
        self.store.heartbeat(
            "v2-role-apply",
            "healthy" if trading_state == "ACTIVE" else "halted",
            {
                "role_outputs_applied": count,
                "trading_state": trading_state,
                "real_money": False,
            },
        )
        return count

    def run_forever(self, interval_seconds: int = 5) -> None:
        ready()
        while True:
            try:
                self.run_once()
                watchdog()
            except Exception as exc:
                print(f"V2_ROLE_APPLY_ERROR={type(exc).__name__}", flush=True)
            time.sleep(max(1, interval_seconds))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Persist v2 regime/critic outputs for subsequent packets"
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--interval", type=int, default=5)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    worker = RoleApplyWorkerV2(args.config)
    try:
        if args.once:
            print(f"V2_ROLE_OUTPUTS_APPLIED={worker.run_once()}")
        else:
            worker.run_forever(args.interval)
    finally:
        worker.close()


if __name__ == "__main__":
    main()
