from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from .agent import TradingAgent
from .agent_portfolio import AgentPortfolioReader
from .ai_decision import AIDecisionStore
from .ai_review import (
    MINIMAX_MODEL,
    AIReviewStore,
    LLMRun,
    LLMUsage,
    build_trade_review_packet,
    canonical_json,
    sha256_text,
    validate_strategy_change_proposal,
)
from .audit import AuditLog
from .backtest import load_closes, run_backtest
from .config import load_config
from .market import MarketDataCollector
from .mcp import EtoroMCPClient
from .models import KillState
from .portfolio import MASTER_PORTFOLIO_ID
from .risk import generate_signing_keypair
from .strategy import MovingAverageStrategy, build_strategy_suite
from .strategy_catalog import STRATEGY_DEFINITIONS, STRATEGY_PORTFOLIO_BY_ID
from .trade_registry import TradeRecord, TradeRegistry

SOL_MODEL = "gpt-5.6-sol"
SOL_PROVIDER = "openai-chatgpt"
SOL_PURPOSE = "TRADE_DECISION"


def _review_trade_payload(trade: TradeRecord, config: object) -> dict[str, object]:
    strategy_by_portfolio = {
        portfolio_id: strategy_id for strategy_id, portfolio_id in STRATEGY_PORTFOLIO_BY_ID.items()
    }
    strategy_versions = {
        strategy.strategy_id: strategy.parameter_version
        for strategy in build_strategy_suite(config.strategy)  # type: ignore[attr-defined]
    }
    strategy_id = strategy_by_portfolio.get(trade.portfolio_id, "sol_master")
    pricing_quality = (
        "BROKER_RECONCILED_MARK_ESTIMATE"
        if trade.portfolio_id == MASTER_PORTFOLIO_ID
        else "PAPER_SIMULATED_NEXT_QUOTE"
    )
    return {
        "trade_id": trade.trade_id,
        "strategy_id": strategy_id,
        "strategy_version": strategy_versions.get(strategy_id, "master-policy-v1"),
        "portfolio_id": trade.portfolio_id,
        "symbol": trade.symbol,
        "side": trade.side,
        "opened_at": trade.opened_at.isoformat(),
        "closed_at": trade.closed_at.isoformat() if trade.closed_at else None,
        "units": str(trade.entry_units),
        "notional_usd": str(trade.entry_notional_usd),
        "entry_price": str(trade.entry_average_price),
        "exit_price": str(trade.exit_average_price)
        if trade.exit_average_price is not None
        else None,
        "gross_pnl_usd": str(trade.gross_pnl_usd),
        "fees_usd": str(trade.fees_usd),
        "financing_usd": "0",
        "net_pnl_usd": str(trade.net_pnl_usd),
        "holding_seconds": trade.duration_seconds,
        "exit_reason": "ledger_round_trip_close",
        "rule_context": {"source": strategy_id},
        "risk_context": {"deterministic_boundary": True},
        "market_context": {"pricing_quality": pricing_quality},
    }


def _queue_review_jobs(config: object, audit: AuditLog) -> int:
    from .minimax_runner import review_prompt

    store = AIReviewStore(audit)
    queued = 0
    for trade in TradeRegistry(audit.db).trades():
        if trade.status != "closed" or trade.closed_at is None:
            continue
        packet = build_trade_review_packet(_review_trade_payload(trade, config))
        prompt_hash = sha256_text(review_prompt(packet))
        if store.has_review(packet.trade_id, MINIMAX_MODEL, prompt_hash):
            continue
        _, created = store.queue_review_job(packet, prompt_hash)
        queued += int(created)
    return queued


def _llm_run_from_wire(value: object) -> LLMRun:
    if not isinstance(value, dict):
        raise ValueError("LLM telemetry must be an object")
    required = {
        "run_id",
        "purpose",
        "provider",
        "model",
        "status",
        "input_hash",
        "prompt_hash",
        "output_hash",
        "input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "cost_usd",
        "latency_ms",
        "error_type",
        "started_at",
        "completed_at",
    }
    if set(value) != required:
        raise ValueError("LLM telemetry does not match the strict wire schema")

    def token(name: str) -> int | None:
        raw = value[name]
        if raw is None:
            return None
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            raise ValueError(f"invalid {name}")
        return raw

    return LLMRun(
        run_id=str(value["run_id"]),
        purpose=str(value["purpose"]),
        provider=str(value["provider"]),
        model=str(value["model"]),
        status=str(value["status"]),
        input_hash=str(value["input_hash"]),
        prompt_hash=str(value["prompt_hash"]),
        output_hash=str(value["output_hash"]) if value["output_hash"] is not None else None,
        usage=LLMUsage(
            input_tokens=token("input_tokens"),
            output_tokens=token("output_tokens"),
            reasoning_tokens=token("reasoning_tokens"),
            cache_read_tokens=token("cache_read_tokens"),
            cache_write_tokens=token("cache_write_tokens"),
            cost_usd=str(value["cost_usd"]) if value["cost_usd"] is not None else None,
        ),
        latency_ms=int(value["latency_ms"]),
        error_type=str(value["error_type"]) if value["error_type"] is not None else None,
        error_message=None,
        started_at=str(value["started_at"]),
        completed_at=str(value["completed_at"]),
    )


def _paths(args: argparse.Namespace) -> tuple[Path, AuditLog]:
    runtime = Path(args.runtime)
    runtime.mkdir(parents=True, exist_ok=True)
    return runtime, AuditLog(runtime / "audit.sqlite3")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="eToro paper/DEMO trading agent; REAL trading is unsupported"
    )
    parser.add_argument("--config", default="config/demo.json")
    parser.add_argument("--runtime", default="runtime")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run-once")
    run.add_argument("symbol")
    run.add_argument(
        "--execute-demo",
        action="store_true",
        help="after printing the exact DEMO request, prompt for one-time operator approval and execute",
    )
    backtest = sub.add_parser("backtest")
    backtest.add_argument("symbol")
    backtest.add_argument("csv")
    sub.add_parser("status")
    sub.add_parser("kill")
    resume = sub.add_parser("resume")
    resume.add_argument("--confirm", default="")
    sub.add_parser("init-security")
    dashboard = sub.add_parser("dashboard")
    dashboard.add_argument("--host", default="127.0.0.1")
    dashboard.add_argument("--port", default=8765, type=int)
    dashboard.add_argument("--uds", default="")
    sub.add_parser("shadow-once")
    shadow_worker = sub.add_parser("shadow-worker")
    shadow_worker.add_argument("--interval", type=int, default=60)
    sub.add_parser("news-once")
    news_worker = sub.add_parser("news-worker")
    news_worker.add_argument("--interval", type=int, default=120)
    ai_pending = sub.add_parser("ai-pending")
    ai_pending.add_argument("--limit", type=int, default=10)
    ai_runner_pending = sub.add_parser("ai-runner-pending")
    ai_runner_pending.add_argument("--limit", type=int, default=5)
    ai_decide = sub.add_parser("ai-decide")
    ai_decide.add_argument("--packet-id", required=True)
    ai_decide.add_argument("--packet-hash", required=True)
    ai_decide.add_argument("--action", choices=("OPEN", "CLOSE", "HOLD"), required=True)
    ai_decide.add_argument("--candidate-id", default="")
    ai_decide.add_argument("--confidence", required=True)
    ai_decide.add_argument("--reason-code", action="append", required=True)
    ai_decide.add_argument("--rationale", required=True)
    ai_decide.add_argument("--model", default="gpt-5.6-sol")
    sub.add_parser("ai-decide-stdin")
    sub.add_parser("ai-run-record-stdin")
    ai_review_pending = sub.add_parser("ai-review-pending")
    ai_review_pending.add_argument("--limit", type=int, default=5)
    ai_review_pending.add_argument("--worker-id", default="dell-minimax-runner")
    sub.add_parser("ai-review-submit-stdin")
    ai_strategy_pending = sub.add_parser("ai-strategy-review-pending")
    ai_strategy_pending.add_argument("--limit", type=int, default=5)
    sub.add_parser("ai-strategy-proposal-submit-stdin")
    sub.add_parser("ai-runner-heartbeat-stdin")
    reconcile_close = sub.add_parser("reconcile-demo-close")
    reconcile_close.add_argument("--symbol", required=True)
    reconcile_close.add_argument("--position-id", required=True, type=int)
    reconcile_close.add_argument("--confirm", default="")
    reconcile_open = sub.add_parser("reconcile-demo-open")
    reconcile_open.add_argument("--symbol", required=True)
    reconcile_open.add_argument("--position-id", required=True, type=int)
    reconcile_open.add_argument("--replace-local-projection", action="store_true")
    reconcile_open.add_argument("--confirm", default="")
    sub.add_parser("agent-portfolio-status")
    args = parser.parse_args()
    config = load_config(args.config)
    runtime, audit = _paths(args)

    if args.command in {"news-once", "news-worker"}:
        from .news import CommodityNewsScanner

        scanner = CommodityNewsScanner(audit)
        if args.command == "news-once":
            print(json.dumps(scanner.scan_once(), sort_keys=True))
        else:
            scanner.run_forever(args.interval)
    elif args.command == "ai-runner-heartbeat-stdin":
        payload = json.load(sys.stdin)
        if set(payload) != {"service", "status", "consecutive_errors", "last_success"}:
            raise ValueError("AI runner heartbeat does not match the strict schema")
        service = str(payload["service"])
        status = str(payload["status"])
        if service not in {"sol-runner", "minimax-runner"} or status not in {"ok", "error"}:
            raise ValueError("AI runner heartbeat identity/status is invalid")
        errors = int(payload["consecutive_errors"])
        if errors < 0:
            raise ValueError("AI runner heartbeat error count is invalid")
        audit.heartbeat(
            service,
            status,
            {"consecutive_errors": errors, "last_success": payload["last_success"]},
        )
        print("HEARTBEAT=RECORDED")
    elif args.command == "agent-portfolio-status":
        reader = AgentPortfolioReader(EtoroMCPClient())
        portfolios = reader.list()
        print(
            json.dumps(
                {
                    "allowed_scopes": reader.allowed_scopes(),
                    "portfolio_count": len(portfolios),
                    "portfolios": [
                        {
                            "portfolio_id": portfolio.portfolio_id,
                            "name": portfolio.name,
                            "virtual_balance_usd": str(portfolio.virtual_balance_usd),
                            "tokens": [
                                {
                                    "token_id": token.token_id,
                                    "name": token.name,
                                    "scope_names": token.scope_names,
                                    "expires_at": token.expires_at,
                                }
                                for token in portfolio.tokens
                            ],
                        }
                        for portfolio in portfolios
                    ],
                    "real_money": False,
                    "read_only": True,
                },
                indent=2,
            )
        )
    elif args.command == "reconcile-demo-open":
        if config.account_mode != "demo" or not config.etoro_demo_execution_enabled:
            raise SystemExit("DEMO broker-open reconciliation is disabled")
        expected = f"RECONCILE_DEMO_OPEN_{args.position_id}"
        if args.confirm != expected:
            raise SystemExit(f"reconciliation requires --confirm {expected}")
        if audit.kill_state() is not KillState.LOCKED:
            raise SystemExit("reconciliation requires kill state LOCKED")
        if not audit.verify_chain():
            raise SystemExit("reconciliation requires a valid audit chain")
        from .engine import AutonomousShadowEngine

        client = EtoroMCPClient()
        client.verify_isolated_demo_execution_scope()
        engine = AutonomousShadowEngine(config, audit)
        engine.demo_client = client
        symbol = str(args.symbol).upper()
        if symbol not in config.symbols:
            raise SystemExit("reconciliation symbol is unsupported")
        changed = engine.reconcile_master_broker_open(
            symbol,
            args.position_id,
            replace_local_projection=bool(args.replace_local_projection),
        )
        if not changed:
            raise SystemExit("broker open was already reconciled")
        state = engine.master_ledger.snapshot(MASTER_PORTFOLIO_ID)
        row = audit.db.execute(
            """
            SELECT broker_evidence_hash FROM shadow_broker_open_reconciliations
            WHERE portfolio_id=? AND broker_position_id=?
            """,
            (MASTER_PORTFOLIO_ID, args.position_id),
        ).fetchone()
        print(
            json.dumps(
                {
                    "status": "RECONCILED",
                    "account": "DEMO",
                    "real_money": False,
                    "network_write_attempted": False,
                    "symbol": symbol,
                    "broker_position_id": args.position_id,
                    "broker_evidence_hash": str(row[0]),
                    "replaced_local_projection": bool(args.replace_local_projection),
                    "cash_usd": str(state.cash_usd),
                    "equity_usd": str(state.equity_usd),
                    "realized_pnl_usd": str(state.realized_pnl_usd),
                    "fees_usd": str(state.fees_usd),
                },
                sort_keys=True,
            )
        )
    elif args.command == "reconcile-demo-close":
        if config.account_mode != "demo" or not config.etoro_demo_execution_enabled:
            raise SystemExit("DEMO broker-close reconciliation is disabled")
        expected = f"RECONCILE_DEMO_CLOSE_{args.position_id}"
        if args.confirm != expected:
            raise SystemExit(f"reconciliation requires --confirm {expected}")
        if audit.kill_state() is not KillState.LOCKED:
            raise SystemExit("reconciliation requires kill state LOCKED")
        if not audit.verify_chain():
            raise SystemExit("reconciliation requires a valid audit chain")
        from .engine import AutonomousShadowEngine

        client = EtoroMCPClient()
        client.verify_isolated_demo_execution_scope()
        engine = AutonomousShadowEngine(config, audit)
        engine.demo_client = client
        symbol = str(args.symbol).upper()
        if symbol not in config.symbols:
            raise SystemExit("reconciliation symbol is unsupported")
        broker_positions = engine._broker_symbol_position_state(symbol)
        if broker_positions:
            raise SystemExit("broker position is still open; reconciliation refused")
        changed = engine.reconcile_master_broker_close(symbol, args.position_id)
        if not changed:
            raise SystemExit("broker close was already reconciled")
        state = engine.master_ledger.snapshot(MASTER_PORTFOLIO_ID)
        row = audit.db.execute(
            """
            SELECT broker_evidence_hash FROM shadow_broker_close_reconciliations
            WHERE portfolio_id=? AND broker_position_id=?
            """,
            (MASTER_PORTFOLIO_ID, args.position_id),
        ).fetchone()
        print(
            json.dumps(
                {
                    "status": "RECONCILED",
                    "account": "DEMO",
                    "real_money": False,
                    "network_write_attempted": False,
                    "symbol": symbol,
                    "broker_position_id": args.position_id,
                    "broker_evidence_hash": str(row[0]),
                    "cash_usd": str(state.cash_usd),
                    "equity_usd": str(state.equity_usd),
                    "realized_pnl_usd": str(state.realized_pnl_usd),
                    "fees_usd": str(state.fees_usd),
                },
                sort_keys=True,
            )
        )
    elif args.command in {"ai-strategy-review-pending", "ai-strategy-proposal-submit-stdin"}:
        from .sol_runner import PROVIDER, STRATEGY_REVIEW_PURPOSE, strategy_review_prompt

        review_store = AIReviewStore(audit)
        if not config.ai_review_enabled:
            print("[]" if args.command == "ai-strategy-review-pending" else '{"status":"DISABLED"}')
        elif args.command == "ai-strategy-review-pending":
            candidates: set[tuple[date, str]] = set()
            today = datetime.now(UTC).date()
            for row in audit.db.execute(
                "SELECT strategy_id,packet_json FROM trade_ai_reviews ORDER BY created_at"
            ):
                packet = json.loads(str(row[1]))
                closed_at = packet.get("closed_at")
                if not closed_at:
                    continue
                closed_day = (
                    datetime.fromisoformat(str(closed_at).replace("Z", "+00:00"))
                    .astimezone(UTC)
                    .date()
                )
                if closed_day < today:
                    candidates.add((closed_day, str(row[0])))
            pending: list[dict[str, object]] = []
            for source_day, strategy_id in sorted(candidates):
                exists = audit.db.execute(
                    "SELECT 1 FROM strategy_change_proposals WHERE source_day=? AND strategy_id=?",
                    (source_day.isoformat(), strategy_id),
                ).fetchone()
                if exists is not None:
                    continue
                aggregate = review_store.daily_aggregate(source_day, strategy_id)
                if int(aggregate["review_count"]) < 1:
                    continue
                prompt_hash = sha256_text(strategy_review_prompt(aggregate))
                claim = review_store.claim_daily_capacity(
                    provider=PROVIDER,
                    model=SOL_MODEL,
                    purpose=STRATEGY_REVIEW_PURPOSE,
                    input_hash=str(aggregate["aggregate_hash"]),
                    prompt_hash=prompt_hash,
                    daily_cap=config.sol_daily_strategy_review_limit,
                    claim_key=(
                        f"{source_day.isoformat()}:{strategy_id}:"
                        f"{int(datetime.now(UTC).timestamp()) // 300}"
                    ),
                )
                if claim == "CAP_REACHED":
                    break
                if claim == "CLAIMED":
                    pending.append(
                        {
                            "source_day": source_day.isoformat(),
                            "strategy_id": strategy_id,
                            "aggregate_hash": str(aggregate["aggregate_hash"]),
                            "aggregate": aggregate,
                        }
                    )
                if len(pending) >= max(1, min(args.limit, 20)):
                    break
            print(json.dumps(pending, sort_keys=True, separators=(",", ":")))
        else:
            envelope = json.load(sys.stdin)
            if set(envelope) != {
                "source_day",
                "strategy_id",
                "aggregate_hash",
                "proposal",
                "llm_run",
            }:
                raise ValueError("Sol strategy proposal envelope is invalid")
            source_day = date.fromisoformat(str(envelope["source_day"]))
            strategy_id = str(envelope["strategy_id"])
            aggregate = review_store.daily_aggregate(source_day, strategy_id)
            if str(aggregate["aggregate_hash"]) != str(envelope["aggregate_hash"]):
                raise ValueError("Sol strategy proposal aggregate hash mismatch")
            proposal_raw = envelope["proposal"]
            if not isinstance(proposal_raw, dict):
                raise ValueError("Sol strategy proposal body is invalid")
            proposal = validate_strategy_change_proposal(aggregate, proposal_raw)
            run = _llm_run_from_wire(envelope["llm_run"])
            expected_prompt_hash = sha256_text(strategy_review_prompt(aggregate))
            if (
                run.provider != PROVIDER
                or run.model != SOL_MODEL
                or run.purpose != STRATEGY_REVIEW_PURPOSE
                or run.status != "COMPLETED"
                or run.input_hash != str(aggregate["aggregate_hash"])
                or run.prompt_hash != expected_prompt_hash
                or run.output_hash != sha256_text(canonical_json(proposal_raw))
            ):
                raise ValueError("Sol strategy proposal telemetry mismatch")
            review_store.record_run(run)
            review_store.record_strategy_proposal(aggregate, proposal, llm_run_id=run.run_id)
            print(f"STRATEGY_PROPOSAL=RECORDED proposal_id={proposal.proposal_id}")
    elif args.command in {"ai-review-pending", "ai-review-submit-stdin"}:
        review_store = AIReviewStore(audit)
        if not config.ai_review_enabled:
            print("[]" if args.command == "ai-review-pending" else '{"status":"DISABLED"}')
        elif args.command == "ai-review-pending":
            _queue_review_jobs(config, audit)
            packets = review_store.claim_pending_reviews(
                worker_id=args.worker_id,
                limit=max(1, min(args.limit, 50)),
                daily_cap=config.minimax_daily_review_limit,
                lease_seconds=config.ai_review_lease_seconds,
                max_attempts=config.ai_review_max_attempts,
            )
            print(json.dumps(packets, sort_keys=True, separators=(",", ":")))
        else:
            result = json.load(sys.stdin)
            outcome = review_store.submit_review_result(result)
            print(json.dumps(outcome, sort_keys=True, separators=(",", ":")))
    elif args.command in {
        "ai-pending",
        "ai-runner-pending",
        "ai-decide",
        "ai-decide-stdin",
        "ai-run-record-stdin",
    }:
        store = AIDecisionStore(audit)
        review_store = AIReviewStore(audit)
        if args.command == "ai-pending":
            print(json.dumps(store.pending(args.limit), default=str, indent=2))
        elif args.command == "ai-runner-pending":
            from .sol_runner import PROVIDER, PURPOSE, _prompt

            claimed: list[dict[str, object]] = []
            for packet in store.pending(max(1, min(args.limit, 20))):
                prompt_hash = hashlib.sha256(_prompt(packet).encode()).hexdigest()
                claim = review_store.claim_daily_capacity(
                    provider=PROVIDER,
                    model=SOL_MODEL,
                    purpose=PURPOSE,
                    input_hash=str(packet["packet_hash"]),
                    prompt_hash=prompt_hash,
                    daily_cap=config.sol_daily_call_limit,
                    claim_key=(
                        f"{packet['packet_id']}:{int(datetime.now(UTC).timestamp()) // 300}"
                    ),
                )
                if claim == "CAP_REACHED":
                    break
                if claim == "CLAIMED":
                    claimed.append(packet)
            print(json.dumps(claimed, default=str, separators=(",", ":")))
        elif args.command == "ai-run-record-stdin":
            run = _llm_run_from_wire(json.load(sys.stdin))
            if (
                run.provider != SOL_PROVIDER
                or run.model != SOL_MODEL
                or run.purpose != SOL_PURPOSE
                or run.status != "ERROR"
            ):
                raise ValueError("invalid Sol error telemetry identity")
            row = audit.db.execute(
                "SELECT packet_id,packet_hash,payload_json,created_at,expires_at "
                "FROM ai_decision_packets WHERE packet_hash=?",
                (run.input_hash,),
            ).fetchone()
            if row is None:
                raise ValueError("Sol telemetry is not bound to a decision packet")
            from .sol_runner import _prompt

            packet = {
                "packet_id": str(row[0]),
                "packet_hash": str(row[1]),
                "payload": json.loads(str(row[2])),
                "created_at": str(row[3]),
                "expires_at": int(row[4]),
            }
            expected_prompt_hash = hashlib.sha256(_prompt(packet).encode()).hexdigest()
            if run.prompt_hash != expected_prompt_hash:
                raise ValueError("Sol telemetry prompt hash mismatch")
            review_store.record_run(run)
            print(f"AI_RUN=RECORDED run_id={run.run_id} status={run.status}")
        else:
            if args.command == "ai-decide-stdin":
                payload = json.load(sys.stdin)
                expected_keys = {
                    "packet_id",
                    "packet_hash",
                    "action",
                    "candidate_id",
                    "intent",
                    "confidence",
                    "reason_codes",
                    "rationale",
                    "model",
                    "llm_run",
                }
                if set(payload) != expected_keys:
                    raise ValueError("Sol decision envelope does not match the strict schema")
                packet_id = str(payload["packet_id"])
                packet_hash = str(payload["packet_hash"])
                action = str(payload["action"])
                candidate_id = str(payload.get("candidate_id", ""))
                direct_intent = payload.get("intent")
                confidence = Decimal(str(payload["confidence"]))
                reason_codes = tuple(str(value) for value in payload["reason_codes"])
                rationale = str(payload["rationale"])
                model = str(payload["model"])
                run = _llm_run_from_wire(payload["llm_run"])
                if (
                    run.provider != SOL_PROVIDER
                    or run.model != SOL_MODEL
                    or run.purpose != SOL_PURPOSE
                    or run.status != "COMPLETED"
                    or run.input_hash != packet_hash
                ):
                    raise ValueError("invalid Sol decision telemetry identity")
                row = audit.db.execute(
                    "SELECT payload_json,created_at,expires_at FROM ai_decision_packets "
                    "WHERE packet_id=? AND packet_hash=?",
                    (packet_id, packet_hash),
                ).fetchone()
                if row is None:
                    raise ValueError("Sol decision telemetry is not bound to a packet")
                from .sol_runner import _prompt

                packet = {
                    "packet_id": packet_id,
                    "packet_hash": packet_hash,
                    "payload": json.loads(str(row[0])),
                    "created_at": str(row[1]),
                    "expires_at": int(row[2]),
                }
                expected_prompt_hash = hashlib.sha256(_prompt(packet).encode()).hexdigest()
                decision_body = {key: payload[key] for key in expected_keys if key != "llm_run"}
                if run.prompt_hash != expected_prompt_hash or run.output_hash != sha256_text(
                    canonical_json(decision_body)
                ):
                    raise ValueError("Sol decision telemetry hash mismatch")
                review_store.record_run(run)
            else:
                packet_id = args.packet_id
                packet_hash = args.packet_hash
                action = args.action
                candidate_id = args.candidate_id
                direct_intent = None
                confidence = Decimal(args.confidence)
                reason_codes = tuple(args.reason_code)
                rationale = args.rationale
                model = args.model
            store.decide(
                packet_id,
                packet_hash,
                action,
                candidate_id,
                confidence,
                reason_codes,
                rationale,
                model,
                intent=(direct_intent if isinstance(direct_intent, dict) else None),
            )
            print(f"AI_DECISION=RECORDED packet_id={packet_id} action={action}")
    elif args.command == "kill":
        (runtime / "KILL_SWITCH").touch(exist_ok=True)
        audit.set_kill_state(KillState.LOCKED, "cli-owner", "manual kill")
        print("KILL_STATE=LOCKED")
    elif args.command == "resume":
        if args.confirm != "RESUME_DEMO":
            raise SystemExit("resume requires --confirm RESUME_DEMO")
        if not audit.verify_chain():
            raise SystemExit("resume refused: audit chain is invalid")
        if audit.state_get("master_reconciliation_drift", ""):
            raise SystemExit("resume refused: master reconciliation drift is unresolved")
        if audit.state_get("master_pending_execution", ""):
            raise SystemExit("resume refused: master execution is still pending")
        if any(item["state"] == "UNKNOWN" for item in audit.list_pending()):
            raise SystemExit("resume refused: UNKNOWN execution requires reconciliation")
        (runtime / "KILL_SWITCH").unlink(missing_ok=True)
        audit.set_kill_state(KillState.ACTIVE, "cli-owner", "explicit resume")
        print("KILL_STATE=ACTIVE")
    elif args.command == "init-security":
        key_path = runtime / "risk-signing.key"
        verifying_path = runtime / "risk-verifying.pub"
        generate_signing_keypair(key_path, verifying_path)
        audit.set_kill_state(KillState.LOCKED, "cli-owner", "security initialized")
        print(f"SIGNER=PROVISIONED private={key_path} public={verifying_path} kill=LOCKED")
    elif args.command == "status":
        print(
            json.dumps(
                {
                    "mode": config.account_mode,
                    "real_money": False,
                    "demo_execution_enabled": config.etoro_demo_execution_enabled,
                    "kill_state": audit.kill_state().value,
                    "kill_file": (runtime / "KILL_SWITCH").exists(),
                    "pending_approvals": len(audit.list_pending()),
                    "audit_events": audit.event_count(),
                    "audit_chain_valid": audit.verify_chain(),
                    "ai_pending": len(AIDecisionStore(audit).pending()),
                },
                indent=2,
            )
        )
    elif args.command == "dashboard":
        import uvicorn

        from .dashboard import DashboardService, create_app
        from .strategy import build_strategy_suite

        suite = build_strategy_suite(config.strategy)
        strategy_definitions = tuple(
            {
                **definition,
                "parameter_version": strategy.metadata.parameter_version,
                "fingerprint": strategy.metadata.fingerprint,
                "timeframe": config.candle_interval,
            }
            for definition, strategy in zip(STRATEGY_DEFINITIONS, suite, strict=True)
        )
        service = DashboardService(
            audit.path,
            runtime,
            strategies=strategy_definitions,
            ai_budgets=(
                {
                    "model": "gpt-5.6-sol",
                    "limit": config.sol_daily_call_limit,
                    "scope": "provider quota"
                    if config.sol_daily_call_limit is None
                    else "configured daily",
                },
                {
                    "model": MINIMAX_MODEL,
                    "limit": config.minimax_daily_review_limit,
                    "scope": "configured daily",
                },
            ),
        )
        application = create_app(service, control_audit=audit)
        if args.uds:
            uvicorn.run(application, uds=args.uds)
        else:
            uvicorn.run(application, host=args.host, port=args.port)
    elif args.command in {"shadow-once", "shadow-worker"}:
        from .engine import AutonomousShadowEngine

        engine = AutonomousShadowEngine(config, audit)
        collector = MarketDataCollector(EtoroMCPClient())
        if args.command == "shadow-once":
            print(json.dumps(engine.collect_and_tick(collector).__dict__, default=str, indent=2))
        else:
            engine.run_forever(collector, args.interval)
    elif args.command == "backtest":
        result = run_backtest(
            MovingAverageStrategy(config.strategy),
            args.symbol.upper(),
            load_closes(args.csv),
            config.initial_cash_usd,
        )
        audit.append("backtest", result.__dict__)
        print(json.dumps(result.__dict__, default=str, indent=2))
    elif args.command == "run-once":
        if (
            config.account_mode == "demo"
            and args.execute_demo
            and not config.etoro_demo_execution_enabled
        ):
            raise SystemExit("DEMO execution is disabled; proposal generation remains available")
        agent = TradingAgent(config, audit, MarketDataCollector(EtoroMCPClient()), runtime)
        result = agent.run_once(args.symbol)
        print(json.dumps(result, default=str, indent=2))
        if result.get("status") == "awaiting_operator_approval" and args.execute_demo:
            proposal_id = str(result["proposal_id"])
            envelope_hash = str(result["envelope_hash"])
            confirmation = input(
                f"Type APPROVE {proposal_id} {envelope_hash[-8:]} to execute this one DEMO order: "
            )
            if confirmation != f"APPROVE {proposal_id} {envelope_hash[-8:]}":
                raise SystemExit("not approved; nothing executed")
            audit.approve_once(proposal_id, envelope_hash, "cli-owner")
            print(json.dumps(agent.execute_pending_demo(proposal_id), default=str, indent=2))


if __name__ == "__main__":
    main()
