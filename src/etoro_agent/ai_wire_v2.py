from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

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


def _store(config_path: str) -> tuple[PostgresRuntimeStoreV2, CanonicalPostgresAIStoreV2]:
    store = PostgresRuntimeStoreV2.from_dsn(_dsn(config_path))
    store.migrate()
    return store, CanonicalPostgresAIStoreV2(store)


def _wire_run(value: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "run_id", "status", "latency_ms", "input_tokens", "output_tokens",
        "reasoning_tokens", "error_type",
    }
    if set(value) != required:
        raise ValueError("AI run telemetry does not match strict wire schema")
    return dict(value)


def main() -> None:
    parser = argparse.ArgumentParser(description="Credential-free wire surface for remote v2 AI workers")
    parser.add_argument("--config", default="config/v2-demo.json")
    sub = parser.add_subparsers(dest="command", required=True)
    claim = sub.add_parser("claim")
    claim.add_argument("--role", choices=tuple(role.value for role in AIRole), required=True)
    claim.add_argument("--worker-id", required=True)
    claim.add_argument("--lease-seconds", type=int, default=300)
    claim.add_argument("--daily-cap", type=int, default=0)
    sub.add_parser("submit")
    sub.add_parser("error")
    decided = sub.add_parser("decided")
    decided.add_argument("--limit", type=int, default=20)
    applied = sub.add_parser("mark-applied")
    applied.add_argument("--packet-id", required=True)
    args = parser.parse_args()

    store, queue = _store(args.config)
    try:
        if args.command == "claim":
            result = queue.claim(
                args.worker_id,
                AIRole(args.role),
                now=datetime.now(timezone.utc),
                lease_seconds=args.lease_seconds,
                daily_cap=None if args.daily_cap <= 0 else args.daily_cap,
            )
            print("null" if result is None else json.dumps(result, sort_keys=True, separators=(",", ":"), default=str))
            return
        if args.command == "decided":
            print(json.dumps(queue.decided(args.limit), sort_keys=True, separators=(",", ":"), default=str))
            return
        if args.command == "mark-applied":
            queue.mark_applied(args.packet_id, now=datetime.now(timezone.utc))
            print(json.dumps({"status": "APPLIED", "packet_id": args.packet_id}, sort_keys=True))
            return
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("AI wire payload must be an object")
        if args.command == "submit":
            required = {"packet_id", "claim_token", "output", "model", "prompt_hash", "run"}
            if set(payload) != required or not isinstance(payload["output"], Mapping) or not isinstance(payload["run"], Mapping):
                raise ValueError("AI submit envelope does not match strict schema")
            queue.submit(
                str(payload["packet_id"]),
                str(payload["claim_token"]),
                dict(payload["output"]),
                model=str(payload["model"]),
                prompt_hash=str(payload["prompt_hash"]),
                run=_wire_run(payload["run"]),
                now=datetime.now(timezone.utc),
            )
            print(json.dumps({"status": "DECIDED", "packet_id": payload["packet_id"]}, sort_keys=True))
            return
        required = {"packet_id", "claim_token", "model", "prompt_hash", "run", "retryable"}
        if set(payload) != required or not isinstance(payload["run"], Mapping):
            raise ValueError("AI error envelope does not match strict schema")
        queue.fail(
            str(payload["packet_id"]),
            str(payload["claim_token"]),
            model=str(payload["model"]),
            prompt_hash=str(payload["prompt_hash"]),
            run=_wire_run(payload["run"]),
            retryable=bool(payload["retryable"]),
            now=datetime.now(timezone.utc),
        )
        print(json.dumps({"status": "ERROR_RECORDED", "packet_id": payload["packet_id"]}, sort_keys=True))
    finally:
        store.close()


if __name__ == "__main__":
    main()
