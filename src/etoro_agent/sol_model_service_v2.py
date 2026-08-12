from __future__ import annotations

import hashlib
import importlib.resources
import json
import os
import re
import socket
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

from .ai_v2 import AIRole, DecisionPacketV2
from .bounded_subprocess_v2 import run_bounded
from .codec_v2 import decode_dataclass
from .codex_auth_attestation_v2 import CodexAuthAttestationV2, attest_chatgpt_codex
from .roles_v2 import role_prompt

MODEL = "gpt-5.6-sol"
CODEX_NATIVE = Path(
    "/usr/lib/node_modules/@openai/codex/node_modules/@openai/codex-linux-x64/"
    "vendor/x86_64-unknown-linux-musl/bin/codex"
)
MODEL_SOCKET = Path("/run/etoro-v2-sol-model.sock")
PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 512 * 1024
MAX_MODEL_OUTPUT_BYTES = 1024 * 1024
RETRYABLE_MODEL_ERROR_TYPES = frozenset({"RuntimeError", "TimeoutExpired"})


def _reject_nonfinite_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def _reject_model_booleans(value: Any) -> None:
    if isinstance(value, bool):
        raise ValueError("Codex v2 output contains a boolean where strict model output is required")
    if isinstance(value, Mapping):
        for child in value.values():
            _reject_model_booleans(child)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            _reject_model_booleans(child)


def _parse_model_output(raw: str) -> dict[str, Any]:
    value = json.loads(
        raw,
        parse_float=Decimal,
        parse_constant=_reject_nonfinite_json_constant,
    )
    if not isinstance(value, dict):
        raise ValueError("Codex v2 output is not a JSON object")
    _reject_model_booleans(value)
    return value


def _encode_lossless_json(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non-finite Decimal cannot cross the model IPC boundary")
        return str(value)
    if isinstance(value, float):
        return json.dumps(value, allow_nan=False, separators=(",", ":"))
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("model IPC object keys must be strings")
        return (
            "{"
            + ",".join(
                f"{json.dumps(key, ensure_ascii=False)}:{_encode_lossless_json(value[key])}"
                for key in sorted(value)
            )
            + "}"
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return "[" + ",".join(_encode_lossless_json(child) for child in value) + "]"
    raise TypeError(f"unsupported model IPC JSON type: {type(value).__name__}")


def _model_attestation() -> CodexAuthAttestationV2:
    auth_path = Path(os.getenv("CODEX_HOME", "")) / "auth.json"
    account_hash_path = os.getenv("ETORO_V2_CODEX_ACCOUNT_HASH_FILE", "")
    executable_hash_path = os.getenv("ETORO_V2_CODEX_EXECUTABLE_HASH_FILE", "")
    if not account_hash_path or not executable_hash_path:
        raise PermissionError("Codex deployment attestation credentials are unavailable")
    return attest_chatgpt_codex(
        auth_path=auth_path,
        executable_path=CODEX_NATIVE,
        trusted_account_hash_path=account_hash_path,
        trusted_executable_hash_path=executable_hash_path,
        model=MODEL,
    )


class IsolatedModelError(Exception):
    def __init__(self, error_type: str, *, attested_model_id: str | None = None) -> None:
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", error_type) is None:
            raise ValueError("isolated model error type is invalid")
        self.error_type = error_type
        self.retryable = error_type in RETRYABLE_MODEL_ERROR_TYPES
        self.attested_model_id = attested_model_id
        super().__init__(f"isolated model failed: {error_type}")


class IsolatedModelTransportError(RuntimeError):
    """Retryable failure limited to the local AF_UNIX request boundary."""


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


def _run(command: tuple[str, ...], *, input_text: str, timeout: int = 240) -> str:
    completed = run_bounded(
        command,
        input_text=input_text,
        timeout=timeout,
        max_output_bytes=MAX_MODEL_OUTPUT_BYTES,
        env={**os.environ, "NO_COLOR": "1"},
    )
    if completed.returncode != 0:
        raise RuntimeError(f"isolated model exited with status {completed.returncode}")
    return completed.stdout


def model_command(schema: Path, output: Path, working_directory: Path) -> tuple[str, ...]:
    return (
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
        "--cd",
        str(working_directory),
        "--output-schema",
        str(schema),
        "--output-last-message",
        str(output),
        "-",
    )


def evaluate_claim(
    claim: Mapping[str, Any],
    role: AIRole,
    *,
    run_command: Callable[..., str] = _run,
) -> tuple[dict[str, Any], dict[str, Any]]:
    packet = decode_dataclass(DecisionPacketV2, claim["packet"])
    if packet.packet_hash != str(claim["packet_hash"]):
        raise ValueError("decision packet failed immutable hash verification")
    if str(claim.get("role", "")) != role.value:
        raise ValueError("model request role does not match the claimed packet")
    auth_attestation = _model_attestation()
    prompt = role_prompt(role, packet)
    prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()
    schema_resource = importlib.resources.files("etoro_agent").joinpath(_schema_name(role))
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix=f"etoro-v2-{role.value}-") as folder:
        temp = Path(folder)
        schema = temp / "schema.json"
        output = temp / "output.json"
        schema.write_text(schema_resource.read_text(encoding="utf-8"), encoding="utf-8")
        raw = run_command(model_command(schema, output, temp), input_text=prompt, timeout=240)
        value = _parse_model_output(output.read_text(encoding="utf-8"))
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
    attested_model_id = ":".join(
        (
            MODEL,
            auth_attestation.auth_mode,
            auth_attestation.account_id_sha256,
            auth_attestation.executable_sha256,
            "no-platform-fallback",
        )
    )
    return value, {
        "prompt_hash": prompt_hash,
        "run": run,
        "attested_model_id": attested_model_id,
    }


def process_request(
    request: Mapping[str, Any],
    *,
    evaluator: Callable[[Mapping[str, Any], AIRole], tuple[dict[str, Any], dict[str, Any]]] = (
        evaluate_claim
    ),
) -> dict[str, Any]:
    if set(request) != {"protocol_version", "role", "claim"}:
        raise ValueError("isolated model request does not match the strict schema")
    if request.get("protocol_version") != PROTOCOL_VERSION or not isinstance(
        request.get("claim"), Mapping
    ):
        raise ValueError("isolated model request protocol is invalid")
    role = AIRole(str(request["role"]))
    claim = dict(request["claim"])
    if str(claim.get("role", "")) != role.value:
        raise ValueError("isolated model request role mismatch")
    output, telemetry = evaluator(claim, role)
    if not isinstance(output, dict) or set(telemetry) != {
        "prompt_hash",
        "run",
        "attested_model_id",
    }:
        raise ValueError("isolated model result does not match the strict schema")
    return {
        "protocol_version": PROTOCOL_VERSION,
        "output": output,
        "telemetry": telemetry,
    }


def _read_frame(stream: Any, *, lossless_numbers: bool = False) -> dict[str, Any]:
    raw = stream.readline(MAX_FRAME_BYTES + 1)
    if not raw or len(raw) > MAX_FRAME_BYTES or not raw.endswith(b"\n"):
        raise ValueError("isolated model request frame is invalid")
    if stream.read(1):
        raise ValueError("isolated model request must contain exactly one frame")
    parse_options: dict[str, Any] = {"parse_constant": _reject_nonfinite_json_constant}
    if lossless_numbers:
        parse_options["parse_float"] = Decimal
    value = json.loads(raw, **parse_options)
    if not isinstance(value, dict):
        raise ValueError("isolated model request must be an object")
    return value


def _write_frame(stream: Any, value: Mapping[str, Any]) -> None:
    raw = _encode_lossless_json(value).encode("utf-8") + b"\n"
    if len(raw) > MAX_FRAME_BYTES:
        raise ValueError("isolated model response exceeds the frame limit")
    stream.write(raw)
    stream.flush()


class SolModelClientV2:
    def __init__(
        self, socket_path: str | Path = MODEL_SOCKET, *, timeout_seconds: float = 270
    ) -> None:
        self.socket_path = str(socket_path)
        self.timeout_seconds = timeout_seconds

    def evaluate(
        self, claim: Mapping[str, Any], role: AIRole
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        request = {
            "protocol_version": PROTOCOL_VERSION,
            "role": role.value,
            "claim": dict(claim),
        }
        raw = _encode_lossless_json(request).encode("utf-8") + b"\n"
        if len(raw) > MAX_FRAME_BYTES:
            raise ValueError("isolated model request exceeds the frame limit")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(self.timeout_seconds)
                client.connect(self.socket_path)
                client.sendall(raw)
                client.shutdown(socket.SHUT_WR)
                with client.makefile("rb") as stream:
                    response = _read_frame(stream, lossless_numbers=True)
        except OSError as exc:
            raise IsolatedModelTransportError("isolated model transport unavailable") from exc
        if response.get("protocol_version") != PROTOCOL_VERSION:
            raise RuntimeError("isolated model response protocol is invalid")
        if set(response) in (
            {"protocol_version", "error_type"},
            {"protocol_version", "error_type", "attested_model_id"},
        ):
            error_type = response["error_type"]
            if not isinstance(error_type, str):
                raise RuntimeError("isolated model error response schema is invalid")
            attested_model_id = response.get("attested_model_id")
            if attested_model_id is not None and not isinstance(attested_model_id, str):
                raise RuntimeError("isolated model attestation response is invalid")
            raise IsolatedModelError(error_type, attested_model_id=attested_model_id)
        if (
            set(response) != {"protocol_version", "output", "telemetry"}
            or not isinstance(response.get("output"), dict)
            or not isinstance(response.get("telemetry"), dict)
            or set(response["telemetry"]) != {"prompt_hash", "run", "attested_model_id"}
        ):
            raise RuntimeError("isolated model response schema is invalid")
        return dict(response["output"]), dict(response["telemetry"])


def main() -> None:
    try:
        response = process_request(_read_frame(sys.stdin.buffer))
    except Exception as exc:
        response = {
            "protocol_version": PROTOCOL_VERSION,
            "error_type": type(exc).__name__,
        }
        try:
            attestation = _model_attestation()
            response["attested_model_id"] = ":".join(
                (
                    MODEL,
                    attestation.auth_mode,
                    attestation.account_id_sha256,
                    attestation.executable_sha256,
                    "no-platform-fallback",
                )
            )
        except Exception:
            response["attested_model_id"] = f"{MODEL}:attestation-unavailable"
    _write_frame(sys.stdout.buffer, response)


if __name__ == "__main__":
    main()
