from __future__ import annotations

import argparse
import os
from pathlib import Path

from .config_v2 import load_config_v2
from .executor_current_v2 import DemoExecutionWorkerCurrentV2
from .kernel_v2 import UnifiedTradingKernel
from .postgres_runtime_v2 import PostgresRuntimeStoreV2
from .risk_v2 import GlobalRiskKernel


def _dsn(config_path: str) -> str:
    config = load_config_v2(config_path)
    path = os.getenv("ETORO_V2_POSTGRES_DSN_FILE") or config.postgres_dsn_file
    if not path:
        raise RuntimeError("PostgreSQL DSN credential file is required")
    value = Path(path).read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError("PostgreSQL DSN credential file is empty")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Production PostgreSQL-backed eToro DEMO executor v2"
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--interval", type=int, default=2)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    config = load_config_v2(args.config)
    store = PostgresRuntimeStoreV2.from_dsn(_dsn(args.config))
    store.migrate()
    kernel = UnifiedTradingKernel(store, GlobalRiskKernel(config.mandate))  # type: ignore[arg-type]
    worker = DemoExecutionWorkerCurrentV2(config, store, kernel)  # type: ignore[arg-type]
    try:
        if args.once:
            print(f"V2_DEMO_EXECUTED={worker.run_once()}")
        else:
            worker.run_forever(args.interval)
    finally:
        store.close()


if __name__ == "__main__":
    main()
