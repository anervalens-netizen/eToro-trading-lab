from __future__ import annotations

import argparse
import hashlib
import importlib.resources
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from .ai_review import canonical_json, sha256_text, validate_strategy_change_proposal


MODEL = "gpt-5.6-sol"
PROVIDER = "openai-chatgpt"
PURPOSE = "TRADE_DECISION"
STRATEGY_REVIEW_PURPOSE = "STRATEGY_REVIEW"
CODEX_NATIVE = Path(
    "/usr/lib/node_modules/@openai/codex/node_modules/@openai/"
    "codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex"
)
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
    "ai-runner-pending --limit 5"
)
REMOTE_DECIDE = (
    "sudo -n -u etoro-agent /opt/eToro/.venv/bin/etoro-agent "
    "--config /opt/eToro/config/demo.json --runtime /var/lib/etoro-agent "
    "ai-decide-stdin"
)
REMOTE_RECORD_RUN = (
    "sudo -n -u etoro-agent /opt/eToro/.venv/bin/etoro-agent "
    "--config /opt/eToro/config/demo.json --runtime /var/lib/etoro-agent "
    "ai-run-record-stdin"
)
REMOTE_STRATEGY_PENDING = (
    "sudo -n -u etoro-agent /opt/eToro/.venv/bin/etoro-agent "
    "--config /opt/eToro/config/demo.json --runtime /var/lib/etoro-agent "
    "ai-strategy-review-pending --limit 5"
)
REMOTE_STRATEGY_SUBMIT = (
    "sudo -n -u etoro-agent /opt/eToro/.venv/bin/etoro-agent "
    "--config /opt/eToro/config/demo.json --runtime /var/lib/etoro-agent "
    "ai-strategy-proposal-submit-stdin"
)
REMOTE_HEARTBEAT = (
    "sudo -n -u etoro-agent /opt/eToro/.venv/bin/etoro-agent "
    "--config /opt/eToro/config/demo.json --runtime /var/lib/etoro-agent "
    "ai-runner-heartbeat-stdin"
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
        detail = " ".join(
            (completed.stderr.strip() or completed.stdout.strip()).splitlines()
        )[-500:]
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
        "For OPEN, either select one supplied candidate_id with intent=null, or leave candidate_id "
        "empty and create a direct intent within intent_constraints and allowed_symbols. CLOSE is allowed only "
        "when mode is POSITION_REVIEW and position is non-null. Never invent a candidate, symbol, "
        "price, or fact. Prefer HOLD when edge, data quality, market status, or cost margin is weak. "
        "market_events contain public-source headlines plus a deterministic keyword hint; treat the "
        "hint as unverified, require confirmation from the supplied market features, and use HOLD when "
        "the headline is stale, ambiguous, or not specific enough. "
        "Do not use tools, browse, read files, or run commands. This is a short-horizon research "
        "decision, not a request to maximize trade count. Deterministic risk checks run after you.\n\n"
        f"DECISION_PACKET={json.dumps(packet, sort_keys=True, separators=(',', ':'))}"
    )


def _parse_codex_usage(raw: str) -> dict[str, int | None]:
    usage: dict[str, int | None] = {
        "input_tokens": None,
        "output_tokens": None,
        "reasoning_tokens": None,
        "cache_read_tokens": None,
        "cache_write_tokens": None,
    }
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError("Codex emitted invalid JSONL telemetry") from exc
        if not isinstance(event, dict):
            raise ValueError("Codex emitted a non-object JSONL event")
        candidate = event.get("usage")
        if not isinstance(candidate, dict):
            continue

        def available(*names: str) -> int | None:
            for name in names:
                value = candidate.get(name)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    return value
            return None

        usage = {
            "input_tokens": available("input_tokens", "inputTokens", "input"),
            "output_tokens": available("output_tokens", "outputTokens", "output"),
            "reasoning_tokens": available(
                "reasoning_tokens", "reasoningTokens", "reasoning"
            ),
            "cache_read_tokens": available(
                "cached_input_tokens", "cache_read_tokens", "cacheReadTokens"
            ),
            "cache_write_tokens": available(
                "cache_write_tokens", "cacheWriteTokens"
            ),
        }
    return usage


def _validate(packet: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    action = str(decision.get("action", "")).upper()
    candidate_id = str(decision.get("candidate_id", ""))
    direct = decision.get("intent")
    if action not in {"OPEN", "CLOSE", "HOLD"}:
        raise ValueError("Sol returned an unsupported action")
    candidates = {
        str(item.get("candidate_id"))
        for item in packet.get("payload", {}).get("candidates", [])
        if isinstance(item, dict)
    }
    normalized_intent: dict[str, Any] | None = None
    if action == "OPEN" and candidate_id:
        if candidate_id not in candidates or direct is not None:
            raise ValueError("Sol OPEN did not bind to exactly one candidate")
    elif action == "OPEN":
        if not isinstance(direct, dict):
            raise ValueError("Sol direct OPEN lacks a bounded intent")
        required = {
            "symbol", "side", "amount_usd", "stop_loss_fraction",
            "take_profit_fraction", "max_holding_seconds",
        }
        if set(direct) != required:
            raise ValueError("Sol direct intent does not match the strict schema")
        constraints = packet.get("payload", {}).get("intent_constraints", {})
        symbol = str(direct["symbol"]).upper()
        side = str(direct["side"]).lower()
        amount = float(direct["amount_usd"])
        stop = float(direct["stop_loss_fraction"])
        take = float(direct["take_profit_fraction"])
        holding = int(direct["max_holding_seconds"])
        if symbol not in set(packet.get("payload", {}).get("allowed_symbols", [])):
            raise ValueError("Sol direct intent symbol is not allowed")
        if side not in {"buy", "sell"}:
            raise ValueError("Sol direct intent side is invalid")
        if not 0 < amount <= float(constraints.get("max_order_notional_usd", 0)):
            raise ValueError("Sol direct intent amount exceeds its packet boundary")
        minimums = constraints.get("minimum_amount_usd_by_symbol", {})
        minimum = float(minimums.get(symbol, 0)) if isinstance(minimums, dict) else 0
        if amount < minimum:
            raise ValueError("Sol direct intent is below the broker minimum amount")
        if not float(constraints.get("min_stop_loss_fraction", 0)) <= stop <= float(
            constraints.get("max_stop_loss_fraction", 0)
        ):
            raise ValueError("Sol direct intent stop is outside its packet boundary")
        max_trade_risk = float(constraints.get("max_trade_risk_usd", 0))
        if max_trade_risk and amount * stop > max_trade_risk:
            raise ValueError("Sol direct intent exceeds maximum projected trade risk")
        if not 0 < take <= 2 or not 300 <= holding <= 604800:
            raise ValueError("Sol direct intent exit parameters are invalid")
        normalized_intent = {
            "symbol": symbol, "side": side, "amount_usd": amount,
            "stop_loss_fraction": stop, "take_profit_fraction": take,
            "max_holding_seconds": holding,
        }
    elif direct is not None:
        raise ValueError("Sol supplied an intent for a non-OPEN action")
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
        "intent": normalized_intent,
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
        codex_home = temp / "codex-home"
        home = temp / "home"
        codex_home.mkdir(mode=0o700)
        home.mkdir(mode=0o700)
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
            "--property=ProtectHome=tmpfs",
            "--property=PrivateTmp=yes",
            "--property=NoExecPaths=/",
            f"--property=ExecPaths={CODEX_NATIVE}",
            "--property=InaccessiblePaths=-/opt/Mobiup/.ssh",
            "--property=InaccessiblePaths=-/home/andrei/.ssh",
            f"--property=ReadWritePaths={temp}",
            "--property=BindReadOnlyPaths=/home/andrei/.codex/auth.json:"
            f"{codex_home}/auth.json",
            f"--working-directory={temp}",
            f"--setenv=HOME={home}",
            f"--setenv=CODEX_HOME={codex_home}",
            str(CODEX_NATIVE),
            "exec",
            "--model",
            MODEL,
            "--dangerously-bypass-approvals-and-sandbox",
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
        prompt = _prompt(packet)
        started = time.time()
        raw_events = _run(command, input_text=prompt, timeout=240)
        completed = time.time()
        value = json.loads(output.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Sol output is not a JSON object")
        decision = _validate(packet, value)
        decision_json = canonical_json(decision)
        started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started))
        completed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(completed))
        run_seed = canonical_json(
            {
                "packet_hash": str(packet["packet_hash"]),
                "prompt_hash": hashlib.sha256(prompt.encode()).hexdigest(),
                "started_at": started_at,
            }
        )
        decision["llm_run"] = {
            "run_id": f"llm-run-{hashlib.sha256(run_seed.encode()).hexdigest()[:24]}",
            "purpose": PURPOSE,
            "provider": PROVIDER,
            "model": MODEL,
            "status": "COMPLETED",
            "input_hash": str(packet["packet_hash"]),
            "prompt_hash": hashlib.sha256(prompt.encode()).hexdigest(),
            "output_hash": hashlib.sha256(decision_json.encode()).hexdigest(),
            **_parse_codex_usage(raw_events),
            "cost_usd": None,
            "latency_ms": max(0, round((completed - started) * 1000)),
            "error_type": None,
            "started_at": started_at,
            "completed_at": completed_at,
        }
        return decision


def submit(decision: dict[str, Any]) -> None:
    _run(
        (*SSH_BASE, REMOTE_DECIDE),
        input_text=json.dumps(decision, sort_keys=True, separators=(",", ":")),
        timeout=30,
    )


def _error_run(packet: dict[str, Any], exc: Exception, started: float) -> dict[str, Any]:
    completed = time.time()
    prompt = _prompt(packet)
    prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()
    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started))
    completed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(completed))
    seed = canonical_json(
        {
            "packet_hash": str(packet["packet_hash"]),
            "prompt_hash": prompt_hash,
            "started_at": started_at,
            "status": "ERROR",
        }
    )
    return {
        "run_id": f"llm-run-{hashlib.sha256(seed.encode()).hexdigest()[:24]}",
        "purpose": PURPOSE,
        "provider": PROVIDER,
        "model": MODEL,
        "status": "ERROR",
        "input_hash": str(packet["packet_hash"]),
        "prompt_hash": prompt_hash,
        "output_hash": None,
        "input_tokens": None,
        "output_tokens": None,
        "reasoning_tokens": None,
        "cache_read_tokens": None,
        "cache_write_tokens": None,
        "cost_usd": None,
        "latency_ms": max(0, round((completed - started) * 1000)),
        "error_type": type(exc).__name__,
        "started_at": started_at,
        "completed_at": completed_at,
    }


def submit_error_run(run: dict[str, Any]) -> None:
    _run(
        (*SSH_BASE, REMOTE_RECORD_RUN),
        input_text=canonical_json(run),
        timeout=30,
    )


def run_once() -> int:
    count = 0
    for packet in fetch_pending():
        started = time.time()
        try:
            decision = decide(packet)
        except Exception as exc:
            submit_error_run(_error_run(packet, exc, started))
            continue
        submit(decision)
        count += 1
    return count


def strategy_review_prompt(aggregate: dict[str, Any]) -> str:
    return (
        "You are the research critic for an eToro PAPER/DEMO trading lab. "
        "Return exactly one JSON object matching the supplied schema. Use only the "
        "immutable daily aggregate. Propose offline experiments, never orders, live code, "
        "configuration, risk-limit changes, credentials, or REAL trading. Evidence must be "
        "traceable to the aggregate. A proposal is RESEARCH_ONLY and must pass backtest, "
        "cost stress, and shadow A/B before promotion.\n\n"
        f"DAILY_REVIEW_AGGREGATE={canonical_json(aggregate)}"
    )


def fetch_strategy_reviews() -> tuple[dict[str, Any], ...]:
    raw = _run((*SSH_BASE, REMOTE_STRATEGY_PENDING), timeout=30)
    value = json.loads(raw)
    if not isinstance(value, list):
        raise ValueError("primary strategy review queue returned a non-list payload")
    return tuple(item for item in value if isinstance(item, dict))


def review_strategy(value: dict[str, Any]) -> dict[str, Any]:
    if set(value) != {"source_day", "strategy_id", "aggregate_hash", "aggregate"}:
        raise ValueError("strategy review packet does not match the strict wire schema")
    aggregate = value["aggregate"]
    if not isinstance(aggregate, dict):
        raise ValueError("strategy review aggregate is invalid")
    if (
        str(aggregate.get("day")) != str(value["source_day"])
        or str(aggregate.get("strategy_id")) != str(value["strategy_id"])
        or str(aggregate.get("aggregate_hash")) != str(value["aggregate_hash"])
    ):
        raise ValueError("strategy review aggregate binding is invalid")
    schema_resource = importlib.resources.files("etoro_agent").joinpath(
        "strategy_proposal.schema.json"
    )
    runtime_base = os.getenv("XDG_RUNTIME_DIR", "/run/user/1000")
    with tempfile.TemporaryDirectory(prefix="etoro-strategy-review-", dir=runtime_base) as folder:
        temp = Path(folder)
        codex_home = temp / "codex-home"
        home = temp / "home"
        codex_home.mkdir(mode=0o700)
        home.mkdir(mode=0o700)
        schema = temp / "schema.json"
        output = temp / "proposal.json"
        schema.write_text(schema_resource.read_text(encoding="utf-8"), encoding="utf-8")
        command = (
            "sudo", "-n", "systemd-run", "--pipe", "--wait", "--collect", "--quiet",
            "--property=User=andrei", "--property=Group=andrei",
            "--property=NoNewPrivileges=yes", "--property=ProtectSystem=strict",
            "--property=ProtectHome=tmpfs", "--property=PrivateTmp=yes",
            "--property=NoExecPaths=/", f"--property=ExecPaths={CODEX_NATIVE}",
            "--property=InaccessiblePaths=-/opt/Mobiup/.ssh",
            "--property=InaccessiblePaths=-/home/andrei/.ssh",
            f"--property=ReadWritePaths={temp}",
            "--property=BindReadOnlyPaths=/home/andrei/.codex/auth.json:"
            f"{codex_home}/auth.json",
            f"--working-directory={temp}", f"--setenv=HOME={home}",
            f"--setenv=CODEX_HOME={codex_home}",
            str(CODEX_NATIVE), "exec", "--model", MODEL,
            "--dangerously-bypass-approvals-and-sandbox",
            "--ephemeral", "--ignore-user-config", "--skip-git-repo-check", "--json",
            "--output-schema", str(schema), "--output-last-message", str(output), "-",
        )
        prompt = strategy_review_prompt(aggregate)
        started = time.time()
        raw_events = _run(command, input_text=prompt, timeout=240)
        completed = time.time()
        proposal_value = json.loads(output.read_text(encoding="utf-8"))
        if not isinstance(proposal_value, dict):
            raise ValueError("Sol strategy proposal is not a JSON object")
        proposal = validate_strategy_change_proposal(aggregate, proposal_value)
        normalized = {
            "strategy_id": proposal.strategy_id,
            "objective": proposal.objective,
            "evidence": list(proposal.evidence),
            "suggested_experiments": list(proposal.suggested_experiments),
            "confidence": proposal.confidence,
        }
        prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()
        started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started))
        completed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(completed))
        seed = canonical_json(
            {"aggregate_hash": value["aggregate_hash"], "prompt_hash": prompt_hash, "started_at": started_at}
        )
        return {
            "source_day": str(value["source_day"]),
            "strategy_id": str(value["strategy_id"]),
            "aggregate_hash": str(value["aggregate_hash"]),
            "proposal": normalized,
            "llm_run": {
                "run_id": f"llm-run-{hashlib.sha256(seed.encode()).hexdigest()[:24]}",
                "purpose": STRATEGY_REVIEW_PURPOSE,
                "provider": PROVIDER,
                "model": MODEL,
                "status": "COMPLETED",
                "input_hash": str(value["aggregate_hash"]),
                "prompt_hash": prompt_hash,
                "output_hash": sha256_text(canonical_json(normalized)),
                **_parse_codex_usage(raw_events),
                "cost_usd": None,
                "latency_ms": max(0, round((completed - started) * 1000)),
                "error_type": None,
                "started_at": started_at,
                "completed_at": completed_at,
            },
        }


def submit_strategy_proposal(value: dict[str, Any]) -> None:
    _run(
        (*SSH_BASE, REMOTE_STRATEGY_SUBMIT),
        input_text=canonical_json(value),
        timeout=30,
    )


def run_strategy_reviews_once() -> int:
    count = 0
    for packet in fetch_strategy_reviews():
        submit_strategy_proposal(review_strategy(packet))
        count += 1
    return count


def report_heartbeat(status: str, consecutive_errors: int, last_success: str | None) -> None:
    _run(
        (*SSH_BASE, REMOTE_HEARTBEAT),
        input_text=canonical_json(
            {
                "service": "sol-runner",
                "status": status,
                "consecutive_errors": consecutive_errors,
                "last_success": last_success,
            }
        ),
        timeout=30,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Sandboxed ChatGPT-authenticated Sol decision runner")
    parser.add_argument("--interval", type=int, default=120)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.once:
        print(f"SOL_DECISIONS={run_once()} STRATEGY_REVIEWS={run_strategy_reviews_once()}")
        return
    if args.interval < 30:
        raise SystemExit("Sol polling interval must be at least 30 seconds")
    consecutive_errors = 0
    last_success: str | None = None
    while True:
        try:
            count = run_once()
            strategy_reviews = run_strategy_reviews_once()
            consecutive_errors = 0
            last_success = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            report_heartbeat("ok", consecutive_errors, last_success)
            print(
                f"SOL_DECISIONS={count} STRATEGY_REVIEWS={strategy_reviews}",
                flush=True,
            )
        except Exception as exc:
            consecutive_errors += 1
            print(f"SOL_RUNNER_ERROR={type(exc).__name__}", flush=True)
            try:
                report_heartbeat("error", consecutive_errors, last_success)
            except Exception:
                print("SOL_HEARTBEAT_ERROR=1", flush=True)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
