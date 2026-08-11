from __future__ import annotations

import argparse
import hashlib
import importlib.resources
import json
import os
import re

# subprocess is required for fixed argv; shell execution is never enabled.
import subprocess  # nosec B404
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .ai_v2 import AIRole, DecisionPacketV2
from .codec_v2 import decode_dataclass
from .roles_v2 import role_prompt

MODEL = "gpt-5.6-sol"
PROVIDER = "openai-chatgpt"
CODEX_NATIVE = Path(
    "/usr/lib/node_modules/@openai/codex/node_modules/@openai/codex-linux-x64/"
    "vendor/x86_64-unknown-linux-musl/bin/codex"
)
SSH_IDENTITY = Path("/opt/Mobiup/.ssh/id_ed25519_mobiup_primary_admin")
REMOTE_HOST = "andrei@server"
REMOTE_CONFIG = "/etc/etoro-agent/v2-demo.json"
REMOTE_DSN_FILE = "/etc/etoro-agent/postgres-v2-engine-dsn"
WORKER_ID = os.getenv("ETORO_V2_AI_WORKER_ID", "dell-sol-v2")
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
        REMOTE_HOST,
        command,
    )


def _remote_prefix() -> str:
    return (
        "sudo -n -u etoro-engine env "
        f"ETORO_V2_POSTGRES_DSN_FILE={REMOTE_DSN_FILE} "
        "/opt/etoro-v2/current/.venv/bin/python -m etoro_agent.ai_wire_v2 "
        f"--config {REMOTE_CONFIG} "
    )


def _run(command: tuple[str, ...], *, input_text: str | None = None, timeout: int = 120) -> str:
    # Fixed SSH/Codex argv; no local shell is enabled.
    completed = subprocess.run(  # nosec B603
        command,
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
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


def _schema_name(role: AIRole) -> str:
    return {
        AIRole.PORTFOLIO_DECIDER: "portfolio_decision_v2.schema.json",
        AIRole.MARKET_REGIME_ANALYST: "regime_v2.schema.json",
        AIRole.ADVERSARIAL_CRITIC: "critic_v2.schema.json",
    }[role]


def _usage(jsonl: str) -> dict[str, int | None]:
    result: dict[str, int | None] = {
        "input_tokens": None,
        "output_tokens": None,
        "reasoning_tokens": None,
    }
    for line in jsonl.splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if not isinstance(event, Mapping):
            raise ValueError("Codex emitted a non-object telemetry event")
        usage = event.get("usage")
        if not isinstance(usage, Mapping):
            continue
        for key, aliases in {
            "input_tokens": ("input_tokens", "inputTokens", "input"),
            "output_tokens": ("output_tokens", "outputTokens", "output"),
            "reasoning_tokens": ("reasoning_tokens", "reasoningTokens", "reasoning"),
        }.items():
            for alias in aliases:
                value = usage.get(alias)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    result[key] = value
                    break
    return result


def run_model(claim: Mapping[str, Any], role: AIRole) -> tuple[dict[str, Any], dict[str, Any]]:
    packet = decode_dataclass(DecisionPacketV2, claim["packet"])
    if packet.packet_hash != str(claim["packet_hash"]):
        raise ValueError("decision packet failed immutable hash verification")
    prompt = role_prompt(role, packet)
    prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()
    schema_resource = importlib.resources.files("etoro_agent").joinpath(_schema_name(role))
    runtime_base = os.getenv("XDG_RUNTIME_DIR", "/run/user/1000")
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix=f"etoro-v2-{role.value}-", dir=runtime_base) as folder:
        temp = Path(folder)
        codex_home = temp / "codex-home"
        home = temp / "home"
        codex_home.mkdir(mode=0o700)
        home.mkdir(mode=0o700)
        schema = temp / "schema.json"
        output = temp / "output.json"
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
            "--property=ProtectHome=tmpfs",
            "--property=PrivateTmp=yes",
            "--property=NoExecPaths=/",
            f"--property=ExecPaths={CODEX_NATIVE}",
            "--property=InaccessiblePaths=-/opt/Mobiup/.ssh",
            "--property=InaccessiblePaths=-/home/andrei/.ssh",
            f"--property=ReadWritePaths={temp}",
            f"--property=BindReadOnlyPaths=/home/andrei/.codex/auth.json:{codex_home}/auth.json",
            f"--working-directory={temp}",
            f"--setenv=HOME={home}",
            f"--setenv=CODEX_HOME={codex_home}",
            str(CODEX_NATIVE),
            "exec",
            "--model",
            MODEL,
            "--sandbox",
            "read-only",
            "--ephemeral",
            "--ignore-user-config",
            "--skip-git-repo-check",
            "--json",
            "--output-schema",
            str(schema),
            "--output-last-message",
            str(output),
            "-",
        )
        raw = _run(command, input_text=prompt, timeout=240)
        value = json.loads(output.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Codex v2 output is not a JSON object")
    elapsed = max(0, round((time.monotonic() - started) * 1000))
    usage = _usage(raw)
    run_id_seed = f"{claim['packet_hash']}:{role.value}:{claim['attempt']}:{prompt_hash}"
    run = {
        "run_id": f"v2-ai-run-{hashlib.sha256(run_id_seed.encode()).hexdigest()[:24]}",
        "status": "COMPLETED",
        "latency_ms": elapsed,
        "input_tokens": usage["input_tokens"],
        "output_tokens": usage["output_tokens"],
        "reasoning_tokens": usage["reasoning_tokens"],
        "error_type": None,
    }
    return value, {"prompt_hash": prompt_hash, "run": run}


def submit(
    claim: Mapping[str, Any], output: Mapping[str, Any], telemetry: Mapping[str, Any]
) -> None:
    envelope = {
        "packet_id": claim["packet_id"],
        "claim_token": claim["claim_token"],
        "output": dict(output),
        "model": MODEL,
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
    envelope = {
        "packet_id": claim["packet_id"],
        "claim_token": claim["claim_token"],
        "model": MODEL,
        "prompt_hash": prompt_hash,
        "run": {
            "run_id": f"v2-ai-run-{hashlib.sha256(run_id_seed.encode()).hexdigest()[:24]}",
            "status": "ERROR",
            "latency_ms": max(0, round((time.monotonic() - started) * 1000)),
            "input_tokens": None,
            "output_tokens": None,
            "reasoning_tokens": None,
            "error_type": type(exc).__name__,
        },
        "retryable": type(exc).__name__ in {"RuntimeError", "TimeoutExpired"},
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
