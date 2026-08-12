from __future__ import annotations

import os
from pathlib import Path

DEFAULT_EXECUTION_GATE = Path("/etc/etoro-v2-control/ENABLE_DEMO_EXECUTION")


def execution_gate_path() -> Path:
    override = os.getenv("ETORO_V2_EXECUTION_GATE_FILE", "").strip()
    return Path(override) if override else DEFAULT_EXECUTION_GATE


def execution_gate_present(path: Path | None = None) -> bool:
    candidate = path or execution_gate_path()
    return candidate.is_file() and not candidate.is_symlink()


def authority_for_state(
    trading_state: str,
    trading_state_version: int,
    *,
    execution_gate: bool,
) -> tuple[str, int | None] | None:
    """Resolve the only packet authority valid for the current control epoch."""

    if trading_state_version < 0:
        raise ValueError("trading state version cannot be negative")
    if trading_state == "LOCKED" and not execution_gate:
        return ("SHADOW", None)
    if trading_state == "ACTIVE" and execution_gate and trading_state_version >= 1:
        return ("EXECUTION", trading_state_version)
    return None
