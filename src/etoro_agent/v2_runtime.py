from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from importlib.metadata import version

from .config_v2 import load_config_v2


def _json_default(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (set, frozenset, tuple)):
        return sorted(value)
    if hasattr(value, "value"):
        return value.value
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def _print(value: object) -> None:
    print(json.dumps(value, sort_keys=True, indent=2, default=_json_default))


def _config_report(config_path: str) -> dict[str, object]:
    config = load_config_v2(config_path)
    compatibility = config.compatibility()
    return {
        "schema_version": 2,
        "package_version": version("etoro-demo-agent"),
        "account_mode": config.account_mode,
        "real_money": False,
        "live_demo_execution_enabled": config.live_demo_execution_enabled,
        "canonical_store": "postgresql",
        "broker_writer": "etoro-v2-executor-postgres.service",
        "sqlite_authority": "research-replay-only",
        "capital_mandate": asdict(config.mandate),
        "compatibility": [asdict(item) for item in compatibility],
        "executable_profiles": sum(item.status.value == "EXECUTABLE" for item in compatibility),
        "shadow_only_profiles": sum(item.status.value == "SHADOW_ONLY" for item in compatibility),
        "invalid_profiles": sum(item.status.value == "INVALID" for item in compatibility),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "eToro Trading Lab canonical V2 inspection CLI. Broker writes and state mutations "
            "are available only through isolated PostgreSQL system services."
        )
    )
    parser.add_argument("--config", default="config/v2-demo.json")
    parser.add_argument("command", choices=("validate-config", "release-info"))
    args = parser.parse_args()

    report = _config_report(args.config)
    if args.command == "validate-config":
        report["status"] = "VALID"
    else:
        report = {
            "package_version": report["package_version"],
            "account_mode": report["account_mode"],
            "real_money": False,
            "canonical_store": report["canonical_store"],
            "broker_writer": report["broker_writer"],
            "sqlite_authority": report["sqlite_authority"],
        }
    _print(report)


if __name__ == "__main__":
    main()
