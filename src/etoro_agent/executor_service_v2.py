from __future__ import annotations

import argparse
import os
from pathlib import Path

from .config_v2 import load_config_v2
from .executor_current_v2 import DemoExecutionWorkerCurrentV2
from .kernel_v2 import UnifiedTradingKernel
from .risk import load_public_verifying_key
from .risk_seal_v2 import RiskCommandVerifierV2, risk_mandate_hash
from .risk_v2 import GlobalRiskKernel
from .runtime_store_v2 import RuntimeStoreV2


def main() -> None:
    parser = argparse.ArgumentParser(description="Current autonomous eToro DEMO executor v2")
    parser.add_argument("--config", required=True)
    parser.add_argument("--runtime", default="/var/lib/etoro-agent/v2.sqlite3")
    parser.add_argument("--interval", type=int, default=2)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    config = load_config_v2(args.config)
    verifying_key_path = os.getenv("ETORO_V2_RISK_VERIFYING_KEY_FILE", "")
    if not verifying_key_path:
        raise RuntimeError("v2 executor risk verifying key is required")
    store = RuntimeStoreV2(Path(args.runtime))
    kernel = UnifiedTradingKernel(store, GlobalRiskKernel(config.mandate))
    verifier = RiskCommandVerifierV2(
        load_public_verifying_key(verifying_key_path),
        expected_risk_config_hash=risk_mandate_hash(config.mandate),
        allowed_sources=config.allowed_proposal_sources,
    )
    worker = DemoExecutionWorkerCurrentV2(config, store, kernel, verifier=verifier)
    try:
        if args.once:
            print(f"V2_DEMO_EXECUTED={worker.run_once()}")
        else:
            worker.run_forever(args.interval)
    finally:
        store.close()


if __name__ == "__main__":
    main()
