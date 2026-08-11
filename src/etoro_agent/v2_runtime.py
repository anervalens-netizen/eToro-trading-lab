from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from .config_v2 import load_config_v2
from .executor_v2 import DemoExecutionWorkerV2
from .kernel_v2 import UnifiedTradingKernel
from .postgres_store_v2 import PostgresStoreV2
from .risk_v2 import GlobalRiskKernel
from .runtime_store_v2 import RuntimeStoreV2


def _json_default(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    if hasattr(value, "value"):
        return value.value
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    return str(value)


def _print(value: object) -> None:
    print(json.dumps(value, sort_keys=True, indent=2, default=_json_default))


def _store(path: str) -> RuntimeStoreV2:
    return RuntimeStoreV2(Path(path))


def _config_report(config_path: str) -> dict[str, object]:
    config = load_config_v2(config_path)
    compatibility = config.compatibility()
    return {
        "schema_version": 2,
        "account_mode": config.account_mode,
        "real_money": False,
        "live_demo_execution_enabled": config.live_demo_execution_enabled,
        "websocket_enabled": config.websocket_enabled,
        "initial_cash_usd": config.initial_cash_usd,
        "capital_mandate": asdict(config.mandate),
        "compatibility": [asdict(item) for item in compatibility],
        "executable_profiles": sum(item.status.value == "EXECUTABLE" for item in compatibility),
        "shadow_only_profiles": sum(item.status.value == "SHADOW_ONLY" for item in compatibility),
        "invalid_profiles": sum(item.status.value == "INVALID" for item in compatibility),
    }


def _status(config_path: str, runtime_path: str) -> dict[str, object]:
    config = load_config_v2(config_path)
    store = _store(runtime_path)
    try:
        orders = store.db.execute("SELECT state_json FROM v2_broker_orders").fetchall()
        unknown = 0
        for row in orders:
            try:
                if json.loads(str(row[0])).get("status") == "UNKNOWN":
                    unknown += 1
            except json.JSONDecodeError:
                unknown += 1
        open_positions = store.positions(open_only=True)
        event_count = int(store.db.execute("SELECT COUNT(*) FROM v2_events").fetchone()[0])
        return {
            "schema_version": 2,
            "account_mode": config.account_mode,
            "real_money": False,
            "trading_state": store.state_get("trading_state", "LOCKED"),
            "audit_chain_valid": store.verify_event_chain(),
            "event_count": event_count,
            "open_positions": [asdict(item) for item in open_positions],
            "unknown_orders": unknown,
            "pending_outbox": len(store.pending_outbox()),
            "compatibility": [asdict(item) for item in config.compatibility()],
        }
    finally:
        store.close()


def _read_dsn(config_path: str) -> str:
    config = load_config_v2(config_path)
    override = os.getenv("ETORO_V2_POSTGRES_DSN_FILE")
    raw = override or config.postgres_dsn_file
    if not raw:
        raise RuntimeError("PostgreSQL DSN credential file is unavailable")
    path = Path(raw)
    if not path.is_file():
        raise RuntimeError("PostgreSQL DSN credential file is unavailable")
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError("PostgreSQL DSN credential file is empty")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(
        description="eToro Trading Lab v2 canonical DEMO runtime; REAL routes are unavailable"
    )
    parser.add_argument("--config", default="config/v2-demo.json")
    parser.add_argument("--runtime", default="runtime/v2.sqlite3")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate-config")
    sub.add_parser("status")
    sub.add_parser("postgres-migrate")
    executor_once = sub.add_parser("executor-once")
    executor_once.add_argument("--limit", type=int, default=20)
    executor_worker = sub.add_parser("executor-worker")
    executor_worker.add_argument("--interval", type=int, default=2)
    state = sub.add_parser("set-state")
    state.add_argument("state", choices=("ACTIVE", "HALT_NEW", "REDUCE_ONLY", "LOCKED"))
    state.add_argument("--confirm", default="")
    args = parser.parse_args()

    if args.command == "validate-config":
        _print(_config_report(args.config))
        return
    if args.command == "status":
        _print(_status(args.config, args.runtime))
        return
    if args.command == "postgres-migrate":
        store = PostgresStoreV2.from_dsn(_read_dsn(args.config))
        try:
            store.migrate()
            _print({"status": "MIGRATED", "event_chain_valid": store.verify_event_chain()})
        finally:
            store.close()
        return
    if args.command == "set-state":
        if args.state == "ACTIVE" and args.confirm != "ACTIVATE_DEMO_V2":
            raise SystemExit("ACTIVE requires --confirm ACTIVATE_DEMO_V2")
        store = _store(args.runtime)
        try:
            store.set_trading_state(
                args.state,
                actor="v2-cli-owner",
                reason="explicit DEMO v2 state command",
            )
            _print({"trading_state": args.state, "real_money": False})
        finally:
            store.close()
        return

    config = load_config_v2(args.config)
    store = _store(args.runtime)
    kernel = UnifiedTradingKernel(store, GlobalRiskKernel(config.mandate))
    worker = DemoExecutionWorkerV2(config, store, kernel)
    if args.command == "executor-once":
        try:
            _print({"processed": worker.run_once(args.limit), "real_money": False})
        finally:
            store.close()
    else:
        worker.run_forever(args.interval)


if __name__ == "__main__":
    main()
