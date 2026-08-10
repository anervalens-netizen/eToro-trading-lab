from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from .ai_store_postgres_v2 import CanonicalPostgresAIStoreV2
from .ai_v2 import AIRole
from .config_v2 import load_config_v2
from .postgres_runtime_v2 import PostgresRuntimeStoreV2


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
        self.store.migrate()
        self.queue = CanonicalPostgresAIStoreV2(self.store)

    def close(self) -> None:
        self.store.close()

    def run_once(self, limit: int = 20) -> int:
        count = 0
        for row in self.queue.decided(limit):
            role = str(row["role"])
            if role == AIRole.PORTFOLIO_DECIDER.value:
                continue
            if role not in {AIRole.MARKET_REGIME_ANALYST.value, AIRole.ADVERSARIAL_CRITIC.value}:
                continue
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
            self.store.state_set(key, value)
            self.queue.mark_applied(str(row["packet_id"]), now=datetime.now(timezone.utc))
            count += 1
        return count

    def run_forever(self, interval_seconds: int = 5) -> None:
        while True:
            try:
                self.run_once()
            except Exception:
                pass
            time.sleep(max(1, interval_seconds))


def main() -> None:
    parser = argparse.ArgumentParser(description="Persist v2 regime/critic outputs for subsequent packets")
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
