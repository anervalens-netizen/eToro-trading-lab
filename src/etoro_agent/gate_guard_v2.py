from __future__ import annotations

import argparse
from pathlib import Path

from .execution_gate_v2 import execution_gate_path, execution_gate_present
from .postgres_runtime_v2 import PostgresRuntimeStoreV2


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fail-closed PostgreSQL state transition after DEMO gate removal"
    )
    parser.add_argument("--dsn-file", required=True)
    args = parser.parse_args()
    gate = execution_gate_path()
    if execution_gate_present(gate):
        print("V2_GATE_GUARD=noop_present")
        return
    dsn = Path(args.dsn_file).read_text(encoding="utf-8").strip()
    if not dsn:
        raise RuntimeError("PostgreSQL DSN credential file is empty")
    store = PostgresRuntimeStoreV2.from_dsn(dsn)
    try:
        store.require_schema()
        invalidated = store.lock_and_invalidate_unstarted(
            actor="v2-execution-gate-path",
            reason="execution gate removed; broker writers isolated",
        )
        print(f"V2_GATE_GUARD=locked invalidated={invalidated}")
    finally:
        store.close()


if __name__ == "__main__":
    main()
