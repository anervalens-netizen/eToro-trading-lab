from __future__ import annotations

import argparse
import importlib.resources
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any


MODEL = "gpt-5.6-sol"
SSH_IDENTITY = Path("/opt/Mobiup/.ssh/id_ed25519_mobiup_primary_admin")
SSH_BASE = (
    "ssh",
    "-i",
    str(SSH_IDENTITY),
    "-o",
    "IdentitiesOnly=yes",
    "-o",
    "BatchMode=yes",
    "andrei@server",
)
REMOTE_PENDING = (
    "sudo -n -u etoro-agent /opt/eToro/.venv/bin/etoro-agent "
    "--config /opt/eToro/config/demo.json --runtime /var/lib/etoro-agent "
    "ai-pending --limit 5"
)
REMOTE_DECIDE = (
    "sudo -n -u etoro-agent /opt/eToro/.venv/bin/etoro-agent "
    "--config /opt/eToro/config/demo.json --runtime /var/lib/etoro-agent "
    "ai-decide-stdin"
)


def _run(command: tuple[str, ...], *, input_text: str | None = None, timeout: int = 120) -> str:
    completed = subprocess.run(
        command,
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
        env={**os.environ, "NO_COLOR": "1"},
    )
    if completed.returncode != 0:
        detail = " ".join(completed.stderr.strip().splitlines())[-500:]
        raise RuntimeError(
            f"command failed with exit {completed.returncode}: {detail or 'no diagnostic'}"
        )
    return completed.stdout


def fetch_pending() -> tuple[dict[str, Any], ...]:
    raw = _run((*SSH_BASE, REMOTE_PENDING), timeout=30)
    value = json.loads(raw)
    if not isinstance(value, list):
        raise ValueError("primary AI queue returned a non-list payload")
    return tuple(item for item in value if isinstance(item, dict))


def _prompt(packet: dict[str, Any]) -> str:
    return (
        "You are the bounded portfolio manager for an eToro PAPER/DEMO research account. "
        "Return exactly one JSON object matching the supplied schema. Decide OPEN, CLOSE, or HOLD. "
        "For OPEN, candidate_id must exactly match one supplied candidate. CLOSE is allowed only "
        "when mode is POSITION_REVIEW and position is non-null. Never invent a candidate, symbol, "
        "price, or fact. Prefer HOLD when edge, data quality, market status, or cost margin is weak. "
        "Do not use tools, browse, read files, or run commands. This is a short-horizon research "
        "decision, not a request to maximize trade count. Deterministic risk checks run after you.\n\n"
        f"DECISION_PACKET={json.dumps(packet, sort_keys=True, separators=(',', ':'))}"
    )


def _validate(packet: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    action = str(decision.get("action", "")).upper()
    candidate_id = str(decision.get("candidate_id", ""))
    if action not in {"OPEN", "CLOSE", "HOLD"}:
        raise ValueError("Sol returned an unsupported action")
    candidates = {
        str(item.get("candidate_id"))
        for item in packet.get("payload", {}).get("candidates", [])
        if isinstance(item, dict)
    }
    if action == "OPEN" and candidate_id not in candidates:
        raise ValueError("Sol OPEN did not bind to an exact candidate")
    payload = packet.get("payload", {})
    if action == "CLOSE" and (
        payload.get("mode") != "POSITION_REVIEW" or payload.get("position") is None
    ):
        raise ValueError("Sol CLOSE is not bound to an open position")
    confidence = float(decision.get("confidence", -1))
    reason_codes = decision.get("reason_codes")
    rationale = str(decision.get("rationale", ""))
    if not 0 <= confidence <= 1:
        raise ValueError("Sol confidence is outside [0,1]")
    if (
        not isinstance(reason_codes, list)
        or not reason_codes
        or len(reason_codes) > 8
        or any(not isinstance(code, str) or not code or len(code) > 64 for code in reason_codes)
    ):
        raise ValueError("Sol reason codes are invalid")
    if not rationale or len(rationale) > 1000:
        raise ValueError("Sol rationale is invalid")
    return {
        "packet_id": str(packet["packet_id"]),
        "packet_hash": str(packet["packet_hash"]),
        "action": action,
        "candidate_id": candidate_id if action == "OPEN" else "",
        "confidence": confidence,
        "reason_codes": reason_codes,
        "rationale": rationale,
        "model": MODEL,
    }


def decide(packet: dict[str, Any]) -> dict[str, Any]:
    schema_resource = importlib.resources.files("etoro_agent").joinpath(
        "sol_decision.schema.json"
    )
    runtime_base = os.getenv("XDG_RUNTIME_DIR", "/run/user/1000")
    with tempfile.TemporaryDirectory(prefix="etoro-sol-", dir=runtime_base) as folder:
        temp = Path(folder)
        schema = temp / "schema.json"
        output = temp / "decision.json"
        schema.write_text(schema_resource.read_text(encoding="utf-8"), encoding="utf-8")
        command = (
            "sudo",
            "-n",
            "systemd-run",
            "--pipe",
            "--wait",
            "--collect",
            "--quiet",
            "--property=User=andrei",
            "--property=Group=andrei",
            "--property=NoNewPrivileges=yes",
            "--property=ProtectSystem=strict",
            "--property=ProtectHome=read-only",
            "--property=PrivateTmp=yes",
            "--property=InaccessiblePaths=/opt/Mobiup/.ssh",
            "--property=InaccessiblePaths=/home/andrei/.ssh",
            f"--property=ReadWritePaths={temp}",
            "--property=ReadWritePaths=/home/andrei/.codex",
            f"--working-directory={temp}",
            "--setenv=HOME=/home/andrei",
            "--setenv=CODEX_HOME=/home/andrei/.codex",
            "/usr/bin/codex",
            "exec",
            "--model",
            MODEL,
            "--sandbox",
            "read-only",
            "--ephemeral",
            "--ignore-user-config",
            "--skip-git-repo-check",
            "--output-schema",
            str(schema),
            "--output-last-message",
            str(output),
            "-",
        )
        _run(command, input_text=_prompt(packet), timeout=240)
        value = json.loads(output.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Sol output is not a JSON object")
        return _validate(packet, value)


def submit(decision: dict[str, Any]) -> None:
    _run(
        (*SSH_BASE, REMOTE_DECIDE),
        input_text=json.dumps(decision, sort_keys=True, separators=(",", ":")),
        timeout=30,
    )


def run_once() -> int:
    count = 0
    for packet in fetch_pending():
        submit(decide(packet))
        count += 1
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description="Sandboxed ChatGPT-authenticated Sol decision runner")
    parser.add_argument("--interval", type=int, default=120)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.once:
        print(f"SOL_DECISIONS={run_once()}")
        return
    if args.interval < 30:
        raise SystemExit("Sol polling interval must be at least 30 seconds")
    while True:
        try:
            count = run_once()
            print(f"SOL_DECISIONS={count}", flush=True)
        except Exception as exc:
            print(f"SOL_RUNNER_ERROR={type(exc).__name__}", flush=True)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
