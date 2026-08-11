from __future__ import annotations

import argparse
import json
import os

# subprocess is required for fixed argv; shell execution is never enabled.
import subprocess  # nosec B404
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .ai_review import (
    MINIMAX_MODEL,
    MINIMAX_PROVIDER,
    REVIEW_PROMPT_VERSION,
    AIReviewStore,
    LLMRun,
    LLMUsage,
    TradeReviewPacket,
    build_trade_review_packet,
    canonical_json,
    sha256_text,
    validate_trade_review,
)

OPENCODE = Path("/home/andrei/.local/bin/opencode")
DEFAULT_DAILY_CAP = 50
PURPOSE = "TRADE_REVIEW"
DENY_ALL_TOOLS_CONFIG = canonical_json({"permission": "deny"})
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
    "ai-review-pending --limit 5"
)
REMOTE_SUBMIT = (
    "sudo -n -u etoro-agent /opt/eToro/.venv/bin/etoro-agent "
    "--config /opt/eToro/config/demo.json --runtime /var/lib/etoro-agent "
    "ai-review-submit-stdin"
)
REMOTE_HEARTBEAT = (
    "sudo -n -u etoro-agent /opt/eToro/.venv/bin/etoro-agent "
    "--config /opt/eToro/config/demo.json --runtime /var/lib/etoro-agent "
    "ai-runner-heartbeat-stdin"
)


@dataclass(frozen=True)
class MiniMaxExecutionResult:
    status: str
    trade_id: str
    review_id: str | None = None
    run_id: str | None = None
    review: Mapping[str, Any] | None = None
    error_type: str | None = None


def review_prompt(packet: TradeReviewPacket) -> str:
    schema = {
        "verdict": "GOOD_PROCESS_GOOD_OUTCOME | GOOD_PROCESS_BAD_OUTCOME | BAD_PROCESS | INCONCLUSIVE",
        "process_score": "integer 0..100",
        "confidence": "number 0..1",
        "rule_adherence": "PASS | FAIL | UNKNOWN",
        "reason_codes": ["1..8 bounded snake_case codes"],
        "findings": ["1..8 evidence-bound findings"],
        "suggested_experiments": ["1..5 offline test hypotheses"],
        "summary": "bounded summary",
    }
    return (
        "You are MiniMax-M3, an asynchronous forensic reviewer for a closed PAPER/DEMO "
        "round-trip trade. Return exactly one JSON object and nothing else. Do not call tools, "
        "read files, browse, or execute commands. Use only the immutable packet. Never propose "
        "a broker order, live configuration edit, code edit, credential use, risk-limit change, "
        "or REAL trading. Suggested experiments are RESEARCH_ONLY and require backtest plus "
        "shadow A/B validation. Distinguish process quality from realized outcome. Do not invent "
        "market facts.\n"
        f"PROMPT_VERSION={REVIEW_PROMPT_VERSION}\n"
        f"OUTPUT_SCHEMA={canonical_json(schema)}\n"
        f"TRADE_PACKET={packet.packet_json}"
    )


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        converted = int(value)
    except (TypeError, ValueError):
        return None
    return converted if converted >= 0 else None


def _usage_from_jsonl(events: list[dict[str, Any]]) -> LLMUsage:
    totals: dict[str, int | None] = {
        "input": None,
        "output": None,
        "reasoning": None,
        "cache_read": None,
        "cache_write": None,
    }
    costs: list[float] = []

    def add(name: str, value: Any) -> None:
        converted = _int_or_none(value)
        if converted is not None:
            totals[name] = (totals[name] or 0) + converted

    for event in events:
        part = event.get("part") if isinstance(event.get("part"), dict) else {}
        candidates = [
            event.get("usage"),
            event.get("tokens"),
            part.get("usage"),
            part.get("tokens"),
        ]
        for usage in candidates:
            if not isinstance(usage, dict):
                continue
            add("input", usage.get("input", usage.get("input_tokens", usage.get("inputTokens"))))
            add(
                "output", usage.get("output", usage.get("output_tokens", usage.get("outputTokens")))
            )
            add(
                "reasoning",
                usage.get("reasoning", usage.get("reasoning_tokens", usage.get("reasoningTokens"))),
            )
            cache = usage.get("cache") if isinstance(usage.get("cache"), dict) else {}
            add(
                "cache_read",
                cache.get("read", usage.get("cache_read", usage.get("cacheReadTokens"))),
            )
            add(
                "cache_write",
                cache.get("write", usage.get("cache_write", usage.get("cacheWriteTokens"))),
            )
        cost = event.get("cost", part.get("cost"))
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            costs.append(float(cost))
    return LLMUsage(
        input_tokens=totals["input"],
        output_tokens=totals["output"],
        reasoning_tokens=totals["reasoning"],
        cache_read_tokens=totals["cache_read"],
        cache_write_tokens=totals["cache_write"],
        cost_usd=(format(sum(costs), ".12g") if costs else None),
    )


def parse_opencode_jsonl(raw: str) -> tuple[dict[str, Any], LLMUsage]:
    events: list[dict[str, Any]] = []
    text_parts: list[str] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError("OpenCode emitted invalid JSONL") from exc
        if not isinstance(event, dict):
            raise ValueError("OpenCode emitted a non-object JSONL event")
        events.append(event)
        event_type = str(event.get("type", "")).lower()
        part = event.get("part") if isinstance(event.get("part"), dict) else {}
        part_type = str(part.get("type", "")).lower()
        if "tool" in event_type or "tool" in part_type:
            raise PermissionError("MiniMax attempted a forbidden tool call")
        if part_type == "text" and isinstance(part.get("text"), str):
            text_parts.append(str(part["text"]))
        elif event_type in {"text", "message"} and isinstance(event.get("text"), str):
            text_parts.append(str(event["text"]))
    if not text_parts:
        raise ValueError("OpenCode did not emit a MiniMax text result")
    raw_result = "".join(text_parts).strip()
    # MiniMax commonly wraps an otherwise strict response in one canonical
    # JSON fence. Normalize only that exact wrapper; arbitrary prose and all
    # non-JSON output remain fail-closed below.
    fence_start = "```json\n"
    fence_end = "\n```"
    if raw_result.startswith(fence_start) and raw_result.endswith(fence_end):
        raw_result = raw_result[len(fence_start) : -len(fence_end)].strip()
    try:
        value = json.loads(raw_result)
    except json.JSONDecodeError as exc:
        raise ValueError("MiniMax result was not strict JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("MiniMax result was not a JSON object")
    return validate_trade_review(value), _usage_from_jsonl(events)


def run_opencode(prompt: str, *, timeout: int = 240) -> tuple[dict[str, Any], LLMUsage]:
    runtime_base = Path(os.getenv("XDG_RUNTIME_DIR") or tempfile.gettempdir())
    with tempfile.TemporaryDirectory(prefix="etoro-minimax-", dir=runtime_base) as folder:
        prompt_path = Path(folder) / "review-packet.txt"
        prompt_path.write_text(prompt, encoding="utf-8")
        prompt_path.chmod(0o600)
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
            "--property=InaccessiblePaths=-/opt/Mobiup/.ssh",
            "--property=InaccessiblePaths=-/home/andrei/.ssh",
            f"--property=ReadWritePaths={folder}",
            "--property=ReadWritePaths=/home/andrei/.local/share/opencode",
            "--property=ReadWritePaths=/home/andrei/.cache/opencode",
            f"--working-directory={folder}",
            "--setenv=HOME=/home/andrei",
            "--setenv=NO_COLOR=1",
            f"--setenv=OPENCODE_CONFIG_CONTENT={DENY_ALL_TOOLS_CONFIG}",
            str(OPENCODE),
            "run",
            "--pure",
            "--format",
            "json",
            "--model",
            MINIMAX_MODEL,
            "--title",
            "eToro post-trade review",
            "--dir",
            folder,
            "Review the attached immutable trade packet and return only the required JSON.",
            "--file",
            str(prompt_path),
        )
        # Fixed systemd-run/OpenCode argv; model text is stdin only.
        completed = subprocess.run(  # nosec B603
            command,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
            env={**os.environ, "NO_COLOR": "1"},
        )
        if completed.returncode != 0:
            detail = " ".join(completed.stderr.strip().splitlines())[-500:]
            raise RuntimeError(
                f"OpenCode MiniMax failed with exit {completed.returncode}: "
                f"{detail or 'no diagnostic'}"
            )
        return parse_opencode_jsonl(completed.stdout)


def _run_command(
    command: tuple[str, ...], *, input_text: str | None = None, timeout: int = 120
) -> str:
    # Internal callers select only the fixed SSH argv constants above.
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
        detail = " ".join(completed.stderr.strip().splitlines())[-500:]
        raise RuntimeError(
            f"command failed with exit {completed.returncode}: {detail or 'no diagnostic'}"
        )
    return completed.stdout


def fetch_pending() -> tuple[dict[str, Any], ...]:
    raw = _run_command((*SSH_BASE, REMOTE_PENDING), timeout=30)
    value = json.loads(raw)
    if not isinstance(value, list):
        raise ValueError("primary MiniMax review queue returned a non-list payload")
    return tuple(item for item in value if isinstance(item, dict))


def _packet_from_wire(value: Mapping[str, Any]) -> TradeReviewPacket:
    required = {
        "job_id",
        "attempt",
        "claim_token",
        "packet_id",
        "packet_hash",
        "packet_json",
        "trade_id",
        "strategy_id",
    }
    if set(value) != required:
        raise ValueError("primary MiniMax packet does not match the wire contract")
    packet_json = str(value["packet_json"])
    try:
        payload = json.loads(packet_json)
    except json.JSONDecodeError as exc:
        raise ValueError("primary MiniMax packet_json is invalid") from exc
    if not isinstance(payload, dict):
        raise ValueError("primary MiniMax packet payload is not an object")
    rebuilt = build_trade_review_packet(payload)
    if (
        rebuilt.packet_id != str(value["packet_id"])
        or rebuilt.packet_hash != str(value["packet_hash"])
        or rebuilt.packet_json != packet_json
        or rebuilt.trade_id != str(value["trade_id"])
        or rebuilt.strategy_id != str(value["strategy_id"])
    ):
        raise ValueError("primary MiniMax packet failed immutable hash verification")
    return rebuilt


def execute_wire_packet(
    value: Mapping[str, Any],
    *,
    runner: Callable[[str], tuple[dict[str, Any], LLMUsage]] = run_opencode,
) -> dict[str, Any]:
    packet = _packet_from_wire(value)
    prompt = review_prompt(packet)
    prompt_hash = sha256_text(prompt)
    started = datetime.now(UTC)
    monotonic_start = time.monotonic()
    try:
        review, usage = runner(prompt)
        review = validate_trade_review(review)
        output_hash = sha256_text(canonical_json(review))
        status = "COMPLETED"
        error_type = None
    except Exception as exc:
        review = None
        usage = LLMUsage()
        output_hash = None
        status = "ERROR"
        error_type = type(exc).__name__
    completed = datetime.now(UTC)
    run_seed = canonical_json(
        {
            "purpose": PURPOSE,
            "model": MINIMAX_MODEL,
            "packet_hash": packet.packet_hash,
            "started_at": started.isoformat(),
        }
    )
    return {
        "run_id": f"llm-run-{sha256_text(run_seed)[:24]}",
        "purpose": PURPOSE,
        "provider": MINIMAX_PROVIDER,
        "model": MINIMAX_MODEL,
        "status": status,
        "job_id": str(value["job_id"]),
        "attempt": int(value["attempt"]),
        "claim_token": str(value["claim_token"]),
        "packet_id": packet.packet_id,
        "packet_hash": packet.packet_hash,
        "trade_id": packet.trade_id,
        "strategy_id": packet.strategy_id,
        "prompt_version": REVIEW_PROMPT_VERSION,
        "prompt_hash": prompt_hash,
        "output_hash": output_hash,
        "review": review,
        "usage": asdict(usage),
        "latency_ms": max(0, round((time.monotonic() - monotonic_start) * 1000)),
        "error_type": error_type,
        "started_at": started.isoformat(),
        "completed_at": completed.isoformat(),
    }


def submit_wire_result(result: Mapping[str, Any]) -> None:
    _run_command(
        (*SSH_BASE, REMOTE_SUBMIT),
        input_text=canonical_json(dict(result)),
        timeout=30,
    )


def run_once() -> int:
    count = 0
    for packet in fetch_pending():
        submit_wire_result(execute_wire_packet(packet))
        count += 1
    return count


def report_heartbeat(status: str, consecutive_errors: int, last_success: str | None) -> None:
    _run_command(
        (*SSH_BASE, REMOTE_HEARTBEAT),
        input_text=canonical_json(
            {
                "service": "minimax-runner",
                "status": status,
                "consecutive_errors": consecutive_errors,
                "last_success": last_success,
            }
        ),
        timeout=30,
    )


def review_closed_trade(
    store: AIReviewStore,
    trade: Mapping[str, Any],
    *,
    daily_cap: int = DEFAULT_DAILY_CAP,
    runner: Callable[[str], tuple[dict[str, Any], LLMUsage]] = run_opencode,
) -> MiniMaxExecutionResult:
    """Review one closed trade; all MiniMax failures become non-blocking results."""

    trade_id = str(trade.get("trade_id", "unknown"))[:128]
    try:
        packet = build_trade_review_packet(trade)
        prompt = review_prompt(packet)
        prompt_hash = sha256_text(prompt)
        if store.has_review(packet.trade_id, MINIMAX_MODEL, prompt_hash):
            return MiniMaxExecutionResult(status="DEDUPED", trade_id=packet.trade_id)
        claim = store.claim_daily_capacity(
            provider=MINIMAX_PROVIDER,
            model=MINIMAX_MODEL,
            purpose=PURPOSE,
            input_hash=packet.packet_hash,
            prompt_hash=prompt_hash,
            daily_cap=daily_cap,
        )
        if claim == "DUPLICATE":
            return MiniMaxExecutionResult(status="DEDUPED", trade_id=packet.trade_id)
        if claim == "CAP_REACHED":
            return MiniMaxExecutionResult(status="CAP_REACHED", trade_id=packet.trade_id)

        started = datetime.now(UTC)
        monotonic_start = time.monotonic()
        try:
            review, usage = runner(prompt)
            review = validate_trade_review(review)
            output_json = canonical_json(review)
            status = "COMPLETED"
            error_type = None
            error_message = None
            output_hash = sha256_text(output_json)
        except Exception as exc:
            review = None
            usage = LLMUsage()
            status = "ERROR"
            error_type = type(exc).__name__
            error_message = str(exc)
            output_hash = None
        completed = datetime.now(UTC)
        latency_ms = max(0, round((time.monotonic() - monotonic_start) * 1000))
        run_seed = canonical_json(
            {
                "purpose": PURPOSE,
                "model": MINIMAX_MODEL,
                "packet_hash": packet.packet_hash,
                "started_at": started.isoformat(),
            }
        )
        run = LLMRun(
            run_id=f"llm-run-{sha256_text(run_seed)[:24]}",
            purpose=PURPOSE,
            provider=MINIMAX_PROVIDER,
            model=MINIMAX_MODEL,
            status=status,
            input_hash=packet.packet_hash,
            prompt_hash=prompt_hash,
            output_hash=output_hash,
            usage=usage,
            latency_ms=latency_ms,
            error_type=error_type,
            error_message=error_message,
            started_at=started.isoformat(),
            completed_at=completed.isoformat(),
        )
        store.record_run(run)
        if review is None:
            return MiniMaxExecutionResult(
                status="ERROR",
                trade_id=packet.trade_id,
                run_id=run.run_id,
                error_type=error_type,
            )
        review_id = store.record_review(packet, review, run.run_id, prompt_hash)
        return MiniMaxExecutionResult(
            status="COMPLETED",
            trade_id=packet.trade_id,
            review_id=review_id,
            run_id=run.run_id,
            review=review,
        )
    except Exception as exc:
        # Post-trade research must never interrupt the trading or exit path.
        return MiniMaxExecutionResult(
            status="ERROR",
            trade_id=trade_id,
            error_type=type(exc).__name__,
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sandboxed OpenCode MiniMax-M3 post-trade review runner"
    )
    parser.add_argument("--interval", type=int, default=120)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.once:
        print(f"MINIMAX_REVIEWS={run_once()}")
        return
    if args.interval < 30:
        raise SystemExit("MiniMax polling interval must be at least 30 seconds")
    consecutive_errors = 0
    last_success: str | None = None
    while True:
        try:
            count = run_once()
            consecutive_errors = 0
            last_success = datetime.now(UTC).isoformat()
            report_heartbeat("ok", consecutive_errors, last_success)
            print(f"MINIMAX_REVIEWS={count}", flush=True)
        except Exception as exc:
            consecutive_errors += 1
            print(f"MINIMAX_RUNNER_ERROR={type(exc).__name__}", flush=True)
            try:
                report_heartbeat("error", consecutive_errors, last_success)
            except Exception:
                print("MINIMAX_HEARTBEAT_ERROR=1", flush=True)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
