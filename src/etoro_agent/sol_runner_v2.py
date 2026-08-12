from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .ai_v2 import AIRole, DecisionPacketV2
from .bounded_subprocess_v2 import run_bounded
from .codec_v2 import decode_dataclass
from .roles_v2 import role_prompt
from .sol_model_service_v2 import (
    MODEL,
    IsolatedModelError,
    IsolatedModelTransportError,
    SolModelClientV2,
)

PROVIDER = "openai-chatgpt"
SSH_IDENTITY = Path("/opt/Mobiup/.ssh/id_ed25519_mobiup_primary_admin")
SSH_KNOWN_HOSTS = Path("/run/etoro-v2-sol-runner-known-hosts")
REMOTE_HOST = "andrei@server"
REMOTE_WIRE_UNIT = "etoro-v2-ai-wire"
REMOTE_CREDENTIAL_DIRECTORY = f"/run/credentials/{REMOTE_WIRE_UNIT}.service"
REMOTE_CONFIG = f"{REMOTE_CREDENTIAL_DIRECTORY}/v2-demo.json"
REMOTE_DSN_FILE = f"{REMOTE_CREDENTIAL_DIRECTORY}/postgres-v2-dsn"
WORKER_ID = os.getenv("ETORO_V2_AI_WORKER_ID", "dell-sol-v2")
MAX_COMMAND_OUTPUT_BYTES = 1024 * 1024
if re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", WORKER_ID) is None:
    raise RuntimeError("v2 AI worker id is invalid")


def _ssh(command: str) -> tuple[str, ...]:
    return (
        "ssh",
        "-i",
        str(SSH_IDENTITY),
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={SSH_KNOWN_HOSTS}",
        "-o",
        "GlobalKnownHostsFile=/dev/null",
        REMOTE_HOST,
        command,
    )


def _remote_prefix() -> str:
    return (
        "sudo -n systemd-run --wait --pipe --collect --quiet "
        f"--unit={REMOTE_WIRE_UNIT} "
        "--property=User=etoro-ai --property=Group=etoro-ai "
        "--property=NoNewPrivileges=yes --property=PrivateNetwork=yes "
        "--property=ProtectSystem=strict --property=ProtectHome=yes "
        "--property=PrivateTmp=yes --property=RestrictAddressFamilies=AF_UNIX "
        "--property=LoadCredential=postgres-v2-dsn:/etc/etoro-agent/postgres-v2-ai-dsn "
        "--property=LoadCredential=v2-demo.json:/etc/etoro-agent/v2-demo.json "
        f"--setenv=ETORO_V2_POSTGRES_DSN_FILE={REMOTE_DSN_FILE} "
        "/opt/etoro-v2/current/.venv/bin/python -m etoro_agent.ai_wire_v2 "
        f"--config {REMOTE_CONFIG} "
    )


def _run(command: tuple[str, ...], *, input_text: str | None = None, timeout: int = 120) -> str:
    # Fixed SSH/Codex argv; no local shell is enabled.
    completed = run_bounded(
        command,
        input_text=input_text,
        timeout=timeout,
        max_output_bytes=MAX_COMMAND_OUTPUT_BYTES,
        env={**os.environ, "NO_COLOR": "1"},
    )
    if completed.returncode != 0:
        detail = " ".join((completed.stderr.strip() or completed.stdout.strip()).splitlines())[
            -600:
        ]
        raise RuntimeError(
            f"command failed with exit {completed.returncode}: {detail or 'no diagnostic'}"
        )
    return completed.stdout


def fetch_claim(role: AIRole, daily_cap: int | None = None) -> dict[str, Any] | None:
    cap = "" if daily_cap is None else f" --daily-cap {daily_cap}"
    command = (
        _remote_prefix()
        + f"claim --role {role.value} --worker-id {WORKER_ID} --lease-seconds 300{cap}"
    )
    raw = _run(_ssh(command), timeout=30).strip()
    if raw in {"", "null"}:
        return None
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("remote v2 AI claim is not an object")
    required = {
        "packet_id",
        "packet_hash",
        "packet",
        "role",
        "lane",
        "attempt",
        "claim_token",
        "expires_at",
    }
    if set(value) != required:
        raise ValueError("remote v2 AI claim does not match strict schema")
    return value


def remote_status() -> dict[str, Any]:
    raw = _run(_ssh(_remote_prefix() + "status"), timeout=30).strip()
    value = json.loads(raw)
    required = {
        "commit",
        "release_bundle_sha256",
        "schema_version",
        "server_version",
        "session_user",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("remote v2 AI status does not match strict schema")
    if value["session_user"] != "etoro-ai":
        raise PermissionError("remote v2 AI role identity mismatch")
    return value


def run_model(claim: Mapping[str, Any], role: AIRole) -> tuple[dict[str, Any], dict[str, Any]]:
    return SolModelClientV2().evaluate(claim, role)


def submit(
    claim: Mapping[str, Any], output: Mapping[str, Any], telemetry: Mapping[str, Any]
) -> None:
    envelope = {
        "packet_id": claim["packet_id"],
        "claim_token": claim["claim_token"],
        "output": dict(output),
        "model": telemetry["attested_model_id"],
        "prompt_hash": telemetry["prompt_hash"],
        "run": telemetry["run"],
    }
    _run(
        _ssh(_remote_prefix() + "submit"),
        input_text=json.dumps(envelope, sort_keys=True, separators=(",", ":")),
        timeout=30,
    )


def submit_error(claim: Mapping[str, Any], role: AIRole, exc: Exception, started: float) -> None:
    packet = decode_dataclass(DecisionPacketV2, claim["packet"])
    prompt_hash = hashlib.sha256(role_prompt(role, packet).encode()).hexdigest()
    run_id_seed = f"{claim['packet_hash']}:{role.value}:{claim['attempt']}:{prompt_hash}:ERROR"
    error_type = exc.error_type if isinstance(exc, IsolatedModelError) else type(exc).__name__
    retryable = (
        exc.retryable
        if isinstance(exc, IsolatedModelError)
        else isinstance(exc, IsolatedModelTransportError)
    )
    envelope = {
        "packet_id": claim["packet_id"],
        "claim_token": claim["claim_token"],
        "model": (
            exc.attested_model_id
            if isinstance(exc, IsolatedModelError) and exc.attested_model_id
            else f"{MODEL}:not-invoked-or-unattested"
        ),
        "prompt_hash": prompt_hash,
        "run": {
            "run_id": f"v2-ai-run-{hashlib.sha256(run_id_seed.encode()).hexdigest()[:24]}",
            "status": "ERROR",
            "latency_ms": max(0, round((time.monotonic() - started) * 1000)),
            "input_tokens": None,
            "output_tokens": None,
            "reasoning_tokens": None,
            "error_type": error_type,
        },
        "retryable": retryable,
    }
    _run(
        _ssh(_remote_prefix() + "error"),
        input_text=json.dumps(envelope, sort_keys=True, separators=(",", ":")),
        timeout=30,
    )


def run_once(role: AIRole, daily_cap: int | None = None) -> int:
    claim = fetch_claim(role, daily_cap)
    if claim is None:
        return 0
    started = time.monotonic()
    try:
        output, telemetry = run_model(claim, role)
        submit(claim, output, telemetry)
    except Exception as exc:
        try:
            submit_error(claim, role, exc, started)
        except Exception as submit_exc:
            print(
                f"V2_AI_ERROR_REPORT_FAILED={type(submit_exc).__name__}",
                flush=True,
            )
        return 0
    return 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Stateless ChatGPT-authenticated v2 trading research runner"
    )
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--daily-cap", type=int, default=0)
    args = parser.parse_args()
    if args.interval < 30:
        raise SystemExit("v2 AI poll interval must be at least 30 seconds")
    cap = None if args.daily_cap <= 0 else args.daily_cap
    roles = (
        AIRole.MARKET_REGIME_ANALYST,
        AIRole.ADVERSARIAL_CRITIC,
        AIRole.PORTFOLIO_DECIDER,
    )
    if args.once:
        print(" ".join(f"{role.value}={run_once(role, cap)}" for role in roles))
        return
    while True:
        for role in roles:
            try:
                run_once(role, cap)
            except Exception as exc:
                print(
                    f"V2_AI_RUNNER_ERROR={role.value}:{type(exc).__name__}",
                    flush=True,
                )
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
