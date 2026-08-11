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
